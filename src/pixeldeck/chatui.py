"""The Ask screen: chat with the local model, which searches every connected source for you."""

from __future__ import annotations

import subprocess
import time

from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import pixel as px
from .ask import SOURCE_ICON, AskSession, Turn, link_citations

ASK_CSS = f"""
AskScreen {{ background: {px.VOID}; }}
#ask-head {{ height: auto; background: {px.DEEP}; padding: 1 2 0 2; }}
#ask-rule {{ height: 1; }}
#ask-log {{ height: 1fr; background: {px.VOID}; padding: 0 2; scrollbar-color: {px.PURPLE}; scrollbar-size-vertical: 1; }}
#ask-log .turn {{ height: auto; margin-top: 1; max-width: 120; }}
#ask-log .q {{ height: auto; color: {px.INK}; }}
#ask-log .trace {{ height: auto; padding: 0 0 0 2; }}
#ask-log Markdown {{ background: {px.DEEP}; padding: 1 2 0 2; margin: 0 0 0 2; }}
#ask-log MarkdownParagraph {{ margin: 0 0 1 0; }}
#ask-log MarkdownBulletList, #ask-log MarkdownOrderedList {{ margin: 0 0 1 0; }}
#ask-log MarkdownH1, #ask-log MarkdownH2, #ask-log MarkdownH3 {{ color: {px.HOTPINK}; background: transparent;
    text-style: bold; margin: 0; }}
#ask-log MarkdownListItem MarkdownParagraph {{ margin: 0; }}
#ask-log MarkdownBullet {{ color: {px.HOTPINK}; }}
#ask-log MarkdownBlock > .code_inline {{ color: #cbb8ff; background: #221142; }}
#ask-log MarkdownBlock > .strong {{ color: {px.INK}; text-style: bold; }}
#ask-log MarkdownFence {{ background: #120628; border-left: outer {px.PURPLE}; margin: 1 0; }}
#ask-log .err {{ height: auto; padding: 0 0 0 2; }}
#ask-log .hello {{ height: auto; color: {px.DIM}; margin-top: 1; }}
#ask-src {{ height: auto; max-height: 10; background: {px.DEEP}; border: none; border-top: solid {px.SHADOW};
            border-title-color: {px.CYAN}; border-title-style: bold; padding: 0 1; display: none; }}
#ask-src.-on {{ display: block; }}
#ask-src > .option-list--option-highlighted {{ background: #3a1670; }}
#ask-src:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
#ask-q {{ background: {px.PANEL}; border: tall {px.PURPLE}; margin: 0 1; }}
#ask-q:focus {{ border: tall {px.HOTPINK}; }}
"""

HELLO = (
    "Ask anything about your work. The local model searches your dashboard, GitHub, mail, calendar, "
    "Slack and Linear, then answers with links to what it found.\n"
    "e.g.  what needs my attention today? · what did Sam say about the launch plan? · "
    "which PRs touch RBAC? · when am I meeting Dana? · what's on staging vs prod?\n"
    "Enter asks · ↓ sources · click [s3] to open a source · ctrl+n new chat · ctrl+y copy answer · "
    "esc stops an answer, then closes. Read-only: it never sends or changes anything."
)


class AskScreen(Screen[None]):
    CSS = ASK_CSS
    BINDINGS = [
        Binding("escape", "close", "Stop / close"),
        Binding("ctrl+n", "new", "New chat"),
        Binding("ctrl+y", "copy", "Copy answer"),
        Binding("ctrl+b", "browser", "Source in browser"),
        Binding("down", "to_sources", show=False),
    ]

    def __init__(self, session: AskSession) -> None:
        super().__init__()
        self.session = session
        self.busy = False
        self.started = 0.0
        self.stage = ""
        self.live: dict = {}  # widgets of the turn being answered
        self.buf = ""
        self.dirty = False
        self.shown_turn: Turn | None = None  # whose sources the list shows

    def compose(self) -> ComposeResult:
        from .app import Divider
        from .widgets import TextInput

        yield Static(id="ask-head")
        yield Divider(id="ask-rule")
        yield VerticalScroll(id="ask-log")
        yield OptionList(id="ask-src")
        yield TextInput(placeholder="ask about your PRs, mail, Slack, tickets, meetings, deploys…", id="ask-q",
                        select_on_focus=False)
        yield Footer()

    def on_mount(self) -> None:
        self._head()
        log = self.query_one("#ask-log", VerticalScroll)
        if not self.session.turns:
            log.mount(Static(Text(HELLO, Style(color=px.DIM)), classes="hello"))
        for t in self.session.turns:
            self._mount_turn(t)
        if self.session.turns:
            self._show_sources(self.session.turns[-1])
        log.scroll_end(animate=False)
        self.query_one("#ask-q").focus()
        self.set_interval(0.15, self._flush)
        self.set_interval(0.5, self._tick)
        if not self.session.model:
            self.run_worker(self._prepare(), group="ask-prep")

    async def _prepare(self) -> None:
        try:
            await self.session.prepare()
        except Exception:
            pass  # the first question reports it
        self._head()

    def _head(self) -> None:
        s = self.session
        t = Text()
        t.append(" ✦ ASK ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        t.append("  local model · read-only", Style(color=px.DIM))
        t.append("\n")
        t.append(s.model or "LM Studio", Style(color=px.INK, bold=True))
        t.append(f"  {s.ctx // 1024}K context", Style(color=px.DIM))
        t.append("   searches: ", Style(color=px.DIM))
        t.append(" · ".join(s.tools.sources()), Style(color=px.CYAN))
        self.query_one("#ask-head", Static).update(t)

    # transcript ---------------------------------------------------------------------

    def _mount_turn(self, turn: Turn, live: bool = False) -> dict:
        q = Static(Text.assemble(("▸ ", Style(color=px.HOTPINK, bold=True)), (turn.question, Style(color=px.INK, bold=True))),
                   classes="q")
        trace = Static(self._trace_text(turn), classes="trace")
        md = Markdown(self._answer_md(turn), open_links=False)
        md.display = bool(turn.answer) or live
        err = Static(self._err_text(turn), classes="err")
        err.display = bool(turn.error)
        box = Vertical(q, trace, md, err, classes="turn")
        self.query_one("#ask-log", VerticalScroll).mount(box)
        return {"box": box, "trace": trace, "md": md, "err": err, "turn": turn}

    def _answer_md(self, turn: Turn) -> str:
        return link_citations(turn.answer, self.session.tools.hits) if turn.answer else ""

    def _trace_text(self, turn: Turn, status: str = "") -> Text:
        out = Text()
        for line in turn.trace:
            color = px.RED if line.startswith("✖") else px.DIM
            out.append(line + "\n", Style(color=color))
        if status:
            out.append(status, Style(color=px.YELLOW))
        elif turn.answer and not turn.error:
            n = len(turn.cited) or len(turn.seen)
            what = f"{n} source{'s' * (n != 1)}" if n else "no sources"
            out.append(f"✔ {turn.secs:.0f}s · {what}", Style(color=px.DIM))
        if not status:
            out.rstrip()  # in place
        return out

    def _err_text(self, turn: Turn) -> Text:
        msg = turn.error or ""
        if "connect" in msg.lower() or "connection" in msg.lower():
            msg = (f"LM Studio isn't answering at {self.app.assist.url}. Start its server (Developer → Start Server), "
                   "then ask again.")
        elif msg == "stopped":
            return Text("■ stopped", Style(color=px.DIM))
        return Text(f"✖ {msg}", Style(color=px.RED))

    # asking --------------------------------------------------------------------------

    def on_input_submitted(self, e) -> None:
        if getattr(e.input, "id", None) != "ask-q":
            return
        q = e.value.strip()
        if not q:
            return
        if self.busy:
            self.notify("still answering · esc stops it", timeout=3)
            return
        e.input.value = ""
        for hello in self.query(".hello"):
            hello.remove()
        self.busy = True
        self.started = time.monotonic()
        self.stage = "thinking"
        self.buf = ""
        turn = Turn(q)
        self.live = self._mount_turn(turn, live=True)
        self.live["md"].display = False
        self.query_one("#ask-log", VerticalScroll).scroll_end(animate=False)
        self.run_worker(self._ask(q, turn), group="ask", exclusive=True, exit_on_error=False)

    async def _ask(self, q: str, placeholder: Turn) -> None:
        live = self.live

        def emit(kind: str, data: str) -> None:
            if kind == "think":
                if self.stage not in ("searching",):
                    self.stage = "thinking"
            elif kind == "tool":
                self.stage = "searching"
            elif kind == "traced":
                placeholder.trace.append(data)
            elif kind == "text":
                self.buf += data
                if self.buf.strip():  # models send a blank preamble before tool calls
                    self.stage = "writing"
                self.dirty = True
            elif kind == "reset":
                self.buf = ""
                self.dirty = True
            self._tick()

        try:
            turn = await self.session.ask(q, emit)
        except BaseException:  # cancelled (esc): the session already recorded nothing usable
            turn = self.session.turns[-1] if self.session.turns and self.session.turns[-1].question == q else placeholder
            turn.error = turn.error or "stopped"
            turn.secs = time.monotonic() - self.started
            self._finish(live, turn)
            raise
        self._finish(live, turn)

    def _finish(self, live: dict, turn: Turn) -> None:
        self.busy = False
        self.stage = ""
        self.buf = ""
        self.dirty = False
        live["turn"] = turn
        md: Markdown = live["md"]
        md.display = bool(turn.answer)
        md.update(self._answer_md(turn))
        live["trace"].update(self._trace_text(turn))
        live["err"].update(self._err_text(turn))
        live["err"].display = bool(turn.error)
        self._show_sources(turn)
        self.live = {}
        log = self.query_one("#ask-log", VerticalScroll)
        self.call_after_refresh(log.scroll_end, animate=False)
        self.set_timer(0.4, lambda: log.scroll_end(animate=False))  # after the Markdown has laid out

    def _flush(self) -> None:
        if not self.dirty or not self.live:
            return
        self.dirty = False
        md: Markdown = self.live["md"]
        md.display = bool(self.buf.strip())
        md.update(link_citations(self.buf, self.session.tools.hits))
        log = self.query_one("#ask-log", VerticalScroll)
        if log.max_scroll_y - log.scroll_y < 6:
            log.scroll_end(animate=False)

    def _tick(self) -> None:
        if not self.busy or not self.live:
            return
        secs = time.monotonic() - self.started
        label = {"thinking": "◷ thinking", "searching": "⌕ searching", "writing": "✎ writing"}.get(self.stage, "◷")
        self.live["trace"].update(self._trace_text(self.live["turn"], f"{label}… {secs:.0f}s"))

    # sources -------------------------------------------------------------------------

    def _show_sources(self, turn: Turn) -> None:
        self.shown_turn = turn
        hits = self.session.sources_for(turn)
        lst = self.query_one("#ask-src", OptionList)
        width = max(self.size.width - 6, 40)
        opts = []
        for h in hits:
            it = h.item
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(f"{h.id:>4} ", Style(color=px.HOTPINK, bold=True))
            t.append(f"{SOURCE_ICON.get(h.id[0], '▸')} {h.source:<9} ", Style(color=px.CYAN))
            t.append(it.title, Style(color=px.INK))
            if it.meta:
                t.append(f"  · {it.meta}", Style(color=px.DIM))
            t.truncate(width, overflow="ellipsis")
            opts.append(Option(t, id=h.id))
        lst.set_options(opts)
        lst.set_class(bool(opts), "-on")
        cited = "cited" if turn.cited else "found"
        lst.border_title = f" SOURCES · {len(opts)} {cited} · ↓ then Enter opens · ctrl+b in browser "
        if opts:
            lst.highlighted = 0

    def _open_hit(self, hid: str) -> None:
        hits = self.session.sources_for(self.shown_turn) if self.shown_turn else []
        if hid not in [h.id for h in hits]:
            # a citation from an older answer: show that answer's sources first
            for t in self.session.turns:
                if hid in t.cited or hid in t.seen:
                    self._show_sources(t)
                    hits = self.session.sources_for(t)
                    break
        if hid not in [h.id for h in hits]:
            hit = self.session.tools.hits.get(hid)
            if not hit:
                return
            hits = [hit]
        idx = [h.id for h in hits].index(hid)
        item = hits[idx].item
        kind = (item.ref or {}).get("kind")
        if kind == "slack_convo":
            ref = item.ref
            self.app.open_slack(ref.get("id"), ref.get("ts"), ref.get("thread_ts"))
            return
        from .detail import DetailScreen

        self.app.push_screen(DetailScreen([h.item for h in hits], idx, "ASK"))

    @on(OptionList.OptionSelected, "#ask-src")
    def _picked(self, e: OptionList.OptionSelected) -> None:
        self._open_hit(e.option.id)

    @on(Markdown.LinkClicked)
    def _link(self, e: Markdown.LinkClicked) -> None:
        href = e.href or ""
        if href.startswith("pixeldeck-hit:"):
            self._open_hit(href.split(":", 1)[1])
        elif href.startswith(("http://", "https://")):
            self.app.open_url(href)

    def action_to_sources(self) -> None:
        lst = self.query_one("#ask-src", OptionList)
        if self.focused is self.query_one("#ask-q") and lst.option_count:
            lst.focus()
            if lst.highlighted is None:
                lst.highlighted = 0

    def on_key(self, e: events.Key) -> None:
        lst = self.query_one("#ask-src", OptionList)
        if self.focused is not lst:
            return
        if e.key == "up" and (lst.highlighted or 0) == 0:
            self.query_one("#ask-q").focus()
            e.stop()
        elif e.is_printable and e.character:  # typing goes back to the question box
            q = self.query_one("#ask-q")
            q.focus()
            q.insert_text_at_cursor(e.character)
            e.stop()

    def action_browser(self) -> None:
        lst = self.query_one("#ask-src", OptionList)
        opt = lst.highlighted_option
        hit = self.session.tools.hits.get(opt.id) if opt else None
        if hit and hit.item.url:
            subprocess.Popen(["open", hit.item.url])

    # misc --------------------------------------------------------------------------------

    def action_copy(self) -> None:
        done = [t for t in self.session.turns if t.answer]
        if not done:
            return
        subprocess.run(["pbcopy"], input=done[-1].answer.encode())
        self.notify("answer copied", timeout=2)

    def action_new(self) -> None:
        if self.busy:
            self.workers.cancel_group(self, "ask")
        self.app.ask_session = AskSession(self.app)
        self.session = self.app.ask_session
        log = self.query_one("#ask-log", VerticalScroll)
        log.remove_children()
        log.mount(Static(Text(HELLO, Style(color=px.DIM)), classes="hello"))
        lst = self.query_one("#ask-src", OptionList)
        lst.clear_options()
        lst.remove_class("-on")
        self.shown_turn = None
        self.query_one("#ask-q").focus()
        self.run_worker(self._prepare(), group="ask-prep")

    def action_close(self) -> None:
        if self.busy:
            self.workers.cancel_group(self, "ask")
            return
        self.app.pop_screen()
