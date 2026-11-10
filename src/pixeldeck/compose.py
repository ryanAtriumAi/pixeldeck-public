"""Reply / reply-all to an email from its detail page."""

from __future__ import annotations

import subprocess
from urllib.parse import quote

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, TextArea

from .widgets import TextBox

from . import pixel as px
from .setup import SETUP_CSS, _bad, _busy, _ok

COMPOSE_CSS = f"""
ComposeScreen {{ align: center middle; background: {px.VOID} 70%; }}
ComposeScreen .box {{ width: 110; height: 85%; }}
#c-head {{ height: auto; margin-bottom: 1; }}
#c-body {{ height: 1fr; min-height: 6; background: {px.PANEL}; border: tall {px.PURPLE}; }}
#c-body:focus {{ border: tall {px.HOTPINK}; }}
#send {{ min-width: 24; }}
#c-body > .text-area--suggestion {{ color: {px.HOTPINK} 55%; }}
#c-quote {{ height: 7; color: {px.DIM}; background: {px.DEEP}; padding: 0 1; overflow-y: auto; }}
"""


class GhostTextArea(TextBox):
    """TextArea whose suggestion can span lines: it shows the first line and → inserts all of it."""

    full_suggestion = ""

    def show_suggestion(self, text: str) -> None:
        first, _, rest = text.partition("\n")
        self.full_suggestion = text
        self.suggestion = first + (" …" if rest.strip() else "") if first else ("⏎ " + rest.strip().splitlines()[0][:60] + " …")

    def action_cursor_right(self, select: bool = False) -> None:
        if self.suggestion and self.full_suggestion and not select:
            text, self.full_suggestion = self.full_suggestion, ""
            self.suggestion = ""
            self.insert(text)
            return
        super().action_cursor_right(select)


def _subject(s: str) -> str:
    return s if s.lower().startswith(("re:", "re ")) else f"RE: {s}"


class ComposeScreen(ModalScreen[bool]):
    CSS = SETUP_CSS + COMPOSE_CSS
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "send", "Send"),
    ]

    def _finish(self) -> None:
        """Close from a timer: call dismiss without returning its awaitable."""
        if self.is_current:
            self.dismiss(True)

    def __init__(self, reply: dict, reply_all: bool, draft: str | None = None) -> None:
        super().__init__()
        self.r = reply
        self.all = reply_all
        self.draft = draft  # text the agent proposed: you read it, edit it, and still press SEND
        self.armed = False
        self._suggest_timer = None

    def _recipients(self) -> tuple[list[str], list[str]]:
        sender = self.r.get("from") or ""
        skip = {sender.lower(), (self.r.get("me") or "").lower(), ""}
        to = [sender] if sender else []
        cc: list[str] = []
        if self.all:
            to += [a for a in self.r.get("to", []) if a.lower() not in skip]
            cc = [a for a in self.r.get("cc", []) if a.lower() not in skip]
        return to, cc

    def compose(self) -> ComposeResult:
        to, cc = self._recipients()
        via = self.r["via"]
        with Vertical(classes="box") as box:
            box.border_title = f" ✉ {'REPLY ALL' if self.all else 'REPLY'} "
            head = Text()
            head.append(" TO ", Style(color=px.VOID, bgcolor=px.CYAN, bold=True))
            head.append(f" {', '.join(to) or '?'}", Style(color=px.INK))
            if self.all and via == "graph":
                head.append("   (Outlook works out the full reply-all list)", Style(color=px.DIM))
            if cc:
                head.append("\n")
                head.append(" CC ", Style(color=px.VOID, bgcolor=px.PURPLE, bold=True))
                head.append(f" {', '.join(cc)}", Style(color=px.INK))
            head.append("\n")
            head.append(" RE ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
            head.append(f" {_subject(self.r.get('subject', ''))}", Style(color=px.INK, bold=True))
            if via != "graph":
                head.append("\n\n")
                head.append(
                    "This inbox can't send from here: SEND opens a reply in "
                    + ("Mail.app" if via == "apple" else "your mail app")
                    + " with your text on the clipboard.",
                    Style(color=px.YELLOW),
                )
            if getattr(self.app, "assist", None) and self.app.assist.enabled:
                head.append("\n")
                head.append(" → ", Style(color=px.VOID, bgcolor=px.PURPLE, bold=True))
                head.append(" takes the pink suggestion (your local model, your tone)", Style(color=px.DIM))
            yield Static(head, id="c-head")
            yield GhostTextArea(id="c-body", soft_wrap=True, placeholder="write your reply…")
            yield Label("ORIGINAL", classes="field-label")
            yield Static(Text(self.r.get("quote", "").strip() or "(empty)"), id="c-quote")
            yield Static("", id="status", classes="status")
            with Horizontal(classes="buttons"):
                if self.r.get("web"):
                    yield Button("EDIT IN OUTLOOK ↗", id="web")
                yield Button("CANCEL", id="cancel")
                yield Button("SEND  ctrl+s", id="send", variant="primary")

    def on_mount(self) -> None:
        self.call_after_refresh(self._start)  # when pushed from a callback the body mounts a beat later

    def _start(self) -> None:
        ta = self.query_one("#c-body", TextArea)
        ta.focus()
        if self.draft:
            ta.load_text(self.draft)
            ta.move_cursor(ta.document.end)
            return
        self._ask_soon(0.1)

    # suggestions ---------------------------------------------------------------

    @on(TextArea.Changed, "#c-body")
    def _typed(self) -> None:
        ta = self.query_one("#c-body", TextArea)
        if not ta.text and not ta.suggestion:
            ta.placeholder = "write your reply…"
        self._ask_soon()

    def _ask_soon(self, delay: float = 0.4) -> None:
        assist = getattr(self.app, "assist", None)
        if not assist or not assist.enabled:
            return
        if self._suggest_timer:
            self._suggest_timer.stop()
        self._suggest_timer = self.set_timer(delay, lambda: self.run_worker(self._suggest(), exclusive=True,
                                                                              group="suggest"))

    async def _suggest(self) -> None:
        ta = self.query_one("#c-body", TextArea)
        draft = ta.text
        if ta.cursor_location != ta.document.end:
            return  # only complete at the end of what you've written
        to, _ = self._recipients()
        context = (f"Email you are replying to.\nFrom: {self.r.get('from', '?')}\nSubject: {self.r.get('subject', '')}\n"
                   f"Body:\n{self.r.get('quote', '')[:2500]}\n\nReply goes to: {', '.join(to)}")
        tail = await self.app.assist.suggest("email", context, draft)
        if tail and ta.text == draft and ta.cursor_location == ta.document.end:
            ta.placeholder = ""  # an empty box shows its placeholder instead of the suggestion
            ta.show_suggestion(tail)

    def _text(self) -> str:
        return self.query_one("#c-body", TextArea).text.rstrip()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#web")
    def _web(self) -> None:
        subprocess.Popen(["open", self.r["web"]])

    @on(Button.Pressed, "#send")
    def action_send(self) -> None:
        st = self.query_one("#status", Static)
        btn = self.query_one("#send", Button)
        if not self._text():
            st.update(_bad("write something first"))
            return
        if not self.armed:  # two presses, so a stray key never sends mail
            self.armed = True
            btn.label = "PRESS AGAIN TO SEND"
            to, _ = self._recipients()
            st.update(_busy(f"about to send to {', '.join(to)}{' + everyone on the thread' if self.all else ''}"))
            self.set_timer(5, self._disarm)
            return
        self._send()

    def _disarm(self) -> None:
        if self.armed and not self.query_one("#send", Button).disabled:
            self.armed = False
            self.query_one("#send", Button).label = "SEND  ctrl+s"
            self.query_one("#status", Static).update("")

    @work(exclusive=True)
    async def _send(self) -> None:
        from .sources.mail import NeedsConsent

        st = self.query_one("#status", Static)
        btn = self.query_one("#send", Button)
        btn.disabled = True
        via = self.r["via"]
        try:
            if via == "graph":
                st.update(_busy("sending…"))
                await self.app.outlook.reply(self.r["id"], self._text(), self.all)
                st.update(_ok("sent"))
                self.set_timer(1.2, self._finish)
                return
            subprocess.run(["pbcopy"], input=self._text().encode())
            if via == "apple":
                mid = self.r["mid"].replace("\\", "\\\\").replace('"', '\\"')
                script = (
                    'tell application "Mail"\n'
                    f'  set m to first message of inbox whose message id is "{mid}"\n'
                    f'  reply m with opening window{" and reply to all" if self.all else ""}\n'
                    "  activate\nend tell"
                )
                subprocess.Popen(["osascript", "-e", script])
            else:
                to, cc = self._recipients()
                url = f"mailto:{quote(','.join(to))}?subject={quote(_subject(self.r.get('subject', '')))}"
                if cc:
                    url += f"&cc={quote(','.join(cc))}"
                subprocess.Popen(["open", url])
            st.update(_ok("reply opened, your text is on the clipboard: paste it in"))
            self.set_timer(2, self._finish)
        except NeedsConsent:
            from .setup import OutlookScreen

            st.update(_busy("sending needs one more Microsoft permission (Mail.Send)…"))
            self.armed = True

            def granted(ok: bool | None) -> None:
                btn.disabled = False
                if ok:
                    st.update(_ok("permission granted, press SEND to send"))
                    btn.label = "SEND NOW"
                else:
                    st.update(_bad("no send permission, nothing was sent"))
                    btn.label = "SEND  ctrl+s"
                    self.armed = False

            self.app.push_screen(OutlookScreen(extra_scopes=["Mail.Send"]), granted)
        except Exception as exc:
            st.update(_bad(f"not sent: {exc}"[:200]))
            btn.disabled = False
            btn.label = "SEND  ctrl+s"
            self.armed = False
