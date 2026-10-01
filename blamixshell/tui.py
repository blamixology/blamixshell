"""Full-screen terminal UI (Textual). Works over SSH on any Linux box or macOS.

Browse/search servers, connect (the TUI steps aside and gives you the raw shell),
add/edit/delete, favorites, and run a command on a whole group in parallel.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, Tree

from .models import Server, Store

ACCENT = "#7c8cff"
NO_SELECTION = getattr(Select, "NULL", Select.BLANK)   # Textual renamed the sentinel


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    return ("just now" if d < 90 else f"{int(d // 60)} min ago" if d < 3600
            else f"{int(d // 3600)} h ago" if d < 86400 else f"{int(d // 86400)} days ago")


# ============================================================== modals
class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 60; height: auto; padding: 1 2; background: $panel; border: round $error; }
    #row { height: auto; margin-top: 1; align-horizontal: right; }
    #row Button { margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.message)
            with Horizontal(id="row"):
                yield Button("Cancel", id="no")
                yield Button("Delete", variant="error", id="yes")

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")


class PromptScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    #box { width: 80; height: auto; padding: 1 2; background: $panel; border: round $accent; }
    #hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, hint: str = "", placeholder: str = ""):
        super().__init__()
        self.title_text, self.hint, self.placeholder = title, hint, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(Text(self.title_text, style="bold"))
            yield Input(placeholder=self.placeholder, id="value")
            if self.hint:
                yield Label(self.hint, id="hint")

    @on(Input.Submitted)
    def _submit(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)


class ServerForm(ModalScreen[Server | None]):
    DEFAULT_CSS = """
    ServerForm { align: center middle; }
    #form { width: 84; height: auto; max-height: 90%; padding: 1 2; background: $panel;
            border: round $accent; }
    .row { height: auto; }
    .row Label { width: 14; padding-top: 1; color: $text-muted; }
    .row Input, .row Select { width: 1fr; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 1; }
    #err { color: $error; height: auto; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("ctrl+s", "save", "Save")]

    def __init__(self, store: Store, server: Server | None = None, group: str = ""):
        super().__init__()
        self.store = store
        self.server = server.copy() if server else Server(group=group)
        self.is_new = server is None

    def _row(self, label: str, widget) -> Horizontal:
        return Horizontal(Label(label), widget, classes="row")

    def compose(self) -> ComposeResult:
        s = self.server
        jumps = [(f"{o.label} ({o.address})", o.id) for o in
                 sorted(self.store.servers.values(), key=lambda x: x.label.lower()) if o.id != s.id]
        with VerticalScroll(id="form"):
            yield Label(Text("New server" if self.is_new else f"Edit {s.label}", style=f"bold {ACCENT}"))
            yield self._row("Name", Input(s.name, placeholder="api-prod-1", id="name"))
            yield self._row("Connect via", Select([("SSH", "ssh"), ("SSH over AWS SSM", "ssm-ssh"),
                                                   ("AWS SSM shell (no SSH)", "ssm-shell")],
                                                  value=s.connection, allow_blank=False, id="via"))
            yield self._row("AWS profile", Input(s.aws_profile, placeholder="default", id="aws_profile"))
            yield self._row("AWS region", Input(s.aws_region, placeholder="profile's region", id="aws_region"))
            yield self._row("Instance Connect", Select([("No", "no"), ("Yes: one-time key per connection", "yes")],
                                                       value="yes" if s.eic else "no", allow_blank=False, id="eic"))
            yield self._row("Host", Input(s.host, placeholder="host or IP (user@host:port ok) · i-… for SSM",
                                          id="host"))
            yield self._row("Port", Input(str(s.port), id="port", type="integer"))
            yield self._row("Username", Input(s.username, id="user"))
            yield self._row("Auth", Select([("Password", "password"), ("Private key", "key"),
                                            ("SSH agent", "agent")], value=s.auth, allow_blank=False, id="auth"))
            yield self._row("Password", Input(s.password, password=True,
                                              placeholder="empty = ask when connecting", id="password"))
            yield self._row("Key file", Input(s.key_path, placeholder="~/.ssh/id_ed25519", id="key"))
            yield self._row("Passphrase", Input(s.passphrase, password=True, id="passphrase"))
            yield self._row("Group", Input(s.group, placeholder="Prod/EU", id="group"))
            yield self._row("Tags", Input(", ".join(s.tags), placeholder="prod, db", id="tags"))
            yield self._row("Jump host", Select(jumps, value=s.jump_id if s.jump_id else NO_SELECTION,
                                                prompt="None (direct)", id="jump"))
            yield self._row("On connect", Input(s.startup_cmd, placeholder="tmux attach || tmux", id="startup"))
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#name" if self.is_new else "#host", Input).focus()

    def _val(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def action_save(self) -> None:
        s = self.server
        host = self._val("host")
        via = str(self.query_one("#via", Select).value)
        if "@" in host and via == "ssh":
            u, host = host.rsplit("@", 1)
            if not self._val("user"):
                self.query_one("#user", Input).value = u
        if host.count(":") == 1 and via == "ssh":
            host, p = host.split(":")
            if p.isdigit():
                self.query_one("#port", Input).value = p
        if not host:
            self.query_one("#err", Label).update("Host is required.")
            return
        s.host = host
        s.name = self._val("name")
        try:
            s.port = int(self._val("port") or 22)
        except ValueError:
            s.port = 22
        s.username = self._val("user")
        s.auth = self.query_one("#auth", Select).value
        s.password = self.query_one("#password", Input).value
        s.key_path = self._val("key")
        s.passphrase = self.query_one("#passphrase", Input).value
        s.group = self._val("group").strip("/")
        s.tags = [t.strip() for t in self._val("tags").split(",") if t.strip()]
        j = self.query_one("#jump", Select).value
        s.jump_id = "" if j in (NO_SELECTION, Select.BLANK, None) else str(j)
        s.startup_cmd = self._val("startup")
        s.connection = via
        s.aws_profile = "" if self._val("aws_profile") == "default" else self._val("aws_profile")
        s.aws_region = self._val("aws_region")
        s.eic = self.query_one("#eic", Select).value == "yes"
        if s.is_ssm:
            s.jump_id = ""
        self.dismiss(s)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


# ============================================================== app
class BlamixShellTUI(App):
    TITLE = "BlamixShell"
    SUB_TITLE = "SSH server manager"
    CSS = """
    Screen { background: $background; }
    #search { margin: 1 1 0 1; border: round $panel-lighten-2; }
    #search:focus { border: round $accent; }
    #main { height: 1fr; }
    #tree { width: 58%; min-width: 34; margin: 0 0 0 1; border: round $panel-lighten-2;
            background: $surface; padding: 0 1; }
    #tree:focus { border: round $accent; }
    #details { width: 1fr; margin: 0 1; border: round $panel-lighten-2; padding: 1 2;
               background: $surface; }
    """
    BINDINGS = [
        Binding("enter", "connect", "Connect", show=True, priority=False),
        Binding("slash", "search", "Search"),
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("f", "favorite", "Fav"),
        Binding("x", "exec", "Run on group"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.query_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search servers…   (tag:prod, group names, hosts)", id="search")
        with Horizontal(id="main"):
            yield Tree("Servers", id="tree")
            yield Static(id="details")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.theme = "tokyo-night"
        except Exception:
            pass
        tree = self.query_one(Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self.rebuild()
        tree.focus()

    # ---------------------------------------------------------- tree
    def rebuild(self, select_id: str | None = None) -> None:
        tree = self.query_one(Tree)
        tree.clear()
        q = self.query_text
        servers = sorted((s for s in self.store.servers.values() if s.matches(q)),
                         key=lambda s: s.label.lower())

        def label(s: Server) -> Text:
            t = Text()
            t.append("★ " if s.favorite else "  ", style="yellow")
            t.append(s.label, style=f"bold {s.color}" if s.color else "bold")
            t.append(f"  {s.address}", style="dim")
            if s.tags:
                t.append("  " + " ".join("#" + x for x in s.tags), style=ACCENT)
            return t

        target_node = None
        favs = [s for s in servers if s.favorite]
        if favs:
            fnode = tree.root.add(Text("★ Favorites", style="bold yellow"), data=("group", "__favs__"), expand=True)
            for s in favs:
                n = fnode.add_leaf(label(s), data=("server", s.id))
                target_node = target_node or (n if s.id == select_id else None)
        groups: dict[str, object] = {}

        def gnode(path: str):
            if not path:
                return tree.root
            if path in groups:
                return groups[path]
            parent, _, name = path.rpartition("/")
            n = gnode(parent).add(Text(name, style="bold"), data=("group", path), expand=True)
            groups[path] = n
            return n

        if not q:
            for g in self.store.all_groups():
                gnode(g)
        for s in servers:
            n = gnode(s.group).add_leaf(label(s), data=("server", s.id))
            if s.id == select_id and not s.favorite:
                target_node = n
        tree.root.expand()
        if target_node is not None:
            tree.move_cursor(target_node)
        self.show_details()

    def selected(self) -> tuple[str, str] | None:
        node = self.query_one(Tree).cursor_node
        return node.data if node is not None and node.data else None

    def selected_server(self) -> Server | None:
        sel = self.selected()
        return self.store.servers.get(sel[1]) if sel and sel[0] == "server" else None

    @on(Tree.NodeHighlighted)
    def _highlight(self) -> None:
        self.show_details()

    @on(Tree.NodeSelected)
    def _selected(self, ev: Tree.NodeSelected) -> None:
        if ev.node.data and ev.node.data[0] == "server":
            self.action_connect()

    def show_details(self) -> None:
        panel = self.query_one("#details", Static)
        s = self.selected_server()
        sel = self.selected()
        if s:
            t = Text()
            t.append(s.label + "\n", style=f"bold {s.color or ACCENT}")
            t.append(s.address + "\n\n", style="dim")
            rows = [("Group", s.group or "—"), ("Tags", ", ".join(s.tags) or "—"),
                    ("Auth", {"password": "password" + (" (saved)" if s.password else " (ask)"),
                              "key": f"key {s.key_path or '(pasted)'}", "agent": "SSH agent"}[s.auth]),
                    ("Jump host", self.store.servers[s.jump_id].label if s.jump_id in self.store.servers else "—"),
                    ("On connect", s.startup_cmd or "—"),
                    ("Last used", _ago(s.last_connected)), ("Sessions", str(s.connect_count))]
            for k, v in rows:
                t.append(f"{k:<12}", style="dim")
                t.append(f"{v}\n")
            if s.notes:
                t.append("\n" + s.notes, style="italic")
            t.append("\n\n⏎ connect   e edit   x run command   f favorite", style="dim")
            panel.update(t)
        elif sel and sel[0] == "group":
            path = sel[1]
            members = ([x for x in self.store.servers.values() if x.favorite] if path == "__favs__" else
                       [x for x in self.store.servers.values() if x.group == path or x.group.startswith(path + "/")])
            t = Text()
            t.append(("Favorites" if path == "__favs__" else path) + "\n", style=f"bold {ACCENT}")
            t.append(f"{len(members)} server(s)\n\n", style="dim")
            for m in sorted(members, key=lambda x: x.label.lower())[:20]:
                t.append(f"• {m.label}", style="bold")
                t.append(f"  {m.address}\n", style="dim")
            t.append("\nx  run a command on all of them", style="dim")
            panel.update(t)
        else:
            n = len(self.store.servers)
            panel.update(Text.assemble(("BlamixShell\n", f"bold {ACCENT}"),
                                       (f"{n} server(s) in your vault\n\n", "dim"),
                                       ("a  add a server\n/  search\nq  quit", "")))

    # ---------------------------------------------------------- search
    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        inp = self.query_one("#search", Input)
        if inp.value:
            inp.value = ""
        self.query_one(Tree).focus()

    @on(Input.Changed, "#search")
    def _search(self, ev: Input.Changed) -> None:
        self.query_text = ev.value
        self.rebuild()

    @on(Input.Submitted, "#search")
    def _search_go(self) -> None:
        tree = self.query_one(Tree)
        tree.focus()
        # jump to the first server match
        for node in tree.root.children:
            stack = [node]
            while stack:
                n = stack.pop(0)
                if n.data and n.data[0] == "server":
                    tree.move_cursor(n)
                    return
                stack.extend(n.children)

    # ---------------------------------------------------------- actions
    def action_connect(self) -> None:
        if self.focused is not None and self.focused.id == "search":
            return
        s = self.selected_server()
        if not s:
            node = self.query_one(Tree).cursor_node
            if node is not None:
                node.toggle()
            return
        from .cli import interactive_shell
        with self.suspend():
            try:
                interactive_shell(s, self.store)
            except SystemExit:
                input("\nPress Enter to return to BlamixShell…")
        self.rebuild(select_id=s.id)

    def action_add(self) -> None:
        group = ""
        sel = self.selected()
        if sel and sel[0] == "group" and sel[1] != "__favs__":
            group = sel[1]
        elif self.selected_server():
            group = self.selected_server().group

        def done(s: Server | None) -> None:
            if s:
                self.store.upsert(s)
                self.rebuild(select_id=s.id)
                self.notify(f"Saved {s.label}")
        self.push_screen(ServerForm(self.store, group=group), done)

    def action_edit(self) -> None:
        s = self.selected_server()
        if not s:
            return

        def done(new: Server | None) -> None:
            if new:
                self.store.upsert(new)
                self.rebuild(select_id=new.id)
                self.notify(f"Saved {new.label}")
        self.push_screen(ServerForm(self.store, s), done)

    def action_delete(self) -> None:
        s = self.selected_server()
        if not s:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.store.delete(s.id)
                self.rebuild()
                self.notify(f"Deleted {s.label}", severity="warning")
        self.push_screen(ConfirmScreen(f"Delete {s.label} ({s.address}) from the vault?"), done)

    def action_favorite(self) -> None:
        s = self.selected_server()
        if s:
            s.favorite = not s.favorite
            self.store.upsert(s)
            self.rebuild(select_id=s.id)

    def action_exec(self) -> None:
        sel = self.selected()
        if not sel:
            return
        if sel[0] == "server":
            targets = [self.store.servers[sel[1]]]
        elif sel[1] == "__favs__":
            targets = [s for s in self.store.servers.values() if s.favorite]
        else:
            targets = [s for s in self.store.servers.values()
                       if s.group == sel[1] or s.group.startswith(sel[1] + "/")]
        if not targets:
            return
        names = ", ".join(t.label for t in targets[:6]) + ("…" if len(targets) > 6 else "")

        def run(cmd: str | None) -> None:
            if not cmd:
                return
            from .cli import run_many
            with self.suspend():
                print(f"\n$ {cmd}   on {len(targets)} server(s)\n")
                try:
                    run_many(self.store, sorted(targets, key=lambda x: x.label.lower()), cmd)
                except SystemExit:
                    pass
                input("\nPress Enter to return to BlamixShell…")
            self.rebuild()
        self.push_screen(PromptScreen(f"Run a command on {len(targets)} server(s)",
                                      f"Targets: {names}", "uptime"), run)


def run_tui() -> int:
    from .cli import unlock
    store = unlock()
    BlamixShellTUI(store).run()
    return 0
