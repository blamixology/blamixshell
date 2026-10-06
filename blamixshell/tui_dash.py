"""Server dashboard for the terminal (Textual): the desktop dashboard's tabs, from the same readers.

Works over SSH on a headless box. Tabs: Overview, Services, Processes, Logs, Ports, Updates, Users, Cron,
Firewall, Docker, Timers, Storage, Security. Click a column header (or press 1-9 for the column) to sort,
`/` to filter, `a` / Enter for the actions of the selected row (each one asks first), `l` for its log,
`[` and `]` to change tab, `S` to give the sudo password, `p` to write a Markdown report, `r` to reload.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static, TabbedContent, TabPane

from . import collect
from .tablekeys import auto_key
from .tui import ConfirmScreen, PromptScreen

STYLE = {"ok": "green", "warn": "yellow", "bad": "bold red", "dim": "dim", "": ""}
AUTO_TABS = ("overview", "processes")          # reloaded every few seconds while open


class DataPane(Vertical):
    """One tab: a filter box, a sortable table and a line of notes."""

    DEFAULT_CSS = """
    DataPane { height: 1fr; }
    DataPane > Input { margin: 0; }
    DataPane > DataTable { height: 1fr; }
    DataPane > .note { height: auto; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, key: str):
        super().__init__(id=f"pane-{key}")
        self.key = key
        self.data: collect.Table | None = None
        self.filter_text = ""
        self.sort_col = -1
        self.sort_rev = False
        self.shown: list[int] = []            # shown row -> index in data.rows
        self.loaded = False

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter…   (/ to type here, Esc to clear)", classes="filter")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("Loading…", classes="note")

    # ---- content
    def show(self, table: collect.Table) -> None:
        self.data, self.loaded = table, True
        self.render_rows()

    def message(self, text: str) -> None:
        self.query_one(".note", Static).update(text)

    def render_rows(self) -> None:
        t = self.data
        dt = self.query_one(DataTable)
        if t is None:
            return
        keep = self.selected_index()
        q = self.filter_text.strip().lower()
        idx = [i for i, row in enumerate(t.rows) if not q or q in " ".join(row).lower()]
        if self.sort_col >= 0:
            idx.sort(key=lambda i: auto_key(t.rows[i][self.sort_col]) if self.sort_col < len(t.rows[i]) else (1, ""),
                     reverse=self.sort_rev)
        self.shown = idx
        dt.clear(columns=True)
        labels = [c + ("  ▼" if self.sort_rev else "  ▲") if n == self.sort_col else c
                  for n, c in enumerate(t.columns)]
        dt.add_columns(*labels)
        for i in idx:
            style = STYLE.get(t.styles[i], "")
            dt.add_row(*[Text(c, style=style) for c in t.rows[i]], key=str(i))
        if keep is not None and keep in idx:
            dt.move_cursor(row=idx.index(keep))
        count = f"{len(idx)} of {len(t.rows)}" if len(idx) != len(t.rows) else f"{len(t.rows)} rows"
        self.message("  ·  ".join(x for x in (t.note, count if t.rows else "") if x))

    def selected_index(self) -> int | None:
        dt = self.query_one(DataTable)
        if not self.shown or dt.row_count == 0 or not (0 <= dt.cursor_row < len(self.shown)):
            return None
        return self.shown[dt.cursor_row]

    def selected_key(self):
        i = self.selected_index()
        return self.data.keys[i] if self.data is not None and i is not None else None

    # ---- events
    @on(DataTable.HeaderSelected)
    def _sort(self, ev: DataTable.HeaderSelected) -> None:
        self.sort_by(ev.column_index)

    def sort_by(self, col: int) -> None:
        if self.data is None or not (0 <= col < len(self.data.columns)):
            return
        self.sort_rev = (not self.sort_rev) if col == self.sort_col else False
        self.sort_col = col
        self.render_rows()

    @on(Input.Changed)
    def _filter(self, ev: Input.Changed) -> None:
        self.filter_text = ev.value
        self.render_rows()


class ChoiceScreen(ModalScreen):
    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    ChoiceScreen > OptionList { width: 70; max-height: 18; border: round $accent; background: $panel; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, labels: list[str]):
        super().__init__()
        self.labels = labels

    def compose(self) -> ComposeResult:
        yield OptionList(*self.labels)

    @on(OptionList.OptionSelected)
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(ev.option_index)


class TextScreen(ModalScreen):
    DEFAULT_CSS = """
    TextScreen { align: center middle; }
    TextScreen > VerticalScroll { width: 92%; height: 88%; border: round $accent; background: $panel; padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "Close"), Binding("q", "dismiss(None)", "Close")]

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(Text(self.title_text + "\n", style="bold"))
            yield Static(Text(self.text.rstrip() or "(empty)"))


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Reload"),
        Binding("slash", "filter", "Filter"),
        Binding("a", "actions", "Actions"),
        Binding("l", "logs", "Log"),
        Binding("p", "report", "Report"),
        Binding("S", "sudo", "Sudo"),
        Binding("left_square_bracket", "tab(-1)", "◂ Tab", key_display="["),
        Binding("right_square_bracket", "tab(1)", "Tab ▸", key_display="]"),
        Binding("1", "sort(0)", "Sort 1", show=False), Binding("2", "sort(1)", "", show=False),
        Binding("3", "sort(2)", "", show=False), Binding("4", "sort(3)", "", show=False),
        Binding("5", "sort(4)", "", show=False), Binding("6", "sort(5)", "", show=False),
        Binding("7", "sort(6)", "", show=False),
    ]

    def __init__(self, server, ctx: collect.Context):
        super().__init__()
        self.server = server
        self.ctx = ctx
        self._loading: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            for key, title, _fn in collect.TABS:
                with TabPane(title, id=f"tab-{key}"):
                    yield DataPane(key)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Dashboard: {self.server.label}"
        self.sub_title = self.server.address
        self.set_interval(5, self._tick)
        self.load("overview")

    # ---- which tab
    def active_key(self) -> str:
        tabs = self.query_one(TabbedContent)
        return (tabs.active or "tab-overview").removeprefix("tab-")

    def pane(self, key: str | None = None) -> DataPane:
        return self.query_one(f"#pane-{key or self.active_key()}", DataPane)

    @on(TabbedContent.TabActivated)
    def _tab(self, ev: TabbedContent.TabActivated) -> None:
        key = (ev.pane.id or "").removeprefix("tab-")
        if key in collect.LOADERS and not self.pane(key).loaded:
            self.load(key)

    def action_tab(self, step: int) -> None:
        keys = [k for k, _t, _f in collect.TABS]
        i = (keys.index(self.active_key()) + step) % len(keys)
        self.query_one(TabbedContent).active = f"tab-{keys[i]}"

    # ---- loading
    def load(self, key: str, quiet: bool = False) -> None:
        if key in self._loading:
            return
        self._loading.add(key)
        if not quiet:
            self.pane(key).message("Loading…")
        self.run_worker(lambda: self._load_worker(key), thread=True, group=f"load-{key}")

    def _load_worker(self, key: str) -> None:
        try:
            table = collect.LOADERS[key](self.ctx)
        except collect.NeedsSudo:
            self.app.call_from_thread(self._needs_sudo, key)
            return
        except Exception as e:
            self.app.call_from_thread(self._failed, key, e)
            return
        self.app.call_from_thread(self._loaded, key, table)

    def _loaded(self, key: str, table: collect.Table) -> None:
        self._loading.discard(key)
        self.pane(key).show(table)
        if key == "overview" and table.note:
            self.sub_title = f"{self.server.address}  ·  {table.note}"

    def _failed(self, key: str, e: Exception) -> None:
        self._loading.discard(key)
        from .ssh_core import friendly_error
        self.pane(key).message(Text(f"Could not load: {friendly_error(e)}", style="red"))

    def _tick(self) -> None:
        key = self.active_key()
        if key in AUTO_TABS and not self.ctx_busy():
            self.load(key, quiet=True)

    def ctx_busy(self) -> bool:
        return bool(self._loading)

    def action_refresh(self) -> None:
        self.load(self.active_key())

    # ---- sudo
    def _needs_sudo(self, key: str | None) -> None:
        if key:
            self._loading.discard(key)
        self.ask_sudo(lambda: key and self.load(key))

    def ask_sudo(self, then=None) -> None:
        def got(pw: str | None) -> None:
            if pw:
                self.ctx.sudo_pw = pw
                if then:
                    then()
        self.app.push_screen(PromptScreen("sudo password", f"for {self.server.label}: used for this dashboard "
                                          "only, never saved", "password", password=True), got)

    def action_sudo(self) -> None:
        self.ask_sudo(lambda: self.load(self.active_key()))

    # ---- table keys
    def action_filter(self) -> None:
        self.pane().query_one(Input).focus()

    def action_sort(self, col: int) -> None:
        if not isinstance(self.focused, Input):
            self.pane().sort_by(col)

    def on_key(self, event) -> None:
        if event.key == "escape" and isinstance(self.focused, Input):
            inp = self.focused
            inp.value = ""
            self.pane().query_one(DataTable).focus()
            event.stop()

    # ---- actions on the selected row
    @on(DataTable.RowSelected)
    def _row(self, _ev: DataTable.RowSelected) -> None:
        self.action_actions()

    def action_actions(self) -> None:
        key = self.active_key()
        acts = collect.actions_for(key, self.pane(key).selected_key())
        if not acts:
            self.notify("Nothing to do with this row here." if key in ("services", "processes", "docker", "timers", "mounts")
                        else "This tab has no actions.", timeout=3)
            return

        def picked(i: int | None) -> None:
            if i is None:
                return
            act = acts[i]
            if act.prompt:                       # asks for a value first (minutes, a time zone, a size, a host …)
                self.app.push_screen(PromptScreen(act.label.rstrip("…"), act.prompt, act.placeholder),
                                     lambda value: value and self.do_action(act, key, value))
            else:
                self.do_action(act, key, "")
        self.app.push_screen(ChoiceScreen([a.label for a in acts]), picked)

    def do_action(self, act: collect.Action, key: str, value: str) -> None:
        try:
            cmd = act.command_for(value)
        except ValueError as e:
            self.notify(str(e), severity="error", timeout=6)
            return
        if act.readonly:                         # only looks: no confirmation, the output is shown
            self.show_output(act, cmd)
            return
        self.app.push_screen(
            ConfirmScreen(f"{act.label}\n\non {self.server.label}\n\nRuns: {cmd[:200]}",
                          yes="Run", variant="error" if act.danger else "primary"),
            lambda ok: ok and self.run_action(act, key, cmd))

    def show_output(self, act: collect.Action, cmd: str) -> None:
        self.notify(f"{act.label} …", timeout=3)

        def work() -> None:
            try:
                res = self.ctx.runner.run(cmd, timeout=60)
                text = (res.out + ("\n" + res.err if res.err.strip() else "")).strip()
            except Exception as e:
                text = f"Could not run it: {e}"
            self.app.call_from_thread(self.app.push_screen, TextScreen(act.label.rstrip("…"), text))
        self.run_worker(work, thread=True, group="readonly")

    def run_action(self, act: collect.Action, key: str, cmd: str | None = None) -> None:
        cmd = cmd or act.command
        self.notify(f"Running: {act.label} …", timeout=3)

        def work() -> None:
            try:
                res = self.ctx.run(cmd, allow_plain=act.allow_plain)
            except collect.NeedsSudo:
                self.app.call_from_thread(self.ask_sudo, lambda: self.run_action(act, key, cmd))
                return
            except Exception as e:
                self.app.call_from_thread(self.notify, f"{act.label}: {e}", severity="error")
                return
            self.app.call_from_thread(self._action_done, act, key, res)
        self.run_worker(work, thread=True, group="action")

    def _action_done(self, act: collect.Action, key: str, res) -> None:
        if res.ok:
            self.notify(f"Done: {act.label}", timeout=4)
        else:
            msg = (res.err or res.out).strip() or f"exit code {res.code}"
            if "incorrect password" in msg.lower() or "sorry, try again" in msg.lower():
                self.ctx.sudo_pw = None
                msg = "Wrong sudo password."
            self.notify(f"{act.label}: {msg[:300]}", severity="error", timeout=8)
        self.load(key)

    # ---- log of the selected row
    def action_logs(self) -> None:
        key = self.active_key()
        sel = self.pane(key).selected_key()
        cmd = collect.log_command(key, sel)
        if not cmd:
            self.notify("Select a service or a container on its tab first.", timeout=3)
            return
        title = f"Log: {sel[1] if key == 'services' else sel[2]}"

        def work() -> None:
            try:
                out = self.ctx.read(cmd, needs_root=lambda o: "denied" in o.lower())
            except collect.NeedsSudo:
                self.app.call_from_thread(self.ask_sudo, self.action_logs)
                return
            except Exception as e:
                out = f"Could not read it: {e}"
            self.app.call_from_thread(self.app.push_screen, TextScreen(title, out))
        self.run_worker(work, thread=True, group="logs")

    # ---- report
    def action_report(self) -> None:
        self.notify("Collecting the report …", timeout=3)

        def work() -> None:
            try:
                md = collect.build_report(self.server.label, self.server.address, self.ctx)
                safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in self.server.label).strip("-") or "server"
                path = Path.cwd() / f"report-{safe}-{date.today():%Y-%m-%d}.md"
                path.write_text(md, encoding="utf-8")
                self.app.call_from_thread(self.notify, f"Report saved: {path}", timeout=10)
            except Exception as e:
                self.app.call_from_thread(self.notify, f"Report failed: {e}", severity="error")
        self.run_worker(work, thread=True, group="report")

    def action_back(self) -> None:
        self.dismiss(None)


class DashApp(App):
    """`blamixshell dash <server>`: just the dashboard, full screen."""
    TITLE = "BlamixShell"

    def __init__(self, server, ctx: collect.Context):
        super().__init__()
        self.server, self.ctx = server, ctx

    def on_mount(self) -> None:
        try:
            self.theme = "tokyo-night"
        except Exception:
            pass
        self.push_screen(DashboardScreen(self.server, self.ctx), lambda _r: self.exit())


def run_dashboard(server, ctx: collect.Context) -> None:
    DashApp(server, ctx).run()

