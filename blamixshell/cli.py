"""BlamixShell command line: manage the same encrypted vault as the desktop app, connect
interactively, and run commands across many servers. No Qt needed.

    blamixshell                 open the TUI (if Textual is installed)
    blamixshell ls [query]      list servers (query: words, tag:prod)
    blamixshell connect NAME    interactive shell (NAME, id, or user@host[:port]); -L/-R/-D add tunnels
    blamixshell tunnel NAME     run a server's saved tunnels (plus -L/-R/-D) without a shell
    blamixshell exec QUERY -- CMD   run CMD on every matching server in parallel
    blamixshell aws login|instances|import   AWS SSO sign-in, list / import SSM-managed EC2 instances
    blamixshell add | rm NAME | import ssh-config|putty | passwd | gui
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__, aws
from .models import Server, Store, Tunnel
from .paths import VAULT_POINTER, data_dir, default_vault_path, vault_path
from .ssh_core import (AuthConfigError, ChangedHostKey, UnknownHostKey, exec_command, fingerprint,
                       friendly_error, open_client, open_shell, trust_host_key)
from .vault import Vault, VaultError, WrongPassword

# ----------------------------------------------------------------- output helpers
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _TTY else text


def die(msg: str, code: int = 1) -> None:
    print(c("✖ ", "31") + msg, file=sys.stderr)
    sys.exit(code)


def confirm(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    ans = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not ans else ans in ("y", "yes")


# ----------------------------------------------------------------- vault
def unlock(create_if_missing: bool = True) -> Store:
    path = vault_path()
    if not Vault.exists(path) and path != default_vault_path():
        die(f"Your vault is set to {path}, but that file isn't there (drive not connected, "
            "or still syncing?). Fix it in the desktop app, or delete "
            f"{data_dir() / VAULT_POINTER} to use the local vault.")
    if not Vault.exists(path):
        if not create_if_missing:
            die(f"No vault at {path}")
        print(c("Creating a new BlamixShell vault", "1") + f"  ({path})")
        print("Everything is encrypted with your master password. It cannot be recovered if lost.")
        while True:
            pw = getpass.getpass("New master password: ")
            if len(pw) < 8:
                print("Use at least 8 characters.")
                continue
            if pw != getpass.getpass("Confirm: "):
                print("Passwords don't match.")
                continue
            return Store(Vault.create(path, pw), {})
    for _attempt in range(3):
        pw = getpass.getpass("Master password: ")
        try:
            v, data = Vault.open(path, pw)
            return Store(v, data)
        except WrongPassword:
            print(c("Wrong master password.", "31"))
        except VaultError as e:
            die(str(e))
    die("Too many attempts.")
    raise SystemExit  # for type checkers


# ----------------------------------------------------------------- lookup
def find_server(store: Store, target: str) -> Server | None:
    t = target.strip()
    if t in store.servers:
        return store.servers[t]
    by_name = [s for s in store.servers.values() if s.name.lower() == t.lower()]
    if by_name:
        return by_name[0]
    matches = [s for s in store.servers.values() if s.matches(t)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(s.label for s in matches[:8])
        die(f"“{target}” matches {len(matches)} servers: {names}")
    return None


def adhoc_server(target: str) -> Server | None:
    t = target
    port = 22
    user = ""
    if "@" in t:
        user, t = t.rsplit("@", 1)
    if t.count(":") == 1:
        t, p = t.split(":")
        if not p.isdigit():
            return None
        port = int(p)
    if not t or " " in t:
        return None
    return Server(host=t, port=port, username=user or getpass.getuser(), auth="agent")


# ----------------------------------------------------------------- connecting
class Connector:
    """Opens connections with interactive prompts (host keys, passwords)."""

    def __init__(self, store: Store, accept_new: bool = False, interactive: bool = True):
        self.store = store
        self.accept_new = accept_new
        self.interactive = interactive and sys.stdin.isatty()
        self._pw: dict[str, str] = {}
        self._lock = threading.Lock()

    def prepare(self, server: Server) -> Server:
        """Copy of the server with a password filled in (prompting once if needed)."""
        s = server.copy()
        if s.needs_password and not s.password:
            with self._lock:
                if s.id not in self._pw:
                    if not self.interactive:
                        raise AuthConfigError("No saved password (run interactively to enter one)")
                    self._pw[s.id] = getpass.getpass(f"Password for {s.address}: ")
            s.password = self._pw[s.id]
        return s

    def ask(self, title: str, instructions: str, prompts: list) -> list[str] | None:
        """2FA / keyboard-interactive prompts, answered in the terminal."""
        with self._lock:
            for line in (title, instructions):
                if line and line.strip():
                    print(c(line.strip(), "1"))
            try:
                return [input(p) if echo else getpass.getpass(p) for p, echo in prompts]
            except EOFError:
                return None

    def resolve(self, sid: str) -> Server | None:
        s = self.store.servers.get(sid)
        return self.prepare(s) if s else None

    def open(self, server: Server, log=lambda m: None):
        for _attempt in range(4):
            try:
                return open_client(self.prepare(server), self.resolve, log,
                                   interactive=self.ask if self.interactive else None)
            except UnknownHostKey as e:
                fp = f"{e.key.get_name()} {fingerprint(e.key)}"
                if self.accept_new:
                    trust_host_key(e.host_id, e.key)
                    log(f"trusted new host key for {e.host_id}: {fp}")
                    continue
                if not self.interactive:
                    raise AuthConfigError(f"Unknown host key for {e.host_id} ({fp}); "
                                          "connect once interactively or pass --accept-new") from None
                print(f"First connection to {c(e.host_id, '1')}.\n  Host key: {fp}")
                if confirm("Trust this key and continue?"):
                    trust_host_key(e.host_id, e.key)
                    continue
                raise AuthConfigError("Host key not trusted") from None
            except aws.LoginRequired as e:
                if not (e.sso and self.interactive and aws_login(e.profile)):
                    raise AuthConfigError(str(e)) from None
                continue
            except ChangedHostKey as e:
                msg = (f"HOST KEY FOR {e.host_id} HAS CHANGED!\n"
                       f"  new key: {e.key.get_name()} {fingerprint(e.key)}\n"
                       "  This can mean a man-in-the-middle attack, or that the server was reinstalled.")
                if not self.interactive:
                    raise AuthConfigError(msg) from None
                print(c(msg, "1;31"))
                if sys.stdin.isatty() and input("Type 'replace' to trust the new key: ").strip() == "replace":
                    trust_host_key(e.host_id, e.key, replace=True)
                    continue
                raise AuthConfigError("Aborted: host key mismatch") from None
            except Exception as e:
                import paramiko
                no_method = isinstance(e, paramiko.SSHException) and "authentication methods" in str(e)
                if (isinstance(e, paramiko.AuthenticationException) or no_method) and server.auth == "agent" \
                        and server.id not in self.store.servers and self.interactive:
                    server.auth = "password"      # ad-hoc target: keys didn't work, ask for a password
                    continue
                if isinstance(e, paramiko.AuthenticationException) and server.auth == "password" \
                        and not server.password and self.interactive:
                    print(c("Authentication failed, try again.", "31"))
                    self._pw.pop(server.id, None)
                    continue
                raise
        raise AuthConfigError("Giving up after several attempts")


def aws_login(profile: str) -> bool:
    """`aws sso login` in this terminal (the browser opens). True when it worked."""
    import subprocess
    cmd = aws.cli()
    if not cmd:
        print(c(str(aws.CliMissing()), "31"))
        return False
    print(c(f"Signing in to AWS SSO (profile {profile or 'default'}) …", "36"))
    code = subprocess.call(cmd + ["sso", "login"] + (["--profile", profile] if profile else []), env=aws.env())
    return code == 0


def ssm_shell(server: Server, store: Store) -> int:
    """A plain AWS Session Manager shell: the AWS CLI takes over this terminal."""
    import subprocess
    print(c(f"→ {server.label}", "1;36") + c(f"  {server.address}", "90"))
    try:
        for _attempt in range(2):
            try:
                aws.check_credentials(server.aws_profile, server.aws_region)
                break
            except aws.LoginRequired as e:
                if not (e.sso and sys.stdin.isatty() and aws_login(e.profile)):
                    raise
        if not aws.plugin() and "BLAMIXSHELL_AWS" not in os.environ:
            raise aws.PluginMissing()
        argv = aws.shell_argv(server)
    except aws.AwsError as e:
        die(str(e))
    if server.id in store.servers:
        store.touch(server.id)
    code = subprocess.call(argv, env=aws.env())
    print(c(f"\n← disconnected from {server.label}", "90"))
    return code


def _log_settings() -> dict:
    try:
        from .settings import Settings
        return Settings()
    except Exception:
        return {}


def interactive_shell(server: Server, store: Store, record: bool = False) -> int:
    """Hand the local terminal to a remote shell (raw mode, resize-aware)."""
    if server.connection == "ssm-shell":
        return ssm_shell(server, store)
    if os.name != "posix":
        die("Interactive sessions from the CLI need macOS/Linux. On Windows use the desktop app.")
    import select
    import signal
    import termios
    import tty

    conn = Connector(store)
    print(c(f"→ {server.label}", "1;36") + c(f"  {server.address}", "90"))
    try:
        client, chain = conn.open(server, lambda m: print(c(f"  {m}", "90")))
    except AuthConfigError as e:
        die(str(e))
    except Exception as e:
        die(friendly_error(e))
    if server.id in store.servers:
        store.touch(server.id)

    size = os.get_terminal_size() if sys.stdout.isatty() else os.terminal_size((120, 32))
    chan = open_shell(client, server, term=os.environ.get("TERM", "xterm-256color"),
                      width=size.columns, height=size.lines)
    if server.agent_forward:
        print(c("  agent forwarding on", "90"))
    if server.startup_cmd:
        chan.send((server.startup_cmd.rstrip("\n") + "\n").encode())
    tunnels = start_tunnels(client, server)
    recorder = None
    if record or server.record_sessions:
        from .session_log import Recorder, log_root
        st = _log_settings()
        try:
            recorder = Recorder(log_root(st), server, st.get("record_format", "text"),
                                bool(st.get("record_timestamps")))
            print(c(f"  ● recording to {recorder.path}", "31"))
        except OSError as e:
            print(c(f"  could not start recording: {e}", "33"))

    def on_resize(*_a):
        try:
            sz = os.get_terminal_size()
            chan.resize_pty(width=sz.columns, height=sz.lines)
        except Exception:
            pass

    fd = sys.stdin.fileno()
    is_tty = sys.stdin.isatty()
    old = termios.tcgetattr(fd) if is_tty else None
    old_handler = signal.signal(signal.SIGWINCH, on_resize)
    out = sys.stdout.fileno()
    try:
        if is_tty:
            tty.setraw(fd)
        chan.settimeout(0.0)
        stdin_open = True
        while True:
            watch = [chan] + ([fd] if stdin_open else [])
            r, _, _ = select.select(watch, [], [], 0.5)
            if chan in r:
                try:
                    data = chan.recv(65536)
                except Exception:
                    data = b""
                if not data:
                    break
                os.write(out, data)
                if recorder is not None:
                    recorder.write(data)
            if fd in r:
                data = os.read(fd, 4096)
                if not data:
                    stdin_open = False
                    chan.shutdown_write()
                else:
                    chan.sendall(data)
            if chan.exit_status_ready() and not chan.recv_ready():
                break
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        signal.signal(signal.SIGWINCH, old_handler)
    code = chan.recv_exit_status() if chan.exit_status_ready() else 0
    if recorder is not None:
        print(c(f"\n■ recording saved: {recorder.close()}", "90"), end="")
    if tunnels:
        tunnels.stop()
    client.close()
    for cl in chain:
        cl.close()
    print(c(f"\n← disconnected from {server.label}", "90"))
    return code


def start_tunnels(client, server: Server):
    """Start the server's enabled tunnels and print one line per tunnel."""
    from .tunnels import TunnelManager
    wanted = [t for t in server.tunnels if t.enabled]
    if not wanted:
        return None
    mgr = TunnelManager(client.get_transport(), wanted,
                        log=lambda m: print(c(f"\r  ⚠ {m}", "33"), flush=True))
    for st in mgr.start():
        print(c("  ⇄ ", "36") + st.summary() if st.ok else c(f"  ✖ {st.summary()}", "31"))
    return mgr


def add_cli_tunnels(server: Server, a) -> Server:
    """Copy of the server with -L/-R/-D tunnels from the command line added."""
    s = server.copy()
    for kind in ("L", "R", "D"):
        for spec in getattr(a, f"fwd_{kind}", None) or []:
            try:
                s.tunnels.append(Tunnel.parse(kind, spec))
            except ValueError as e:
                die(str(e))
    return s


def run_tunnels(server: Server, store: Store) -> int:
    """Keep a connection open just for its tunnels, until Ctrl+C or the connection drops."""
    if not any(t.enabled for t in server.tunnels):
        die(f"{server.label} has no tunnels. Add some in the app, or pass -L/-R/-D "
            "(e.g. -L 5432:localhost:5432 or -D 1080).")
    conn = Connector(store)
    print(c(f"→ {server.label}", "1;36") + c(f"  {server.address}", "90"))
    try:
        client, chain = conn.open(server, lambda m: print(c(f"  {m}", "90")))
    except AuthConfigError as e:
        die(str(e))
    except Exception as e:
        die(friendly_error(e))
    mgr = start_tunnels(client, server)
    if not mgr or not mgr.active:
        client.close()
        for cl in chain:
            cl.close()
        die("No tunnel could be started.")
    print(c("Tunnels are up. Press Ctrl+C to stop.", "90"))
    tr = client.get_transport()
    try:
        while tr.is_active():
            time.sleep(0.5)
        print(c("✖ The connection was closed by the server.", "31"))
        code = 1
    except KeyboardInterrupt:
        print()
        code = 0
    mgr.stop()
    client.close()
    for cl in chain:
        cl.close()
    print(c(f"← tunnels to {server.label} stopped", "90"))
    return code


# ----------------------------------------------------------------- exec on many
COLORS = ["36", "35", "33", "32", "34", "96", "95", "93", "92", "94"]


def run_many(store: Store, servers: list[Server], command: str, accept_new: bool = False,
             parallel: int = 10, timeout: float | None = None) -> int:
    conn = Connector(store, accept_new=accept_new)
    # ask for any missing passwords up front, before output starts interleaving
    for s in servers:
        try:
            conn.prepare(s)
        except AuthConfigError:
            pass
    conn.interactive = False
    width = max(len(s.label) for s in servers)
    lock = threading.Lock()
    results: dict[str, tuple[int | None, str, float]] = {}

    def emit(s: Server, idx: int, line: str, err: bool = False) -> None:
        tag = c(s.label.ljust(width), COLORS[idx % len(COLORS)])
        with lock:
            print(f"{tag} │ {c(line, '31') if err else line}", flush=True)

    def one(idx_s):
        idx, s = idx_s
        t0 = time.time()
        try:
            client, chain = conn.open(s)
        except Exception as e:
            msg = str(e) if isinstance(e, AuthConfigError) else friendly_error(e)
            emit(s, idx, msg, err=True)
            results[s.id] = (None, msg, time.time() - t0)
            return
        try:
            _in, out, errs = exec_command(client, command, agent_forward=s.agent_forward, timeout=timeout)
            ch = out.channel

            def pump(stream, is_err):
                for raw in iter(stream.readline, ""):
                    emit(s, idx, raw.rstrip("\n"), err=is_err)
            t = threading.Thread(target=pump, args=(errs, True), daemon=True)
            t.start()
            pump(out, False)
            t.join()
            code = ch.recv_exit_status()
            results[s.id] = (code, "", time.time() - t0)
        except Exception as e:
            emit(s, idx, friendly_error(e), err=True)
            results[s.id] = (None, friendly_error(e), time.time() - t0)
        finally:
            client.close()
            for cl in chain:
                cl.close()

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        list(ex.map(one, enumerate(servers)))

    print()
    failed = 0
    for s in servers:
        code, err, dur = results.get(s.id, (None, "not run", 0))
        ok = code == 0
        failed += 0 if ok else 1
        status = c("ok", "32") if ok else c(f"exit {code}" if code is not None else "error", "31")
        print(f"  {s.label.ljust(width)}  {status}  {c(f'{dur:.1f}s', '90')}")
    print(c(f"\n{len(servers) - failed}/{len(servers)} succeeded", "1"))
    return 0 if failed == 0 else 2


# ----------------------------------------------------------------- commands
def cmd_ls(store: Store, a) -> int:
    servers = sorted((s for s in store.servers.values() if s.matches(" ".join(a.query))),
                     key=lambda s: (s.group.lower(), s.label.lower()))
    if a.json:
        print(json.dumps([{"id": s.id, "name": s.label, "host": s.host, "port": s.port,
                           "user": s.username, "group": s.group, "tags": s.tags} for s in servers], indent=2))
        return 0
    if not servers:
        print("No servers." + ("" if store.servers else "  Add one with: blamixshell add"))
        return 0
    group = None
    for s in servers:
        if s.group != group:
            group = s.group
            print(c(f"\n{group or '(no group)'}", "1;34"))
        star = c("★ ", "33") if s.favorite else "  "
        tags = c(" ".join("#" + t for t in s.tags), "90")
        via = c(f"  via {store.servers[s.jump_id].label}", "90") if s.jump_id in store.servers else ""
        print(f" {star}{c(s.label, '1'):<30} {s.address:<32} {tags}{via}")
    print()
    return 0


def _ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{c(f' [{default}]', '90') if default else ''}: ").strip()
    return v or default


def cmd_add(store: Store, a) -> int:
    s = Server()
    s.host = a.host or _ask("Host (user@host:port ok)")
    if not s.host:
        die("Host is required")
    if "@" in s.host:
        s.username, s.host = s.host.rsplit("@", 1)
    if s.host.count(":") == 1:
        s.host, p = s.host.split(":")
        s.port = int(p)
    if a.port:
        s.port = a.port
    if a.ssm:
        s.connection = "ssm-shell" if a.ssm == "shell" else "ssm-ssh"
        s.aws_profile, s.aws_region = a.aws_profile or "", a.aws_region or ""
        s.eic = a.eic and a.ssm == "ssh"
        if not aws.is_instance_id(s.host):
            print(c(f"note: “{s.host}” doesn't look like an instance id (i-…)", "33"))
    if s.connection == "ssm-shell" or s.eic:
        s.username = a.user or s.username or ("" if s.connection == "ssm-shell" else _ask("OS user", "ec2-user"))
        s.name = a.name or _ask("Name", s.host)
        s.group = a.group if a.group is not None else _ask("Group (e.g. AWS/prod)", "")
        tags = a.tags if a.tags is not None else ""
        s.tags = [t.strip() for t in tags.split(",") if t.strip()]
        store.upsert(s)
        print(c("✔ ", "32") + f"Saved {s.label} ({s.address})")
        return 0
    s.username = a.user or s.username or _ask("Username", getpass.getuser())
    s.name = a.name or _ask("Name", s.host)
    s.auth = a.auth or _ask("Auth (password/key/agent)", "password")
    if s.auth not in ("password", "key", "agent"):
        die("Auth must be password, key or agent")
    if s.auth == "password" and not a.no_password:
        s.password = getpass.getpass("Password (empty = ask every time): ")
    if s.auth == "key":
        s.key_path = a.key or _ask("Private key file", os.path.expanduser("~/.ssh/id_ed25519"))
        s.passphrase = getpass.getpass("Key passphrase (empty if none): ")
    s.group = a.group if a.group is not None else _ask("Group (e.g. Prod/EU)", "")
    tags = a.tags if a.tags is not None else _ask("Tags (comma separated)", "")
    s.tags = [t.strip() for t in tags.split(",") if t.strip()]
    if a.jump:
        j = find_server(store, a.jump)
        if not j:
            die(f"Jump host “{a.jump}” not found")
        s.jump_id = j.id
    store.upsert(s)
    print(c("✔ ", "32") + f"Saved {s.label} ({s.address})")
    return 0


def cmd_rm(store: Store, a) -> int:
    s = find_server(store, a.target)
    if not s:
        die(f"No server matching “{a.target}”")
    if a.yes or confirm(f"Delete {s.label} ({s.address})?"):
        store.delete(s.id)
        print(c("✔ ", "32") + f"Deleted {s.label}")
    return 0


def cmd_connect(store: Store, a) -> int:
    s = find_server(store, a.target) or adhoc_server(a.target)
    if not s:
        die(f"No server matching “{a.target}”")
    s = add_cli_tunnels(s, a)
    if a.agent:
        s.agent_forward = True
    return interactive_shell(s, store, record=a.record)


def _open_server(store: Store, target: str, accept_new: bool = False):
    """(server, client, chain) for `target`, with the usual prompts; exits with a message when it can't."""
    s = find_server(store, target) or adhoc_server(target)
    if not s:
        die(f"No server matching “{target}”")
    if not s.uses_ssh:
        die(f"{s.label} is an AWS SSM shell: this needs SSH (switch it to SSH over SSM).")
    try:
        client, chain = Connector(store, accept_new=accept_new).open(s)
    except AuthConfigError as e:
        die(str(e))
    except Exception as e:
        die(friendly_error(e))
    return s, client, chain


def _close_server(client, chain) -> None:
    client.close()
    for cl in chain:
        cl.close()


def cmd_dash(store: Store, a) -> int:
    """The server dashboard, full screen in this terminal (the desktop dashboard's tabs)."""
    try:
        from . import collect, dashboard as dash
        from .tui_dash import run_dashboard
    except ImportError:
        die("The terminal dashboard needs the 'textual' package (pip install textual).")
    s, client, chain = _open_server(store, a.target, a.accept_new)
    ctx = collect.Context(dash.Runner(client), s.username, ssh_port=s.port)
    ctx.chain = chain
    ctx.opener = lambda interactive: Connector(store, accept_new=a.accept_new, interactive=interactive).open(s)
    try:
        run_dashboard(s, ctx)
    finally:
        ctx.close()
    return 0


def cmd_show(store: Store, a) -> int:
    """Print one dashboard tab as a table or JSON. Exit code 1 with --check when something is marked bad."""
    import json
    from . import collect, dashboard as dash
    names = [k for k, _t, _f in collect.TABS]
    hits = [k for k in names if k == a.tab.lower()] or [k for k in names if k.startswith(a.tab.lower())]
    if len(hits) != 1:
        die(f"Unknown tab “{a.tab}”. Tabs: {', '.join(names)}")
    key = hits[0]
    s, client, chain = _open_server(store, a.target, a.accept_new)
    try:
        ctx = collect.Context(dash.Runner(client), s.username)
        if a.sudo:
            ctx.sudo_pw = getpass.getpass(f"sudo password on {s.label}: ")
        try:
            table = collect.LOADERS[key](ctx)
        except collect.NeedsSudo:
            die("This needs root and sudo asks for a password: run again with --sudo.")
        except Exception as e:
            die(friendly_error(e))
    finally:
        _close_server(client, chain)
    rows = list(zip(table.rows, table.styles))
    if a.filter:
        q = a.filter.lower()
        rows = [(r, st) for r, st in rows if q in " ".join(r).lower()]
    if a.sort:
        from .tablekeys import auto_key
        cols = [x.lower() for x in table.columns]
        col = next((i for i, x in enumerate(cols) if x.startswith(a.sort.lower())), None)
        if col is None:
            die(f"Unknown column “{a.sort}”. Columns: {', '.join(table.columns)}")
        rows.sort(key=lambda rs: auto_key(rs[0][col]), reverse=a.desc)
    if a.json:
        print(json.dumps({"server": s.label, "tab": key, "note": table.note, "columns": table.columns,
                          "rows": [dict(zip(table.columns, r)) | ({"status": st} if st else {}) for r, st in rows]},
                         indent=2, ensure_ascii=False))
    else:
        cols = [x or " " for x in table.columns]
        cut = lambda v: v if len(v) <= 90 else v[:89] + "…"            # noqa: E731
        shown = [[cut(x) for x in r] for r, _ in rows]
        widths = [max([len(h)] + [len(r[i]) for r in shown]) for i, h in enumerate(cols)]
        color = {"ok": "32", "warn": "33", "bad": "31", "dim": "90"}
        print(c(f"{s.label}  ·  {key}" + (f"  ·  {table.note}" if table.note else ""), "1;36"))
        print(c("  ".join(h.ljust(widths[i]) for i, h in enumerate(cols)), "90"))
        for r, (_raw, st) in zip(shown, rows):
            line = "  ".join(x.ljust(widths[i]) for i, x in enumerate(r)).rstrip()
            print(c(line, color[st]) if st in color else line)
        if not rows:
            print(c("(nothing)", "90"))
    return 1 if a.check and any(st == "bad" for _r, st in rows) else 0


def cmd_report(store: Store, a) -> int:
    """A Markdown report of the server (overview, failed services, updates, ports, accounts, cron, firewall)."""
    from . import collect, dashboard as dash
    s, client, chain = _open_server(store, a.target, a.accept_new)
    try:
        md = collect.build_report(s.label, s.address, collect.Context(dash.Runner(client), s.username))
    finally:
        _close_server(client, chain)
    if a.output:
        from pathlib import Path
        Path(a.output).write_text(md, encoding="utf-8")
        print(f"Report saved: {a.output}")
    else:
        print(md, end="")
    return 0


def cmd_status(store: Store, a) -> int:
    """One-screen overview of a server (the dashboard's Overview, in the terminal)."""
    from . import dashboard as dash
    s = find_server(store, a.target) or adhoc_server(a.target)
    if not s:
        die(f"No server matching “{a.target}”")
    if not s.uses_ssh:
        die(f"{s.label} is an AWS SSM shell: status needs SSH (switch it to SSH over SSM).")
    conn = Connector(store)
    try:
        client, chain = conn.open(s)
    except AuthConfigError as e:
        die(str(e))
    except Exception as e:
        die(friendly_error(e))
    try:
        r = dash.Runner(client)
        ov = dash.overview(r)
        svcs, problem = dash.services(r) if a.services else ([], "")
    finally:
        client.close()
        for cl in chain:
            cl.close()

    def pct(v, warn=75, bad=90):
        return c(f"{v:3.0f}%", "31" if v >= bad else "33" if v >= warn else "32")

    def bar(v, width=24):
        n = round(width * v / 100)
        return c("█" * n, "31" if v >= 90 else "33" if v >= 80 else "36") + c("·" * (width - n), "90")
    print(c(f"{s.label}", "1;36") + c(f"  {ov.host}  ·  {ov.os}  ·  kernel {ov.kernel}  ·  up {dash.human_uptime(ov.uptime_s)}", "90"))
    if ov.cpu_percent is not None:
        print(f"  CPU     {pct(ov.cpu_percent)}   {ov.cpus} CPUs, load {ov.load[0]:.2f} {ov.load[1]:.2f} {ov.load[2]:.2f}")
    if ov.mem_total_kb:
        print(f"  Memory  {pct(ov.mem_percent)}   {dash.human_kb(ov.mem_used_kb)} of {dash.human_kb(ov.mem_total_kb)}")
    if ov.swap_total_kb:
        print(f"  Swap    {pct(ov.swap_percent)}   of {dash.human_kb(ov.swap_total_kb)}")
    for dk in ov.disks:
        print(f"  {dk.mount[:22]:<22} {bar(dk.percent)} {pct(dk.percent, 80, 90)}  "
              + c(f"{dash.human_kb(dk.used_kb)} of {dash.human_kb(dk.size_kb)}", "90"))
    failed = ov.failed_units or [sv.unit for sv in svcs if sv.failed]
    if ov.init and ov.init != "systemd":
        if failed:
            print(c(f"  ● {len(failed)} failed service(s): " + ", ".join(failed), "31"))
        else:
            print(c(f"  services managed by {dash.INIT_NAMES.get(ov.init, ov.init)}"
                    + ("" if a.services else " (add -s to list them)"), "90"))
    elif ov.systemd in ("", "offline", "unknown"):
        print(c("  no systemd" + (f" (first process: {ov.pid1})" if ov.pid1 and ov.pid1 != "systemd" else ""), "90"))
    elif ov.failed_units:
        print(c(f"  ● {len(ov.failed_units)} failed service(s): " + ", ".join(ov.failed_units), "31"))
    else:
        print(c(f"  ● all services OK ({ov.systemd})", "32"))
    for sv in svcs:
        if sv.active == "active" or sv.failed:
            print(f"    {c('●', '31' if sv.failed else '32')} {sv.unit:<40} {sv.sub:<10} " + c(sv.description, "90"))
    if a.services and problem:
        print(c("  " + problem.replace("\n", "\n  "), "90"))
    return 1 if failed else 0


def cmd_tunnel(store: Store, a) -> int:
    s = find_server(store, a.target) or adhoc_server(a.target)
    if not s:
        die(f"No server matching “{a.target}”")
    if not s.uses_ssh:
        die(f"{s.label} is an AWS SSM shell: tunnels need SSH (switch it to SSH over SSM).")
    s = s.copy()
    if a.only:
        s.tunnels = []
    return run_tunnels(add_cli_tunnels(s, a), store)


def cmd_exec(store: Store, a) -> int:
    query = a.query
    command = " ".join(a.command)
    if not command:
        die("Nothing to run. Usage: blamixshell exec <query> -- <command>")
    if query.startswith("group:"):
        g = query[6:]
        servers = [s for s in store.servers.values() if s.group == g or s.group.startswith(g + "/")]
    elif query in ("all", "*"):
        servers = list(store.servers.values())
    else:
        servers = [s for s in store.servers.values() if s.matches(query)]
    servers.sort(key=lambda s: s.label.lower())
    if a.agent:
        servers = [s.copy() for s in servers]
        for s in servers:
            s.agent_forward = True
    shells = [s for s in servers if not s.uses_ssh]
    servers = [s for s in servers if s.uses_ssh]
    if shells:
        print(c("Skipping AWS SSM shell servers (exec needs SSH): " + ", ".join(s.label for s in shells), "33"))
    if not servers:
        die(f"No servers match “{query}”")
    print(c(f"Running on {len(servers)} server(s): ", "1") + ", ".join(s.label for s in servers))
    print(c(f"$ {command}", "90"))
    if len(servers) > 1 and not a.yes and not confirm("Continue?", default=True):
        return 1
    st = _log_settings()
    from .session_log import CommandLog, log_root
    clog = CommandLog(log_root(st))
    for s in servers:
        if st.get("command_log") or s.log_commands:
            clog.add(s, command, "exec")
    return run_many(store, servers, command, accept_new=a.accept_new, parallel=a.parallel,
                    timeout=a.timeout)


def cmd_import(store: Store, a) -> int:
    from . import importers
    servers = importers.putty_sessions() if a.source == "putty" else importers.ssh_config_servers()
    if a.source == "putty" and sys.platform != "win32":
        die("PuTTY sessions live in the Windows registry; use: blamixshell import ssh-config")
    added = store.import_servers(servers)
    print(c("✔ ", "32") + f"Imported {added} new server(s) ({len(servers) - added} already existed)")
    return 0


def cmd_aws(store: Store | None, a) -> int:
    """blamixshell aws login | instances | import"""
    profile = "" if a.profile in (None, "default") else a.profile
    if a.action == "login":
        if not aws.cli():
            die(str(aws.CliMissing()))
        return 0 if aws_login(profile) else 1
    try:
        for _attempt in range(2):
            try:
                insts, note = aws.list_instances(profile, a.region or "")
                break
            except aws.LoginRequired as e:
                if not (e.sso and sys.stdin.isatty() and aws_login(e.profile)):
                    raise
    except aws.AwsError as e:
        die(str(e))
    if a.online:
        insts = [i for i in insts if i.online]
    if a.action == "instances":
        if a.json:
            print(json.dumps([i.__dict__ for i in insts], indent=2))
            return 0
        for i in insts:
            dot = c("●", "32" if i.online else "90")
            print(f" {dot} {i.label[:32]:<32} {i.id:<21} {(i.platform_name or i.platform)[:28]:<28} "
                  + c(f"{i.ping}{' · ' + i.state if i.state else ''}  {i.ip}", "90"))
        print(c(f"\n{len(insts)} instance(s)" + (f"  ·  {note}" if note else ""), "90"))
        return 0
    # import
    assert store is not None
    if not insts:
        die("No instances to import.")
    added, updated = aws.import_instances(store, insts, profile, a.region or "",
                                          "ssm-shell" if a.shell else "ssm-ssh", a.user or "",
                                          not a.no_eic, a.group or "")
    group = a.group or aws.default_group(profile, a.region or "")
    print(c("✔ ", "32") + f"Added {added}, updated {updated} server(s) in {group}")
    return 0


def cmd_passwd(store: Store, _a) -> int:
    pw = getpass.getpass("New master password: ")
    if len(pw) < 8 or pw != getpass.getpass("Confirm: "):
        die("Passwords must match and be at least 8 characters")
    store.vault.change_password(pw, store.to_dict())
    print(c("✔ ", "32") + "Master password changed")
    return 0


def cmd_tui(_store, _a) -> int:
    try:
        from .tui import run_tui
    except ImportError:
        die("The TUI needs Textual:  pip install textual   (or: pipx install 'blamixshell[tui]')")
    return run_tui()


def cmd_gui(_store, _a) -> int:
    try:
        from .main import main as gui_main
    except ImportError:
        die("The desktop app needs PySide6:  pip install PySide6")
    gui_main()
    return 0


def cmd_selftest(_store=None, _a=None) -> int:
    """For packaging checks (CI): can this build start the terminal dashboard? Uses no server and no vault."""
    import asyncio
    import types
    try:
        from . import collect, dashboard as dash
        from .tui import BlamixShellTUI  # noqa: F401  (its widgets must be in the build too)
        from .tui_dash import DashApp
    except ImportError as e:
        print(f"SELFTEST FAILED: {e}")
        return 1

    class Runner:                                       # a server that answers every command with nothing
        client = None

        def run(self, command, timeout=30, stdin=None):
            return dash.Result(0, "", "")

    async def go() -> bool:
        app = DashApp(types.SimpleNamespace(label="selftest", address="localhost"), collect.Context(Runner()))
        async with app.run_test(size=(120, 30)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if hasattr(app.screen, "pane") and app.screen.pane("overview").loaded:
                    return True
        return False
    try:
        ok = asyncio.run(go())
    except Exception as e:
        print(f"SELFTEST FAILED: {type(e).__name__}: {e}")
        return 1
    print("SELFTEST", "OK: terminal dashboard" if ok else "FAILED: the terminal dashboard did not load")
    return 0 if ok else 1


def cmd_update(a=None) -> int:
    """Check GitHub for a newer version; with --install, replace the one-file binary with it."""
    from . import updater
    install = bool(getattr(a, "install", False))
    try:
        rel = updater.check()
    except updater.UpdateError as e:
        die(str(e))
    if not rel:
        print(c("✔ ", "32") + f"BlamixShell {__version__} is the latest version.")
        return 0
    print(c(f"BlamixShell {rel.version} is available", "1") + f" (you have {__version__})\n")
    if rel.notes:
        print(rel.notes[:1500] + "\n")
    onefile = updater.is_onefile_binary()
    if not install:
        if onefile:
            print("Install it now with:  blamixshell update --install")
        elif updater.install_kind() == "source":
            print("Update with:  pipx upgrade blamixshell   (or git pull in your checkout)")
        else:
            print(f"Download:  {rel.page}")
        return 0
    if not onefile:
        die("--install replaces the one-file command-line binary (blamixshell-linux-<arch>). This copy was "
            "installed another way: use pipx upgrade blamixshell, or the installer or archive from "
            f"{rel.page}")
    asset = updater.pick_cli_asset(rel)
    if not asset:
        die(f"This release has no file named {updater.cli_asset_name() or 'for this system'}. Download it from {rel.page}")
    if not getattr(a, "yes", False) and not confirm(f"Replace {sys.executable} with BlamixShell {rel.version}?"):
        return 1

    def progress(done: int, total: int) -> None:
        if total and sys.stdout.isatty():
            print(f"\r  downloading {done * 100 // total:3d}%", end="", flush=True)
    try:
        target = updater.install_cli_binary(asset, progress=progress)
    except updater.UpdateError as e:
        print()
        die(str(e))
    print("\r" + c("✔ ", "32") + f"Updated to {rel.version}: {target}   (run blamixshell again)")
    return 0


def _tunnel_args(sp) -> None:
    sp.add_argument("-L", dest="fwd_L", action="append", metavar="[BIND:]PORT:HOST:HOSTPORT",
                    help="local forward, like ssh -L (repeatable)")
    sp.add_argument("-R", dest="fwd_R", action="append", metavar="[BIND:]PORT:HOST:HOSTPORT",
                    help="remote forward, like ssh -R (repeatable)")
    sp.add_argument("-D", dest="fwd_D", action="append", metavar="[BIND:]PORT",
                    help="SOCKS proxy, like ssh -D (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blamixshell", description="SSH server manager (shares the desktop app's vault).")
    p.add_argument("--version", action="version", version=f"blamixshell {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("ls", aliases=["list"], help="list servers")
    sp.add_argument("query", nargs="*", help="filter words, tag:prod")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("connect", aliases=["c", "ssh"], help="open an interactive shell")
    sp.add_argument("target", help="name, id, search term or user@host[:port]")
    sp.add_argument("-A", dest="agent", action="store_true", help="forward your SSH agent (like ssh -A)")
    sp.add_argument("--record", action="store_true", help="record the session to the logs folder")
    _tunnel_args(sp)
    sp = sub.add_parser("status", aliases=["st"], help="server overview: CPU, memory, disks, failed services")
    sp.add_argument("target", help="name, id, search term or user@host[:port]")
    sp.add_argument("-s", "--services", action="store_true", help="also list running and failed services")
    sp = sub.add_parser("tunnel", aliases=["t", "fwd"], help="run port forwards without a shell")
    sp.add_argument("target", help="name, id, search term or user@host[:port]")
    _tunnel_args(sp)
    sp.add_argument("--only", action="store_true", help="ignore the server's saved tunnels")
    sp = sub.add_parser("exec", aliases=["x"], help="run a command on many servers in parallel")
    sp.add_argument("query", help="search words, tag:prod, group:Prod/EU, or all")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="-- command to run")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sp.add_argument("-p", "--parallel", type=int, default=10)
    sp.add_argument("-t", "--timeout", type=float, default=None, help="per-command timeout in seconds")
    sp.add_argument("--accept-new", action="store_true", help="trust unknown host keys (never changed ones)")
    sp.add_argument("-A", dest="agent", action="store_true", help="forward your SSH agent (like ssh -A)")
    sp = sub.add_parser("aws", help="AWS: SSO sign-in, list or import SSM-managed instances")
    sp.add_argument("action", choices=["login", "instances", "import"])
    sp.add_argument("--profile", help="AWS CLI profile (default: AWS_PROFILE / default)")
    sp.add_argument("--region", help="AWS region (default: the profile's)")
    sp.add_argument("--online", action="store_true", help="only instances whose SSM agent is online")
    sp.add_argument("--json", action="store_true", help="instances: JSON output")
    sp.add_argument("--group", help="import: server group (default AWS/<profile>/<region>)")
    sp.add_argument("--user", help="import: SSH user (default: from the OS: ubuntu, ec2-user …)")
    sp.add_argument("--shell", action="store_true", help="import as plain SSM shells (no SSH)")
    sp.add_argument("--no-eic", action="store_true", help="import: don't use EC2 Instance Connect keys")
    sp = sub.add_parser("dash", help="server dashboard in the terminal (services, processes, logs, docker, …)")
    sp.add_argument("target")
    sp.add_argument("--accept-new", action="store_true", help="trust an unknown host key (never a changed one)")
    sp = sub.add_parser("show", help="print one dashboard tab: overview, services, processes, ports, updates, "
                                     "users, cron, firewall, docker, timers, storage, security")
    sp.add_argument("target")
    sp.add_argument("tab", help="a tab name (a unique start is enough: serv, proc, fire …)")
    sp.add_argument("-f", "--filter", help="only rows containing this text")
    sp.add_argument("--sort", help="sort by a column (a unique start is enough, e.g. cpu, mem, pid)")
    sp.add_argument("--desc", action="store_true", help="with --sort: biggest first")
    sp.add_argument("--json", action="store_true", help="JSON output, for scripts")
    sp.add_argument("--check", action="store_true", help="exit with 1 when any row is marked bad (health checks)")
    sp.add_argument("--sudo", action="store_true", help="ask for the sudo password (firewall, docker, security)")
    sp.add_argument("--accept-new", action="store_true", help="trust an unknown host key (never a changed one)")
    sp = sub.add_parser("report", help="Markdown report of a server")
    sp.add_argument("target")
    sp.add_argument("-o", "--output", help="write to this file instead of the screen")
    sp.add_argument("--accept-new", action="store_true", help="trust an unknown host key (never a changed one)")
    sp = sub.add_parser("add", help="add a server")
    for f in ("name", "host", "user", "auth", "key", "group", "tags", "jump"):
        sp.add_argument(f"--{f}")
    sp.add_argument("--port", type=int)
    sp.add_argument("--ssm", choices=["ssh", "shell"], help="connect through AWS SSM (host = instance id)")
    sp.add_argument("--aws-profile")
    sp.add_argument("--aws-region")
    sp.add_argument("--eic", action="store_true", help="with --ssm ssh: EC2 Instance Connect one-time keys")
    sp.add_argument("--no-password", action="store_true", help="don't store a password (ask on connect)")
    sp = sub.add_parser("rm", aliases=["remove"], help="delete a server")
    sp.add_argument("target")
    sp.add_argument("-y", "--yes", action="store_true")
    sp = sub.add_parser("import", help="import servers")
    sp.add_argument("source", choices=["ssh-config", "putty"])
    sub.add_parser("passwd", help="change the master password")
    sub.add_parser("tui", help="full-screen terminal UI")
    sub.add_parser("gui", help="launch the desktop app")
    sub.add_parser("where", help="show where data is stored")
    sub.add_parser("selftest", help=argparse.SUPPRESS)
    sp = sub.add_parser("update", help="check GitHub for a newer BlamixShell (--install: update the one-file binary)")
    sp.add_argument("--install", action="store_true", help="download the new one-file binary, verify it, replace this one")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask before replacing")
    return p


HANDLERS = {"ls": cmd_ls, "list": cmd_ls, "connect": cmd_connect, "c": cmd_connect, "ssh": cmd_connect,
            "tunnel": cmd_tunnel, "t": cmd_tunnel, "fwd": cmd_tunnel,
            "status": cmd_status, "st": cmd_status, "dash": cmd_dash, "show": cmd_show, "report": cmd_report,
            "exec": cmd_exec, "x": cmd_exec, "add": cmd_add, "rm": cmd_rm, "remove": cmd_rm,
            "import": cmd_import, "passwd": cmd_passwd, "aws": cmd_aws}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.cmd == "exec" and a.command and a.command[0] == "--":
        a.command = a.command[1:]
    try:
        if a.cmd is None:
            if sys.stdin.isatty():
                sys.exit(cmd_tui(None, a))
            parser.print_help()
            return
        if a.cmd == "tui":
            sys.exit(cmd_tui(None, a))
        if a.cmd == "gui":
            sys.exit(cmd_gui(None, a))
        if a.cmd == "where":
            print(data_dir())
            if vault_path() != default_vault_path():
                print(f"vault: {vault_path()}")
            return
        if a.cmd == "update":
            sys.exit(cmd_update(a))
        if a.cmd == "selftest":
            sys.exit(cmd_selftest())
        if a.cmd == "aws" and a.action != "import":
            sys.exit(cmd_aws(None, a))
        store = unlock()
        sys.exit(HANDLERS[a.cmd](store, a))
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
