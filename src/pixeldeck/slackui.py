"""Full-screen Slack console: conversations, messages, threads, reactions, composer."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static

from .widgets import TextInput
from textual.widgets.option_list import Option

from . import pixel as px
from textual.suggester import SuggestionReady

from .sources.slackclient import Convo, Message, SlackClient

EMOJI = {
    "+1": "👍", "thumbsup": "👍", "-1": "👎", "heart": "❤️", "eyes": "👀", "white_check_mark": "✅",
    "heavy_check_mark": "✔️", "joy": "😂", "tada": "🎉", "fire": "🔥", "pray": "🙏", "raised_hands": "🙌",
    "100": "💯", "rocket": "🚀", "smile": "😄", "ok_hand": "👌", "thinking_face": "🤔", "x": "❌",
    "clap": "👏", "wave": "👋", "laughing": "😆", "sweat_smile": "😅", "muscle": "💪", "ship": "🚢",
}

SLACK_CSS = f"""
SlackScreen {{ background: {px.VOID}; }}
#sl-head {{ height: 2; background: {px.DEEP}; padding: 0 1; }}
#sl-body {{ height: 1fr; }}
#sl-left {{ width: 38; background: {px.DEEP}; border-right: tall {px.PURPLE}; }}
#sl-find {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
#sl-find:focus {{ border: tall {px.HOTPINK}; }}
#sl-convos {{ height: 1fr; background: {px.DEEP}; border: none; scrollbar-size-vertical: 1; }}
#sl-right {{ width: 1fr; }}
#sl-msgs {{ height: 1fr; background: {px.VOID}; border: none; padding: 0 1; scrollbar-size-vertical: 1;
            scrollbar-color: {px.PURPLE}; }}
#sl-compose {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
#sl-compose:focus {{ border: tall {px.HOTPINK}; }}
#sl-compose.-edit {{ border: tall {px.YELLOW}; }}
#sl-compose > .input--suggestion {{ color: {px.HOTPINK} 55%; }}
SlackScreen OptionList > .option-list--option-highlighted {{ background: #3a1670; }}
SlackScreen OptionList:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
SlackScreen OptionList > .option-list--option {{ padding: 0 1; }}
AskScreen {{ align: center middle; background: {px.VOID} 60%; }}
AskScreen > Vertical {{ width: 70; height: auto; max-height: 80%; background: {px.DEEP}; border: panel {px.HOTPINK};
                        border-title-color: {px.VOID}; border-title-style: bold; padding: 1 2; }}
AskScreen Input {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
AskScreen OptionList {{ height: auto; max-height: 14; background: {px.DEEP}; border: none; }}
"""


def _emoji(name: str) -> str:
    return EMOJI.get(name, f":{name}:")


class AskScreen(ModalScreen[str | None]):
    """One-line prompt, optionally with a filtered pick list (for emoji / people)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str, choices: list[tuple[str, str]] | None = None) -> None:
        super().__init__()
        self.title_text, self.placeholder, self.choices = title, placeholder, choices

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = f" {self.title_text} "
            yield TextInput(placeholder=self.placeholder, id="ask")
            if self.choices is not None:
                yield OptionList(id="pick")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#ask", Input).focus()

    def _fill(self, needle: str) -> None:
        if self.choices is None:
            return
        pick = self.query_one("#pick", OptionList)
        n = needle.lower().strip()
        shown = [(label, value) for label, value in self.choices if n in label.lower()][:60]
        pick.set_options([Option(label, id=value) for label, value in shown])
        if shown:
            pick.highlighted = 0

    @on(Input.Changed, "#ask")
    def _changed(self, e: Input.Changed) -> None:
        self._fill(e.value)

    @on(Input.Submitted, "#ask")
    def _submit(self, e: Input.Submitted) -> None:
        if self.choices is not None:
            pick = self.query_one("#pick", OptionList)
            opt = pick.highlighted_option
            self.dismiss(opt.id if opt else None)
        else:
            self.dismiss(e.value.strip() or None)

    @on(OptionList.OptionSelected, "#pick")
    def _picked(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(e.option.id)

    def on_key(self, event) -> None:
        if self.choices is None or event.key not in ("up", "down"):
            return
        pick = self.query_one("#pick", OptionList)
        if pick.option_count:
            cur = pick.highlighted or 0
            pick.highlighted = max(0, min(pick.option_count - 1, cur + (1 if event.key == "down" else -1)))
            event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)


class SlackScreen(Screen):
    CSS = SLACK_CSS
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+n", "new_dm", "New DM"),
        Binding("ctrl+f", "find", "Find convo"),
        Binding("t", "thread", "Thread"),
        Binding("plus,equals_sign", "react", "React"),
        Binding("e", "edit", "Edit mine"),
        Binding("x", "delete", "Delete mine"),
        Binding("o", "open_slack", "Open in Slack"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("ctrl+b", "toggle_apps", "Show/hide apps"),
        Binding("asterisk", "star", "Star channel"),
    ]

    def __init__(self, client: SlackClient, convo: str | None = None, ts: str | None = None,
                 thread_ts: str | None = None, draft: str | None = None) -> None:
        super().__init__()
        self.draft = draft  # an agent's proposed message: put in the box, Enter still sends
        self.sc = client
        self.convo_id = convo
        self.focus_ts = ts
        self.thread_ts: str | None = thread_ts
        self.messages: list[Message] = []
        self.editing: Message | None = None
        self._delete_armed: tuple[str, float] | None = None
        self._suggest_timer = None
        self._last_seen_ts: str | None = None

    # layout -------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="sl-head")
        with Horizontal(id="sl-body"):
            with Vertical(id="sl-left"):
                yield TextInput(placeholder="find a conversation…", id="sl-find")
                yield OptionList(id="sl-convos")
            with Vertical(id="sl-right"):
                yield OptionList(id="sl-msgs")
                yield TextInput(placeholder="pick a conversation", id="sl-compose")
        yield Footer()

    def on_mount(self) -> None:
        self._head()
        self._boot()
        self.set_interval(10, self._poll)

    @work(exclusive=True, group="boot")
    async def _boot(self) -> None:
        try:
            await self.sc.list_convos()
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ SLACK", severity="error", timeout=10)
            return
        self._fill_convos()
        if self.convo_id:
            await self._open(self.convo_id, self.focus_ts, self.thread_ts)
        else:
            self.query_one("#sl-convos", OptionList).focus()
        # fill in unread state for the rest in the background
        while any(not c.refreshed for c in self.sc.convos.values()):
            await self.sc.refresh_some(8)
            self._fill_convos()

    # conversation list --------------------------------------------------------

    def _convo_row(self, c: Convo) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        unread = c.refreshed and c.has_unread
        icon = "✦" if c.kind == "im" else ("◆" if c.kind == "mpim" else
                                          ("★" if self.sc.is_starred(c) else ("🔒" if c.kind == "private" else "#")))
        name = c.name.lstrip("#@")
        t.append(f"{icon} ", Style(color=px.HOTPINK if unread else px.DIM))
        t.append(name[:26], Style(color=px.INK if unread else px.DIM, bold=bool(unread)))
        if c.unread:
            t.append(f" {c.unread}", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        elif unread:
            t.append(" •", Style(color=px.HOTPINK, bold=True))
        return t

    def _fill_convos(self) -> None:
        lst = self.query_one("#sl-convos", OptionList)
        needle = self.query_one("#sl-find", Input).value.strip().lower()
        keep = lst.highlighted_option.id if lst.highlighted_option else self.convo_id
        ranked = [c for c in self.sc.ranked(include_old=bool(needle)) if needle in c.name.lower()]
        dms = [c for c in ranked if self.sc.group(c) == 0]
        starred = [c for c in ranked if self.sc.group(c) == 1]
        chans = [c for c in ranked if self.sc.group(c) == 2]
        opts: list[Option] = []
        for title, group in (("DIRECT MESSAGES", dms), ("★ STARRED", starred), ("CHANNELS", chans)):
            if not group:
                continue
            opts.append(Option(Text(title, Style(color=px.CYAN, bold=True)), disabled=True))
            opts += [Option(self._convo_row(c), id=c.id) for c in group]
        if not needle and self.sc.dm_days:
            hidden = sum(1 for c in self.sc.convos.values() if self.sc.visible(c) and not self.sc.recent(c))
            if hidden:
                opts.append(Option(Text(f"{hidden} DMs quiet > {self.sc.dm_days}d hidden · type to find them",
                                        Style(color=px.DIM, italic=True)), disabled=True))
        lst.set_options(opts)
        if keep:
            try:
                lst.highlighted = lst.get_option_index(keep)
            except Exception:
                pass

    @on(Input.Changed, "#sl-find")
    def _find_changed(self) -> None:
        self._fill_convos()

    @on(Input.Submitted, "#sl-find")
    def _find_submit(self) -> None:
        lst = self.query_one("#sl-convos", OptionList)
        first = next((o for o in lst.options if not o.disabled), None)
        if first:
            self._select(first.id)

    @on(OptionList.OptionSelected, "#sl-convos")
    def _convo_selected(self, e: OptionList.OptionSelected) -> None:
        self._select(e.option.id)

    def _select(self, cid: str) -> None:
        self.thread_ts = None
        self.focus_ts = None
        find = self.query_one("#sl-find", Input)
        if find.value:
            find.value = ""  # bring the full list back once you've jumped
            self.convo_id = cid
            self._fill_convos()
        self.run_worker(self._open(cid), exclusive=True, group="open")

    # messages -----------------------------------------------------------------

    async def _open(self, cid: str, ts: str | None = None, thread_ts: str | None = None) -> None:
        self.convo_id, self.thread_ts = cid, thread_ts
        self.editing = None
        await self._load_messages(scroll_to=ts)
        compose = self.query_one("#sl-compose", Input)
        compose.focus()
        self._head()
        if self.draft:
            compose.value, self.draft = self.draft, None
            compose.cursor_position = len(compose.value)
            return
        self._ask_soon(0.1)

    async def _load_messages(self, scroll_to: str | None = None) -> None:
        if not self.convo_id:
            return
        if not self.messages or getattr(self, "_painted_for", None) != (self.convo_id, self.thread_ts):
            cached = self.sc.cached(self.convo_id, self.thread_ts)
            self._painted_for = (self.convo_id, self.thread_ts)
            if cached:
                self.messages = cached
                self._render_messages(scroll_to)
        try:
            if self.thread_ts:
                msgs = await self.sc.replies(self.convo_id, self.thread_ts)
            else:
                msgs = await self.sc.history(self.convo_id)
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ SLACK", severity="error", timeout=8)
            return
        self.messages = msgs
        self._render_messages(scroll_to)
        if msgs:
            newest = msgs[-1].ts
            if newest != self._last_seen_ts:
                self._last_seen_ts = newest
                c = self.sc.convos.get(self.convo_id)
                if c and not self.thread_ts and (c.has_unread or float(newest) > float(c.last_read or 0)):
                    try:
                        await self.sc.mark_read(self.convo_id, newest)
                    except Exception:
                        pass  # missing *:write scope: reading still works
                    self._fill_convos()

    def _render_message(self, m: Message, width: int) -> Text:
        t = Text()
        mine = m.user == self.sc.me
        app = self.sc.is_app(m.user) or m.subtype == "bot_message"
        t.append(self.sc.name(m.user), Style(color=px.CYAN if mine else (px.DIM if app else px.HOTPINK), bold=True))
        if app:
            t.append(" APP", Style(color=px.VOID, bgcolor=px.SHADOW))
        t.append(f"  {m.when.astimezone():%H:%M}", Style(color=px.DIM))
        if m.edited:
            t.append("  (edited)", Style(color=px.DIM))
        if m.subtype in ("channel_join", "channel_leave"):
            t.append("  " + self.sc.render_text(m.text), Style(color=px.DIM, italic=True))
            return t
        t.append("\n")
        body = self.sc.render_text(m.text)
        if self.sc.mentions_me(m.text):
            t.append(body, Style(color=px.INK, bgcolor="#3a1050"))
        else:
            t.append(body, Style(color=px.DIM if app else px.INK))
        for f in m.files:
            t.append(f"\n📎 {f}", Style(color=px.CYAN))
        if m.reactions:
            t.append("\n")
            for name, count, me in m.reactions:
                t.append(f" {_emoji(name)} {count} ", Style(color=px.VOID if me else px.INK,
                                                            bgcolor=px.PURPLE if me else px.SHADOW))
                t.append(" ")
        if m.reply_count and not self.thread_ts:
            t.append(f"\n↳ {m.reply_count} repl{'y' if m.reply_count == 1 else 'ies'} · t to open",
                     Style(color=px.HOTPINK))
        return t

    def _render_messages(self, scroll_to: str | None = None) -> None:
        lst = self.query_one("#sl-msgs", OptionList)
        keep = lst.highlighted_option.id if lst.highlighted_option else None
        at_bottom = lst.highlighted is None or lst.highlighted >= lst.option_count - 1
        opts: list[Option] = []
        last_day = None
        for m in self.messages:
            day = m.when.astimezone().date()
            if day != last_day:
                opts.append(Option(Text(f"── {m.when.astimezone():%a %d %b} ──", Style(color=px.DIM)), disabled=True))
                last_day = day
            opts.append(Option(self._render_message(m, lst.size.width), id=m.ts))
        if not self.messages:
            opts.append(Option(Text("no messages yet, say hi below", Style(color=px.DIM)), disabled=True))
        lst.set_options(opts)
        target = scroll_to or (None if at_bottom else keep)
        if target:
            try:
                lst.highlighted = lst.get_option_index(target)
            except Exception:
                target = None
        if not target and lst.option_count:
            lst.highlighted = lst.option_count - 1
        lst.scroll_to_highlight()

    def _poll(self) -> None:
        if self.convo_id and not self.editing:
            self.run_worker(self._load_messages(), exclusive=True, group="poll")

    def _selected_msg(self) -> Message | None:
        lst = self.query_one("#sl-msgs", OptionList)
        opt = lst.highlighted_option
        return next((m for m in self.messages if opt and m.ts == opt.id), None)

    # header -------------------------------------------------------------------

    def _head(self) -> None:
        t = Text(no_wrap=True)
        t.append(" SLACK ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        if self.sc.me:
            t.append(f"  as @{self.sc.users.get(self.sc.me, 'you')}", Style(color=px.DIM))
        c = self.sc.convos.get(self.convo_id or "")
        if c:
            t.append("   ")
            t.append(c.name, Style(color=px.INK, bold=True))
            if self.thread_ts:
                t.append("  ▸ THREAD", Style(color=px.YELLOW, bold=True))
        t.append("\n")
        hint = ("Enter send · → take suggestion · ↑ messages · t thread · + react · e edit · x delete · * star · o open in Slack · "
                "ctrl+n new DM · ctrl+b apps · esc back")
        t.append(hint, Style(color=px.DIM))
        self.query_one("#sl-head", Static).update(t)
        compose = self.query_one("#sl-compose", Input)
        if self.editing:
            compose.placeholder = "editing your message · Enter saves · esc cancels"
        elif c:
            where = f"thread in {c.name}" if self.thread_ts else c.name
            compose.placeholder = f"message {where} · Enter sends"

    # composer -----------------------------------------------------------------

    # suggestions ---------------------------------------------------------------

    @on(Input.Changed, "#sl-compose")
    def _typed(self, e: Input.Changed) -> None:
        self._ask_soon()

    def _ask_soon(self, delay: float = 0.35) -> None:
        assist = getattr(self.app, "assist", None)
        if not assist or not assist.enabled or self.editing or not self.convo_id:
            return
        if self._suggest_timer:
            self._suggest_timer.stop()
        self._suggest_timer = self.set_timer(delay, lambda: self.run_worker(self._suggest(), exclusive=True,
                                                                              group="suggest"))

    def _convo_context(self) -> str:
        c = self.sc.convos.get(self.convo_id or "")
        where = c.name if c else "a conversation"
        if self.thread_ts:
            where = f"a thread in {where}"
        lines = [f"{self.sc.name(m.user) if m.user != self.sc.me else 'them (the person replying)'}: "
                 f"{self.sc.render_text(m.text)[:300]}" for m in self.messages[-15:] if m.text]
        return f"Slack conversation in {where}, oldest first:\n" + "\n".join(lines)

    async def _suggest(self) -> None:
        compose = self.query_one("#sl-compose", Input)
        draft = compose.value
        if not self.messages:
            return
        if not draft and self.messages[-1].user == self.sc.me:
            return  # you spoke last: don't push a reply
        tail = await self.app.assist.suggest("slack", self._convo_context(), draft)
        if tail and compose.value == draft and not self.editing:
            compose.post_message(SuggestionReady(draft, draft + tail))

    @on(Input.Submitted, "#sl-compose")
    def _send(self, e: Input.Submitted) -> None:
        text = e.value.strip()
        if not text or not self.convo_id:
            return
        self.run_worker(self._deliver(text), exclusive=True, group="send")

    async def _deliver(self, text: str) -> None:
        compose = self.query_one("#sl-compose", Input)
        try:
            if self.editing:
                await self.sc.edit(self.convo_id, self.editing.ts, text)
                self.editing = None
                compose.remove_class("-edit")
            else:
                await self.sc.post(self.convo_id, text, self.thread_ts)
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ NOT SENT", severity="error", timeout=10)
            return
        compose.value = ""
        self._head()
        await self._load_messages()

    # actions ------------------------------------------------------------------

    def action_back(self) -> None:
        compose = self.query_one("#sl-compose", Input)
        if self.editing:
            self.editing = None
            compose.value = ""
            compose.remove_class("-edit")
            self._head()
        elif self.thread_ts:
            back_to = self.thread_ts
            self.thread_ts = None
            self.run_worker(self._load_and_head(back_to), exclusive=True, group="open")
        elif self.focused is compose and self.messages:
            self.query_one("#sl-msgs", OptionList).focus()
        else:
            self.dismiss()

    async def _load_and_head(self, scroll_to: str | None) -> None:
        await self._load_messages(scroll_to)
        self._head()

    def _typing(self) -> bool:
        return isinstance(self.focused, Input)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # single-letter shortcuts must not fire while you're typing
        if action in ("thread", "react", "edit", "delete", "open_slack", "star") and self._typing():
            return False
        return True

    def action_thread(self) -> None:
        m = self._selected_msg()
        if m and self.convo_id and not self.thread_ts:
            self.thread_ts = m.thread_ts or m.ts
            self.run_worker(self._open(self.convo_id, None, self.thread_ts), exclusive=True, group="open")

    def action_react(self) -> None:
        m = self._selected_msg()
        if not m:
            return
        choices = [(f"{v}  :{k}:", k) for k, v in EMOJI.items()]

        def done(name: str | None) -> None:
            if name:
                mine = any(n == name and me for n, _, me in m.reactions)
                self.run_worker(self._react(m, name, remove=mine), group="react")

        self.app.push_screen(AskScreen("REACT", "emoji name (e.g. eyes), Enter picks", choices), done)

    async def _react(self, m: Message, name: str, remove: bool) -> None:
        try:
            await self.sc.react(self.convo_id, m.ts, name, remove=remove)
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ SLACK", severity="error")
            return
        await self._load_messages(scroll_to=m.ts)

    def action_edit(self) -> None:
        m = self._selected_msg()
        if not m or m.user != self.sc.me:
            self.notify("you can only edit your own messages", timeout=3)
            return
        self.editing = m
        compose = self.query_one("#sl-compose", Input)
        compose.value = self.sc.render_text(m.text)
        compose.add_class("-edit")
        compose.focus()
        self._head()

    def action_delete(self) -> None:
        m = self._selected_msg()
        if not m or m.user != self.sc.me:
            self.notify("you can only delete your own messages", timeout=3)
            return
        armed = self._delete_armed
        if armed and armed[0] == m.ts and time.time() - armed[1] < 5:
            self._delete_armed = None
            self.run_worker(self._delete(m), group="delete")
        else:
            self._delete_armed = (m.ts, time.time())
            self.notify("press x again to delete it", title="DELETE?", timeout=5)

    async def _delete(self, m: Message) -> None:
        try:
            await self.sc.delete(self.convo_id, m.ts)
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ SLACK", severity="error")
            return
        await self._load_messages()

    def action_open_slack(self) -> None:
        m = self._selected_msg()
        link = self.sc.deeplink(self.convo_id or "", m.ts if m else None)
        if link:
            subprocess.Popen(["open", link])

    def action_star(self) -> None:
        """Star / unstar the highlighted channel (or the open one)."""
        from .config import set_config_value, toml_str

        lst = self.query_one("#sl-convos", OptionList)
        cid = (lst.highlighted_option.id if self.focused is lst and lst.highlighted_option else None) or self.convo_id
        c = self.sc.convos.get(cid or "")
        if not c or c.is_dm:
            self.notify("stars are for channels (DMs already come first)", timeout=3)
            return
        if self.sc.is_starred(c):
            self.sc.starred -= {c.name.lower(), c.id.lower()}
            msg = f"{c.name} unstarred"
        else:
            self.sc.starred.add(c.name.lower())
            msg = f"{c.name} starred"
        set_config_value("slack", "starred", "[" + ", ".join(toml_str(n) for n in sorted(self.sc.starred)) + "]")
        self._fill_convos()
        self.notify(msg, title="★", timeout=2)

    def action_toggle_apps(self) -> None:
        from .config import set_config_value

        self.sc.hide_apps = not self.sc.hide_apps
        set_config_value("slack", "hide_apps", "true" if self.sc.hide_apps else "false")
        for c in self.sc.convos.values():
            c.refreshed = 0.0  # unread state depends on the setting; re-sync
        self._fill_convos()
        self.run_worker(self._resync(), exclusive=True, group="boot")
        self.notify("app DMs and app posts " + ("hidden" if self.sc.hide_apps else "shown"),
                    title="⚙ SLACK APPS", timeout=3)

    async def _resync(self) -> None:
        while any(not c.refreshed for c in self.sc.convos.values() if self.sc.visible(c)):
            await self.sc.refresh_some(8)
            self._fill_convos()

    def action_find(self) -> None:
        self.query_one("#sl-find", Input).focus()

    def action_reload(self) -> None:
        self._poll()

    def action_new_dm(self) -> None:
        people = sorted(((name, uid) for uid, name in self.sc.users.items() if uid != self.sc.me),
                        key=lambda p: p[0].lower())

        def done(uid: str | None) -> None:
            if uid:
                self.run_worker(self._start_dm(uid), group="open")

        self.app.push_screen(AskScreen("NEW DM", "type a name", people), done)

    async def _start_dm(self, uid: str) -> None:
        try:
            cid = await self.sc.open_dm([uid])
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ SLACK", severity="error")
            return
        self._fill_convos()
        await self._open(cid)
