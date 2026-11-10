"""Filter popup for LINEAR QUESTS: tick the statuses and priorities you want to see."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, SelectionList, Static

from . import pixel as px
from .config import get_value, set_value
from .sources.chat import PRIORITY_NAMES

FILTER_CSS = f"""
LinearFilter {{ align: center middle; background: {px.VOID} 70%; }}
LinearFilter > Vertical {{ width: 88; max-width: 95%; height: auto; max-height: 90%; background: {px.DEEP};
                           border: panel {px.HOTPINK}; border-title-color: {px.VOID}; border-title-style: bold;
                           padding: 1 2; }}
LinearFilter .cols {{ height: auto; }}
LinearFilter .col {{ width: 1fr; height: auto; }}
LinearFilter .col + .col {{ margin-left: 2; }}
LinearFilter .lbl {{ color: {px.CYAN}; text-style: bold; margin-top: 1; }}
LinearFilter SelectionList {{ height: auto; max-height: 14; background: {px.DEEP}; border: tall {px.PURPLE}; }}
LinearFilter SelectionList:focus {{ border: tall {px.HOTPINK}; }}
LinearFilter Checkbox {{ background: {px.DEEP}; border: none; }}
LinearFilter .buttons {{ height: auto; margin-top: 1; align-horizontal: right; }}
LinearFilter Button {{ margin-left: 1; min-width: 14; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
LinearFilter Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
"""


class LinearFilter(ModalScreen[bool]):
    CSS = FILTER_CSS.replace(".col + .col", "#lf-right")
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "apply", "Apply"),
        Binding("ctrl+a", "show_all", "Show all"),
    ]

    def __init__(self, facets: dict | None) -> None:
        super().__init__()
        self.facets = facets or {"states": {}, "priorities": {}}

    def compose(self) -> ComposeResult:
        hidden = {h.lower() for h in get_value("linear.hide_states") or []}
        prios = {int(p) for p in get_value("linear.priorities") or []}
        types = set(get_value("linear.states") or [])
        states = dict(self.facets.get("states") or {})
        for h in get_value("linear.hide_states") or []:  # hidden names still listed, so you can bring them back
            states.setdefault(h, {"type": "", "count": 0})
        order = {"started": 0, "triage": 1, "unstarted": 2, "backlog": 3}
        rows = sorted(states.items(), key=lambda kv: (order.get(kv[1]["type"], 9), -kv[1]["count"]))
        with Vertical() as box:
            box.border_title = " ⧩ FILTER LINEAR QUESTS "
            yield Static(Text("Ticked = shown. Space/Enter toggles · tab switches list · ctrl+s applies · ctrl+a shows everything · esc cancels",
                              Style(color=px.DIM)))
            with Horizontal(classes="cols"):
                with Vertical(classes="col"):
                    yield Label("STATUS", classes="lbl")
                    yield SelectionList(*[
                        (Text.assemble((name, Style(color=px.INK, bold=True)), (f"  {info['count']}", Style(color=px.DIM))),
                         name, name.lower() not in hidden) for name, info in rows], id="lf-states")
                with Vertical(classes="col", id="lf-right"):
                    yield Label("PRIORITY", classes="lbl")
                    counts = self.facets.get("priorities") or {}
                    yield SelectionList(*[
                        (Text.assemble((label, Style(color=px.INK, bold=True)),
                                       (f"  {counts.get(p, counts.get(str(p), 0))}", Style(color=px.DIM))),
                         p, not prios or p in prios) for p, label in PRIORITY_NAMES], id="lf-prio")
                    yield Label("ALSO FETCH", classes="lbl")
                    yield Checkbox("triage", value="triage" in types, name="triage")
                    yield Checkbox("backlog (long)", value="backlog" in types, name="backlog")
            with Horizontal(classes="buttons"):
                yield Button("SHOW ALL", id="lf-all")
                yield Button("CANCEL", id="lf-cancel")
                yield Button("APPLY", id="lf-apply", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#lf-states", SelectionList).focus()

    @on(Button.Pressed, "#lf-apply")
    def action_apply(self) -> None:
        states = self.query_one("#lf-states", SelectionList)
        shown = set(states.selected)
        all_names = {states.get_option_at_index(i).value for i in range(states.option_count)}
        hide = sorted(all_names - shown)
        prio = self.query_one("#lf-prio", SelectionList)
        keep = sorted(prio.selected)
        types = ["started", "unstarted"] + [cb.name for cb in self.query(Checkbox) if cb.value]
        set_value("linear.hide_states", hide)
        set_value("linear.priorities", [] if len(keep) == prio.option_count else keep)
        set_value("linear.states", types)
        self.dismiss(True)

    @on(Button.Pressed, "#lf-all")
    def action_show_all(self) -> None:
        self.query_one("#lf-states", SelectionList).select_all()
        self.query_one("#lf-prio", SelectionList).select_all()

    @on(Button.Pressed, "#lf-cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


# ── search ───────────────────────────────────────────────────────────────────

SEARCH_CSS = f"""
LinearSearch {{ align: center top; background: {px.VOID} 70%; }}
LinearSearch > Vertical {{ width: 120; max-width: 96%; height: 90%; margin-top: 2; background: {px.DEEP};
                           border: panel {px.HOTPINK}; border-title-color: {px.VOID}; border-title-style: bold;
                           padding: 1 2; }}
LinearSearch #ls-q {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
LinearSearch #ls-q:focus {{ border: tall {px.HOTPINK}; }}
LinearSearch .toggles {{ height: auto; margin-top: 1; }}
LinearSearch Checkbox {{ background: {px.DEEP}; border: none; margin-right: 2; }}
LinearSearch #ls-status {{ height: 1; margin: 1 0 0 1; }}
LinearSearch #ls-results {{ height: 1fr; background: {px.DEEP}; border: none; margin-top: 1; }}
LinearSearch #ls-results > .option-list--option-highlighted {{ background: #3a1670; }}
LinearSearch #ls-results:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
LinearSearch #ls-help {{ height: 1; }}
"""


class LinearSearch(ModalScreen[None]):
    """Search every Linear ticket (Linear's own search): type, then ↓ and Enter to open one."""

    CSS = SEARCH_CSS
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+o", "toggle('open')", "Open only"),
        Binding("ctrl+t", "toggle('mine')", "Assigned to me"),
        Binding("down", "to_results", show=False),
        Binding("ctrl+b", "browser", "Open in Linear"),
        Binding("ctrl+y", "copy", "Copy link"),
    ]
    DEBOUNCE = 0.35

    def __init__(self, query: str = "", open_only: bool = False, mine: bool = False) -> None:
        super().__init__()
        self.query_text, self.open_only, self.mine = query, open_only, mine
        self.items: list = []
        self._timer = None

    def compose(self) -> ComposeResult:
        from .widgets import TextInput
        from textual.widgets import OptionList

        with Vertical() as box:
            box.border_title = " ⌕ SEARCH LINEAR "
            yield TextInput(value=self.query_text, placeholder="words, or a ticket id like ABC-123", id="ls-q",
                            select_on_focus=False)
            with Horizontal(classes="toggles"):
                yield Checkbox("open only  ^o", value=self.open_only, id="ls-open")
                yield Checkbox("assigned to me  ^t", value=self.mine, id="ls-mine")
            yield Static("", id="ls-status")
            yield OptionList(id="ls-results")
            yield Static(Text("↓ results · Enter opens it here · ^b in Linear · ^y copy link · esc closes",
                              Style(color=px.DIM)),
                         id="ls-help")

    def on_mount(self) -> None:
        q = self.query_one("#ls-q")
        q.focus()
        q.cursor_position = len(q.value)
        if self.query_text.strip():
            self._search()

    # searching ---------------------------------------------------------------

    def _status(self, text: str, color: str = px.DIM) -> None:
        self.query_one("#ls-status", Static).update(Text(text, Style(color=color)))

    def on_input_changed(self, e) -> None:
        if getattr(e.input, "id", None) != "ls-q":
            return
        self.query_text = e.value
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(self.DEBOUNCE, self._search)

    def on_input_submitted(self, e) -> None:
        self.action_to_results()

    @on(Checkbox.Changed)
    def _toggled(self, e: Checkbox.Changed) -> None:
        if e.checkbox.id == "ls-open":
            self.open_only = e.value
        elif e.checkbox.id == "ls-mine":
            self.mine = e.value
        self._search()

    def action_toggle(self, which: str) -> None:
        cb = self.query_one("#ls-open" if which == "open" else "#ls-mine", Checkbox)
        cb.value = not cb.value

    def _search(self) -> None:
        term = self.query_text.strip()
        if len(term) < 2:
            self.items = []
            self.query_one("#ls-results").clear_options()
            self._status("type at least 2 characters")
            return
        self._status("⌕ searching…", px.YELLOW)
        self.run_worker(self._run(term, self.open_only, self.mine), group="search", exclusive=True)

    async def _run(self, term: str, open_only: bool, mine: bool) -> None:
        from textual.widgets.option_list import Option

        try:
            items, total = await self.app.linear.search(term, open_only=open_only, mine=mine)
        except Exception as exc:
            self._status(f"✖ {exc}"[:160], px.RED)
            return
        if term != self.query_text.strip() or (open_only, mine) != (self.open_only, self.mine):
            return  # the search changed while this one was out
        self.items = items
        lst = self.query_one("#ls-results")
        width = max(lst.size.width - 4, 40)
        opts = []
        for i, it in enumerate(items):
            top = Text(no_wrap=True, overflow="ellipsis")
            top.append(f"{it.icon} ", Style(color=px.TONE.get(it.tone, px.INK), bold=True))
            ident, _, title = it.title.partition(" ")
            top.append(ident, Style(color=px.CYAN, bold=True))
            top.append(" " + title, Style(color=px.DIM if it.tone == "muted" else px.INK))
            top.truncate(width, overflow="ellipsis")
            sub = Text(f"  {it.meta}", Style(color=px.DIM), no_wrap=True)
            sub.truncate(width, overflow="ellipsis")
            opts.append(Option(Text("\n").join([top, sub]), id=str(i)))
        lst.set_options(opts)
        if items:
            lst.highlighted = 0
        toggles = [t for t, on_ in (("open only", open_only), ("assigned to me", mine)) if on_]
        shown = f"{len(items)} shown" + (f" ({', '.join(toggles)})" if toggles else "")
        more = f" · Linear found {total}, refine to narrow it" if total > len(items) and not toggles else ""
        self._status(f"{shown}{more}" if items else f"nothing matches “{term}”" + (f" ({', '.join(toggles)})" if toggles else ""),
                     px.CYAN if items else px.YELLOW)

    # results ----------------------------------------------------------------

    def action_to_results(self) -> None:
        lst = self.query_one("#ls-results")
        if self.items and not lst.has_focus:
            lst.focus()
            if lst.highlighted is None:
                lst.highlighted = 0

    def on_key(self, event) -> None:
        # ↑ on the first result goes back to the search box
        lst = self.query_one("#ls-results")
        if event.key == "up" and lst.has_focus and (lst.highlighted or 0) == 0:
            self.query_one("#ls-q").focus()
            event.stop()
        elif lst.has_focus and len(event.key) == 1 and event.key.isprintable() and event.key not in "jk":
            # typing while in the list goes to the search box
            q = self.query_one("#ls-q")
            q.focus()
            q.insert_text_at_cursor(event.key)
            event.stop()

    def on_option_list_option_selected(self, e) -> None:
        if e.option_list.id != "ls-results":
            return
        from .detail import DetailScreen

        def back(index: int | None) -> None:
            if index is not None and index < len(self.items):
                self.query_one("#ls-results").highlighted = index

        self.app.push_screen(DetailScreen(self.items, int(e.option.id), "LINEAR SEARCH"), back)

    def _current(self):
        lst = self.query_one("#ls-results")
        i = lst.highlighted
        return self.items[i] if self.items and i is not None and i < len(self.items) else None

    def action_browser(self) -> None:
        if (it := self._current()) and it.url:
            self.app.open_url(it.url)

    def action_copy(self) -> None:
        import subprocess

        if (it := self._current()) and it.url:
            subprocess.run(["pbcopy"], input=it.url.encode())
            self.notify(it.url, title="COPIED", timeout=3)

    def action_close(self) -> None:
        self.app.linear_search_state = (self.query_text, self.open_only, self.mine)
        self.dismiss(None)
