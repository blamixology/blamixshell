"""AWS Systems Manager (SSM) and SSO support, Qt-free.

Everything goes through the AWS CLI v2, so your ~/.aws/config profiles, SSO sessions and
credential cache work exactly as they do in a terminal. Connecting through SSM also needs
AWS's Session Manager plugin (the CLI starts it).

    SSH over SSM   the CLI runs `ssm start-session --document-name AWS-StartSSHSession`;
                   its stdin/stdout carry the SSH connection (like ssh's ProxyCommand).
                   SFTP, tunnels and the dashboard work as on any SSH server.
    SSM shell      plain `aws ssm start-session` in a terminal (no SSH on the instance).
    EC2 Instance Connect  pushes a one-time public key for 60 seconds before connecting,
                   so no SSH keys need to live on the instance.
"""
from __future__ import annotations

import configparser
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

CLI_INSTALL_URL = "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
PLUGIN_INSTALL_URL = ("https://docs.aws.amazon.com/systems-manager/latest/userguide/"
                      "session-manager-working-with-install-plugin.html")

REGIONS = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "sa-east-1",
           "eu-central-1", "eu-central-2", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1",
           "eu-south-1", "eu-south-2", "me-central-1", "il-central-1", "af-south-1",
           "ap-south-1", "ap-south-2", "ap-southeast-1", "ap-southeast-2", "ap-southeast-3",
           "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-east-1"]

INSTANCE_ID = re.compile(r"^(i|mi)-[0-9a-f]{8,17}$")


# ---------------------------------------------------------------- errors
class AwsError(Exception):
    pass


class CliMissing(AwsError):
    def __init__(self):
        super().__init__(f"The AWS CLI (v2) isn't installed or not on PATH. Install it from {CLI_INSTALL_URL}")


class PluginMissing(AwsError):
    def __init__(self):
        super().__init__("AWS's Session Manager plugin isn't installed (the AWS CLI needs it for SSM). "
                         f"Install it from {PLUGIN_INSTALL_URL}, then try again.")


class LoginRequired(AwsError):
    """Credentials missing or expired. `sso` = `aws sso login` fixes it."""

    def __init__(self, profile: str, detail: str = "", sso: bool | None = None):
        self.profile = profile
        self.sso = is_sso_profile(profile) if sso is None else sso
        name = profile or "default"
        if self.sso:
            msg = f"Your AWS SSO session for profile “{name}” has expired or you haven't signed in yet."
        else:
            msg = (f"No valid AWS credentials for profile “{name}” (run `aws configure`, "
                   "or use a profile set up for SSO).")
        super().__init__(msg + (f"\n{detail}" if detail and not self.sso else ""))
        self.detail = detail


_LOGIN = re.compile(r"token has expired|sso session .*(expired|invalid)|aws sso login|"
                    r"error loading sso token|sso token .*(expired|does not exist)|expiredtoken|"
                    r"unable to locate credentials|security token included in the request is (expired|invalid)|"
                    r"unrecognizedclientexception|invalidclienttokenid|refresh failed", re.I)


def classify(text: str, profile: str = "") -> AwsError:
    """Turn AWS CLI error output into a friendly exception."""
    t = _clean(text)
    low = t.lower()
    if "sessionmanagerplugin is not found" in low or ("session-manager-plugin" in low and "not found" in low):
        return PluginMissing()
    if _LOGIN.search(t):
        return LoginRequired(profile, _last_error(t))
    m = re.search(r"config profile \((.*?)\) could not be found", t)
    if m:
        return AwsError(f"AWS profile “{m.group(1)}” isn't in your AWS config (~/.aws/config).")
    if "must specify a region" in t.lower():
        return AwsError("No AWS region: set one on the server, or in the profile (aws configure).")
    if "TargetNotConnected" in t:
        return AwsError("The instance isn't connected to Systems Manager (stopped, SSM agent not running, "
                        "or no route to the SSM endpoints).")
    if "AccessDenied" in t or "not authorized to perform" in t:
        return AwsError("AWS denied access: " + _last_error(t))
    if "InvalidInstanceId" in t or "EC2InstanceNotFoundException" in t:
        return AwsError("No such instance in this account/region: " + _last_error(t))
    if "EC2InstanceUnavailableException" in t or "EC2InstanceStateInvalidException" in t:
        return AwsError("EC2 Instance Connect can't reach the instance: " + _last_error(t))
    return AwsError(_last_error(t) or "The AWS CLI failed without saying why.")


def _clean(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "").replace("\r", "")


def _last_error(text: str) -> str:
    lines = [l.strip() for l in _clean(text).splitlines() if l.strip()]
    for l in reversed(lines):
        if "error" in l.lower() or "exception" in l.lower():
            return l[:400]
    return lines[-1][:400] if lines else ""


# ---------------------------------------------------------------- locating the tools
def _first_existing(paths) -> str:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return ""


def cli() -> list[str] | None:
    """The command that runs the AWS CLI (BLAMIXSHELL_AWS overrides, for tests)."""
    override = os.environ.get("BLAMIXSHELL_AWS")
    if override:      # a JSON list ["python", "fake_aws.py"] or a command line
        return json.loads(override) if override.lstrip().startswith("[") else shlex.split(override)
    found = shutil.which("aws") or _first_existing(
        [r"C:\Program Files\Amazon\AWSCLIV2\aws.exe", "/usr/local/bin/aws", "/opt/homebrew/bin/aws",
         os.path.expanduser("~/.local/bin/aws")])
    return [found] if found else None


def plugin() -> str:
    return shutil.which("session-manager-plugin") or _first_existing([
        r"C:\Program Files\Amazon\SessionManagerPlugin\bin\session-manager-plugin.exe",
        "/usr/local/sessionmanagerplugin/bin/session-manager-plugin",
        "/usr/local/bin/session-manager-plugin", "/opt/homebrew/bin/session-manager-plugin"])


def env() -> dict[str, str]:
    """Environment for the CLI: no pager or prompts, and the plugin findable even when it was
    installed after BlamixShell started (Windows doesn't refresh PATH for running apps)."""
    e = dict(os.environ)
    e["AWS_PAGER"] = ""
    e["AWS_CLI_AUTO_PROMPT"] = "off"
    e["PYTHONUNBUFFERED"] = "1"
    extra = [os.path.dirname(p) for p in (plugin(), (cli() or [""])[0]) if p and os.path.isabs(p)]
    if extra:
        e["PATH"] = os.pathsep.join([e.get("PATH", "")] + extra)
    return e


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}    # CREATE_NO_WINDOW


def _require_cli() -> list[str]:
    c = cli()
    if not c:
        raise CliMissing()
    return c


def run(args: list[str], profile: str = "", region: str = "", timeout: float = 60,
        json_out: bool = True):
    """Run an AWS CLI command, return parsed JSON (or text). Raises a classified AwsError."""
    argv = _require_cli() + args + (["--profile", profile] if profile else []) + \
        (["--region", region] if region else []) + (["--output", "json"] if json_out else [])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env(),
                           stdin=subprocess.DEVNULL, **_no_window())
    except subprocess.TimeoutExpired:
        raise AwsError(f"The AWS CLI didn't answer within {int(timeout)} s.") from None
    except OSError as e:
        raise AwsError(f"Could not run the AWS CLI: {e}") from None
    if r.returncode != 0:
        raise classify(r.stderr or r.stdout, profile)
    if not json_out:
        return r.stdout
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        raise AwsError("Unexpected output from the AWS CLI: " + r.stdout[:200]) from None


# ---------------------------------------------------------------- profiles (read ~/.aws directly: instant)
def _config_paths() -> tuple[Path, Path]:
    home = Path.home()
    return (Path(os.environ.get("AWS_CONFIG_FILE") or home / ".aws" / "config"),
            Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or home / ".aws" / "credentials"))


def _read_ini(path: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        pass
    return cp


def profiles() -> list[str]:
    """Profile names from ~/.aws/config and ~/.aws/credentials ("default" first)."""
    cfg, creds = _config_paths()
    names: list[str] = []
    for sec in _read_ini(cfg).sections():
        name = "default" if sec == "default" else sec[8:].strip() if sec.startswith("profile ") else ""
        if name and name not in names:
            names.append(name)
    for sec in _read_ini(creds).sections():
        if sec not in names:
            names.append(sec)
    return sorted(names, key=lambda n: (n != "default", n.lower()))


def profile_settings(profile: str) -> dict[str, str]:
    cfg, _ = _config_paths()
    cp = _read_ini(cfg)
    name = profile or os.environ.get("AWS_PROFILE") or "default"
    sec = "default" if name == "default" else f"profile {name}"
    out = dict(cp.items(sec)) if cp.has_section(sec) else {}
    sso = out.get("sso_session")
    if sso and cp.has_section(f"sso-session {sso}"):
        out.update({f"sso_session.{k}": v for k, v in cp.items(f"sso-session {sso}")})
    return out


def profile_region(profile: str) -> str:
    return profile_settings(profile).get("region", "") or os.environ.get("AWS_REGION", "") \
        or os.environ.get("AWS_DEFAULT_REGION", "")


def is_sso_profile(profile: str) -> bool:
    st = profile_settings(profile)
    return bool(st.get("sso_session") or st.get("sso_start_url"))


# ---------------------------------------------------------------- SSO login
_URL = re.compile(r"https://\S+")
_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


@dataclass
class LoginInfo:
    url: str = ""
    code: str = ""


def parse_login_output(text: str) -> LoginInfo:
    info = LoginInfo()
    for line in _clean(text).splitlines():
        line = line.strip()
        if "Successfully logged" in line:
            continue
        m = _URL.search(line)
        if m and not info.url:
            info.url = m.group(0)
        m = _CODE.search(line)
        if m and "http" not in line:
            info.code = m.group(0)
    return info


class SsoLogin:
    """`aws sso login --profile X` in the background. The CLI opens the browser itself; the
    output (the sign-in URL and, for the device flow, a code) is passed to `on_output`."""

    def __init__(self, profile: str, on_output=lambda text: None):
        self.profile = profile
        self.output = ""
        self.proc: subprocess.Popen | None = None
        self._on_output = on_output

    def start(self) -> None:
        argv = _require_cli() + ["sso", "login"] + (["--profile", self.profile] if self.profile else [])
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env(), **_no_window())
        threading.Thread(target=self._pump, daemon=True, name="aws-sso-login").start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(1)
            if not chunk:
                break
            self.output += chunk.decode("utf-8", "replace")
            self._on_output(self.output)

    def wait(self, timeout: float | None = None) -> int:
        assert self.proc
        return self.proc.wait(timeout)

    def info(self) -> LoginInfo:
        return parse_login_output(self.output)

    def error(self) -> str:
        return _last_error(self.output)

    def cancel(self) -> None:
        if self.proc and self.proc.poll() is None:
            _kill_tree(self.proc)


def check_credentials(profile: str = "", region: str = "") -> str:
    """The caller's ARN, or raises LoginRequired / AwsError."""
    return run(["sts", "get-caller-identity"], profile, region, timeout=30).get("Arn", "")


# ---------------------------------------------------------------- SSH over SSM (ProxyCommand)
def ssh_proxy_argv(server) -> list[str]:
    return _require_cli() + ["ssm", "start-session", "--target", server.host,
                             "--document-name", "AWS-StartSSHSession",
                             "--parameters", f"portNumber={int(server.port or 22)}"] + server.aws_args()


def shell_argv(server) -> list[str]:
    return _require_cli() + ["ssm", "start-session", "--target", server.host] + server.aws_args()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":     # aws.exe starts session-manager-plugin.exe: end both
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=10, **_no_window())
        proc.kill()
    except Exception:
        pass


class ProcessSocket:
    """A command's stdin/stdout as a socket (what ssh's ProxyCommand does).

    paramiko gets one end of a socket pair; threads copy between the other end and the
    process. Pipes can't be select()ed on Windows, a socket can."""

    def __init__(self, argv: list[str], environ: dict | None = None):
        self.argv = argv
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=environ, bufsize=0, **_no_window())
        except OSError as e:
            raise AwsError(f"Could not start {os.path.basename(argv[0])}: {e}") from None
        self.sock, self._peer = socket.socketpair()
        self._stderr = bytearray()
        self._closed = False
        for target, name in ((self._to_proc, "in"), (self._from_proc, "out"), (self._read_err, "err")):
            threading.Thread(target=target, daemon=True, name=f"ssm-proxy-{name}").start()

    def _to_proc(self) -> None:
        try:
            while True:
                data = self._peer.recv(65536)
                if not data:
                    break
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except OSError:
            pass
        try:
            self.proc.stdin.close()     # EOF: the session ends cleanly
        except OSError:
            pass

    def _from_proc(self) -> None:
        try:
            while True:
                data = self.proc.stdout.read(65536)
                if not data:
                    break
                self._peer.sendall(data)
        except OSError:
            pass
        try:
            self._peer.shutdown(socket.SHUT_WR)   # paramiko sees EOF
        except OSError:
            pass

    def _read_err(self) -> None:
        try:
            while True:
                data = self.proc.stderr.read(4096)
                if not data:
                    break
                if len(self._stderr) < 64 * 1024:
                    self._stderr += data
        except OSError:
            pass

    def error_text(self, wait: float = 3.0) -> str:
        """What the command printed on stderr (waits briefly for it to exit)."""
        try:
            self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.05)
        return self._stderr.decode("utf-8", "replace")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for s in (self.sock, self._peer):
            try:
                s.close()
            except OSError:
                pass

        def reap():
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                _kill_tree(self.proc)
        threading.Thread(target=reap, daemon=True).start()


def open_ssh_proxy(server) -> ProcessSocket:
    """Start `aws ssm start-session` (AWS-StartSSHSession) and return its socket."""
    argv = ssh_proxy_argv(server)
    if not plugin() and "BLAMIXSHELL_AWS" not in os.environ:
        raise PluginMissing()
    return ProcessSocket(argv, env())


# ---------------------------------------------------------------- EC2 Instance Connect
def ephemeral_key():
    """A fresh Ed25519 key pair that only lives in memory: (paramiko key, OpenSSH public key)."""
    import paramiko
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                             serialization.NoEncryption()).decode()
    pub = priv.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                         serialization.PublicFormat.OpenSSH).decode()
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem)), f"{pub} blamixshell-eic"


def send_ssh_public_key(server, public_key: str) -> None:
    """Allow `public_key` for server.username on the instance for 60 seconds."""
    if not server.username:
        raise AwsError("EC2 Instance Connect needs the instance's OS user (ec2-user, ubuntu, admin …).")
    out = run(["ec2-instance-connect", "send-ssh-public-key", "--instance-id", server.host,
               "--instance-os-user", server.username, "--ssh-public-key", public_key],
              server.aws_profile, server.aws_region, timeout=40)
    if out and out.get("Success") is False:
        raise AwsError("EC2 Instance Connect refused the key.")


# ---------------------------------------------------------------- instances
@dataclass
class Instance:
    id: str
    name: str = ""
    ping: str = ""                  # Online / ConnectionLost / Inactive
    platform: str = ""              # Linux / Windows / MacOS
    platform_name: str = ""         # "Amazon Linux", "Ubuntu" …
    ip: str = ""
    computer: str = ""
    state: str = ""                 # EC2 state when known (running, stopped …)

    @property
    def online(self) -> bool:
        return self.ping == "Online"

    @property
    def label(self) -> str:
        return self.name or self.computer or self.id

    @property
    def default_user(self) -> str:
        n = (self.platform_name or "").lower()
        if "ubuntu" in n:
            return "ubuntu"
        if "debian" in n:
            return "admin"
        if "centos" in n:
            return "centos"
        if "rocky" in n:
            return "rocky"
        if "suse" in n:
            return "ec2-user"
        return "ec2-user"


def parse_instance_information(data: dict) -> list[Instance]:
    out = []
    for it in data.get("InstanceInformationList", []) or []:
        iid = it.get("InstanceId", "")
        if not iid:
            continue
        out.append(Instance(iid, ping=it.get("PingStatus", ""), platform=it.get("PlatformType", ""),
                            platform_name=f"{it.get('PlatformName', '')} {it.get('PlatformVersion', '')}".strip(),
                            ip=it.get("IPAddress", ""), computer=it.get("ComputerName", "")))
    return out


def parse_describe_instances(data) -> dict[str, tuple[str, str]]:
    """{instance id: (Name tag, state)} from `ec2 describe-instances` JSON."""
    out = {}
    for res in (data or {}).get("Reservations", []) if isinstance(data, dict) else []:
        for inst in res.get("Instances", []):
            name = next((t.get("Value", "") for t in inst.get("Tags", []) or [] if t.get("Key") == "Name"), "")
            out[inst.get("InstanceId", "")] = (name, (inst.get("State") or {}).get("Name", ""))
    return out


def list_instances(profile: str = "", region: str = "") -> tuple[list[Instance], str]:
    """SSM-managed instances, named from their EC2 Name tag when allowed.
    Returns (instances, note) - note explains missing names (no ec2:DescribeInstances)."""
    infos = parse_instance_information(run(["ssm", "describe-instance-information"], profile, region, 120))
    note = ""
    ec2_ids = [i.id for i in infos if i.id.startswith("i-")]
    if ec2_ids:
        try:
            names = parse_describe_instances(run(["ec2", "describe-instances", "--instance-ids", *ec2_ids],
                                                 profile, region, 120))
            for i in infos:
                i.name, i.state = names.get(i.id, ("", ""))
        except LoginRequired:
            raise
        except AwsError as e:
            note = f"Names come from the OS host name (EC2 names unavailable: {e})"
    infos.sort(key=lambda i: (not i.online, i.label.lower()))
    return infos, note


def default_group(profile: str, region: str) -> str:
    return f"AWS/{profile or 'default'}/{region or 'default'}"


def import_instances(store, instances: list[Instance], profile: str = "", region: str = "",
                     mode: str = "ssm-ssh", user: str = "", eic: bool = True, group: str = "") -> tuple[int, int]:
    """Add instances to the vault as servers (or refresh ones already there, matched by
    instance id + profile; your own edits are kept). Returns (added, updated)."""
    from .models import Server
    group = group.strip().strip("/") or default_group(profile, region)
    by_target = {(s.host, s.aws_profile): s for s in store.servers.values() if s.is_ssm}
    added = updated = 0
    for inst in instances:
        existing = by_target.get((inst.id, profile))
        if existing:
            existing.name = existing.name or inst.label
            existing.aws_region = existing.aws_region or region
            updated += 1
            continue
        # Windows instances: Session Manager's PowerShell (no SSH server there by default)
        conn = "ssm-shell" if inst.platform == "Windows" else mode
        s = Server(name=inst.label, host=inst.id, username=user or inst.default_user, group=group,
                   connection=conn, aws_profile=profile, aws_region=region,
                   eic=eic and conn == "ssm-ssh" and inst.id.startswith("i-"),   # not for on-prem mi-
                   auth="agent", tags=["aws"],
                   notes=f"Imported from AWS ({inst.platform_name or inst.platform}, {inst.ip or 'no IP'}).")
        store.servers[s.id] = s
        store.groups.add(group)
        by_target[(inst.id, profile)] = s
        added += 1
    if added or updated:
        store.save()
    return added, updated


def is_instance_id(text: str) -> bool:
    return bool(INSTANCE_ID.match(text.strip()))


if __name__ == "__main__":     # tiny manual check: python -m blamixshell.aws [profile] [region]
    p = sys.argv[1] if len(sys.argv) > 1 else ""
    r = sys.argv[2] if len(sys.argv) > 2 else ""
    for inst in list_instances(p, r)[0]:
        print(inst)
