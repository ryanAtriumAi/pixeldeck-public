"""The AGENT tile: briefs, suggestions and tasks your agent (Hermes) left through `pixeldeck mcp`.

A suggestion never does anything by itself. Enter opens its card: ACCEPT opens the normal screen for it
(reply composer with the draft, Slack box with the message, NEW EVENT filled in) where you still press
SEND / Enter / CREATE; SNOOZE hides it for a while; DISMISS drops it (and the agent sees you did).
"""

from __future__ import annotations

import json
import time

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import agentstore as st
from . import pixel as px
from .sources.base import Feed, Item, ago

ICON = {"reply_mail": "✉", "slack_message": "#", "create_event": "◷", "add_task": "☐", "open": "▸"}
VERB = {"reply_mail": "REPLY", "slack_message": "SLACK", "create_event": "EVENT", "add_task": "TASK", "open": "LOOK"}
ACCEPT_LABEL = {"reply_mail": "OPEN REPLY", "slack_message": "OPEN IN SLACK", "create_event": "OPEN NEW EVENT",
                "add_task": "ADD TASK", "open": "OPEN"}
SNOOZE_SECS = 2 * 3600


def _ts(t: float) -> object:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t, tz=timezone.utc)


def build_feed(store: st.AgentStore, seen: set[str] | None) -> Feed:
    """seen: keys already shown (None on the first build, so nothing pulses at startup)."""
    items: list[Item] = []
    now = time.time()
    ups = [u for u in store.updates(1)]
    for u in ups:
        items.append(Item(
            title=f"BRIEF · {u['title']}", meta=f"from your agent · {ago(_ts(u['created']))}",
            icon="✎", tone="accent" if not u["seen"] else "muted", unread=not u["seen"],
            ref={"kind": "agent_update", "id": u["id"], "body": u["body"], "key": f"u{u['id']}"}))
    props = store.proposals()
    for p in props:
        about = st.item_from_json(p["item"])
        meta = " · ".join(x for x in [VERB.get(p["kind"], p["kind"]), p["why"] or (about.title if about else "")] if x)
        items.append(Item(title=p["title"], meta=meta, icon=ICON.get(p["kind"], "✦"), tone="secondary",
                          ref={"kind": "agent_proposal", "id": p["id"], "key": f"p{p['id']}"}))
    tasks = store.tasks()
    for t in tasks:
        late = bool(t["due"] and t["due"] < now)
        soon = bool(t["due"] and not late and t["due"] - now < 86400)
        meta = " · ".join(x for x in [
            ("OVERDUE " if late else "due ") + st.fmt_when(t["due"]) if t["due"] else "",
            "from agent" if t["source"] == "agent" else "", (t["notes"] or "")[:80]] if x)
        items.append(Item(title=t["title"], meta=meta or "no due date", icon="☐",
                          tone="error" if late else "warning" if soon else "primary",
                          ref={"kind": "agent_task", "id": t["id"], "key": f"t{t['id']}"}))
    # thin lines between brief · suggestions · tasks
    groups = [len(ups), len(props), len(tasks)]
    at = 0
    for n in groups[:-1]:
        at += n
        if n and at < len(items):
            items[at - 1].rule_after = True
    if seen is not None:
        for it in items:
            if it.ref["key"] not in seen and it.ref["kind"] != "agent_task":
                it.flash = True
    note = None if items else "nothing from your agent yet · + adds a task"
    return Feed(items=items, count=len(props) + len(tasks) or None, note=note,
                active=sum(1 for t in tasks if t["due"] and t["due"] < now) or None)


# ── cards ─────────────────────────────────────────────────────────────────────

CARD_CSS = f"""
AgentCard {{ align: center middle; background: {px.VOID} 70%; }}
AgentCard > VerticalScroll {{ width: 100; max-width: 95%; height: auto; max-height: 85%; background: {px.DEEP};
                              border: panel {px.HOTPINK}; border-title-color: {px.VOID}; border-title-style: bold;
                              padding: 1 2; }}
AgentCard .draft {{ background: {px.PANEL}; color: {px.INK}; padding: 1 2; margin-top: 1; }}
AgentCard .buttons {{ height: auto; margin-top: 1; align-horizontal: right; }}
AgentCard Button {{ margin-left: 1; min-width: 12; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
AgentCard Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
"""


def _label(text: str, color: str) -> Text:
    return Text(f" {text} ", Style(color=px.VOID, bgcolor=color, bold=True))


def card_title(row, is_task: bool) -> str:
    if is_task:
        return " ☐ TASK "
    return f" {ICON.get(row['kind'], '✦')} SUGGESTION · {VERB.get(row['kind'], row['kind'])} "


def card_head(row, is_task: bool) -> Text:
    r, head = row, Text()
    about = st.item_from_json(r["item"])
    head.append(r["title"], Style(color=px.INK, bold=True))
    if is_task:
        if r["due"]:
            late = r["due"] < time.time() and r["status"] == "open"
            head.append("\n")
            head.append_text(_label("OVERDUE" if late else "DUE", px.RED if late else px.CYAN))
            head.append(f" {st.fmt_when(r['due'])}", Style(color=px.INK))
        head.append(f"\nadded {ago(_ts(r['created']))}{' by your agent' if r['source'] == 'agent' else ''}",
                    Style(color=px.DIM))
    else:
        if r["why"]:
            head.append(f"\n{r['why']}", Style(color=px.CYAN))
        head.append(f"\nfrom your agent · {ago(_ts(r['created']))}", Style(color=px.DIM))
    if about:
        head.append("\n\n")
        head.append_text(_label("ABOUT", px.PURPLE))
        head.append(f" {about.title}\n", Style(color=px.INK))
        head.append(f"       {about.meta}", Style(color=px.DIM))
    return head


def card_body(row, is_task: bool) -> Text | None:
    r = row
    if is_task:
        return Text(r["notes"]) if r["notes"] else None
    p = json.loads(r["payload"] or "{}")
    kind = r["kind"]
    if kind in ("reply_mail", "slack_message"):
        out = Text()
        out.append_text(_label("DRAFT", px.HOTPINK))
        out.append("  you'll see it in the composer and still have to send it\n\n", Style(color=px.DIM))
        out.append(p.get("draft") or "(no draft: you write it)", Style(color=px.INK))
        return out
    if kind == "create_event":
        ev = p.get("event") or {}
        out = Text()
        out.append_text(_label("EVENT", px.CYAN))
        out.append("  opens NEW EVENT filled in; nothing is created until you press CREATE\n", Style(color=px.DIM))
        for k in ("title", "day", "start", "length", "invitees", "where", "notes"):
            if ev.get(k):
                out.append(f"\n{k:>9}  ", Style(color=px.DIM))
                out.append(str(ev[k]), Style(color=px.YELLOW if k == "invitees" else px.INK))
        return out
    if kind == "add_task":
        out = Text()
        out.append_text(_label("TASK", px.PURPLE))
        if p.get("due"):
            out.append(f"  due {st.fmt_when(p['due'])}", Style(color=px.INK))
        if p.get("notes"):
            out.append(f"\n\n{p['notes']}", Style(color=px.INK))
        return out
    return None


def card_buttons(row, is_task: bool) -> list[Button]:
    about = bool(row["item"])
    if is_task:
        out = [Button("OPEN SOURCE  o", id="source")] if about else []
        if row["status"] == "open":
            out += [Button("TOMORROW  t", id="tomorrow"), Button("DONE  ⏎", id="done", variant="primary")]
        return out
    out = [Button("OPEN SOURCE  o", id="source")] if about and row["kind"] != "open" else []
    return out + [Button("DISMISS  x", id="dismiss"), Button("SNOOZE 2H  z", id="snooze"),
                  Button(f"{ACCEPT_LABEL.get(row['kind'], 'ACCEPT')}  ⏎", id="accept", variant="primary")]


def allowed(row, is_task: bool, what: str) -> bool:
    if what == "source":
        return bool(row["item"])
    return what in ({"done", "tomorrow"} if is_task else {"accept", "snooze", "dismiss"})


class AgentCard(ModalScreen[str | None]):
    """A suggestion or a task. Dismisses with what you chose: accept/snooze/dismiss/source, done/tomorrow."""

    CSS = CARD_CSS
    BINDINGS = [Binding("escape", "close", "Back"),  # Enter presses the focused button (ACCEPT / DONE first)
                Binding("z", "pick('snooze')", "Snooze"), Binding("x", "pick('dismiss')", "Dismiss"),
                Binding("o", "pick('source')", "Open source"), Binding("t", "pick('tomorrow')", "Tomorrow")]

    def __init__(self, row, is_task: bool) -> None:
        super().__init__()
        self.row, self.is_task = row, is_task

    def compose(self) -> ComposeResult:
        with VerticalScroll() as box:
            box.border_title = card_title(self.row, self.is_task)
            yield Static(card_head(self.row, self.is_task))
            body = card_body(self.row, self.is_task)
            if body is not None:
                yield Static(body, classes="draft")
            with Horizontal(classes="buttons"):
                yield from card_buttons(self.row, self.is_task)

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_primary)  # the buttons mount a beat after the screen

    def _focus_primary(self) -> None:
        for b in self.query("#done, #accept").results(Button):
            b.focus()

    @on(Button.Pressed)
    def _pressed(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id)

    def action_pick(self, what: str) -> None:
        if allowed(self.row, self.is_task, what):
            self.dismiss(what)

    def action_close(self) -> None:
        self.dismiss(None)


# ── the full page (00) ──────────────────────────────────────────────────────────

PAGE_CSS = f"""
AgentScreen {{ background: {px.VOID}; }}
#ag-head {{ height: auto; background: {px.DEEP}; padding: 1 2 0 2; }}
#ag-body {{ height: 1fr; }}
#ag-list {{ width: 52; height: 1fr; background: {px.DEEP}; border: none; border-right: tall {px.PURPLE}; padding: 0 1; }}
#ag-list > .option-list--option-highlighted {{ background: #3a1670; }}
#ag-right {{ width: 1fr; height: 1fr; }}
#ag-detail {{ height: 1fr; padding: 1 2; background: {px.VOID}; scrollbar-color: {px.PURPLE}; scrollbar-size-vertical: 1; }}
#ag-detail .draft {{ background: {px.PANEL}; color: {px.INK}; padding: 1 2; margin-top: 1; max-width: 110; }}
#ag-detail Markdown {{ background: {px.DEEP}; padding: 1 2 0 2; margin: 0; max-width: 110; }}
#ag-detail MarkdownH1, #ag-detail MarkdownH2, #ag-detail MarkdownH3 {{ color: {px.HOTPINK}; background: transparent; text-style: bold; }}
#ag-buttons {{ height: auto; padding: 0 2 1 2; align-horizontal: right; }}
#ag-buttons Button {{ margin-left: 1; min-width: 12; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
#ag-buttons Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
"""


class AgentScreen(Screen):
    """Everything your agent left, in one place: briefs, suggestions, tasks (and what you finished)."""

    CSS = PAGE_CSS
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("z", "act('snooze')", "Snooze"),
        Binding("x", "act('dismiss')", "Dismiss"),
        Binding("o", "act('source')", "Open source"),
        Binding("t", "act('tomorrow')", "Tomorrow"),
        Binding("plus", "add_task", "Add task"),
        Binding("ctrl+r", "reload", "Reload"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[tuple[str, str, object]] = []  # (key, kind, row)
        self.current: tuple[str, str, object] | None = None

    @property
    def store(self) -> st.AgentStore:
        return self.app.agent_store

    def compose(self) -> ComposeResult:
        yield Static(id="ag-head")
        with Horizontal(id="ag-body"):
            yield OptionList(id="ag-list")
            with Vertical(id="ag-right"):
                yield VerticalScroll(id="ag-detail")
                yield Horizontal(id="ag-buttons")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()
        self.query_one("#ag-list", OptionList).focus()

    def on_screen_resume(self) -> None:
        self.reload(self.current[0] if self.current else None)

    def action_reload(self) -> None:
        self.reload(self.current[0] if self.current else None)

    def reload(self, keep: str | None = None) -> None:
        now = time.time()
        ups = self.store.updates(5)
        props = self.store.proposals()
        tasks = self.store.tasks()
        done = [t for t in self.store.tasks(include_done=True) if t["status"] == "done"
                and (t["done_at"] or 0) > now - 3 * 86400][-5:]
        self.entries = ([(f"u{u['id']}", "update", u) for u in ups] + [(f"p{p['id']}", "proposal", p) for p in props]
                        + [(f"t{t['id']}", "task", t) for t in tasks] + [(f"t{t['id']}", "task", t) for t in done])
        late = sum(1 for t in tasks if t["due"] and t["due"] < now)
        head = Text()
        head.append(" ✦ AGENT ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        head.append(f"  {len(props)} suggestions · {len(tasks)} tasks", Style(color=px.INK, bold=True))
        if late:
            head.append(f" · {late} overdue", Style(color=px.RED, bold=True))
        status = hermes_status()
        if status:
            head.append(f"   {status}", Style(color=px.DIM))
        head.append("\n⏎ accept / done · z snooze · x dismiss · o open source · t tomorrow · + add task · esc back",
                    Style(color=px.DIM))
        self.query_one("#ag-head", Static).update(head)

        ol = self.query_one("#ag-list", OptionList)
        width = 46
        opts: list[Option | None] = []
        sections = [("BRIEFS", "update", ups), ("SUGGESTIONS", "proposal", props), ("TASKS", "task", tasks),
                    ("DONE LATELY", "task", done)]
        for label, kind, rows in sections:
            if not rows and label in ("BRIEFS", "DONE LATELY"):
                continue
            opts.append(Option(Text(f" {label} ", Style(color=px.VOID, bgcolor=px.PURPLE, bold=True)), disabled=True))
            if not rows:
                opts.append(Option(Text("  (none)", Style(color=px.DIM)), disabled=True))
            for r in rows:
                opts.append(Option(self._row_text(kind, r, width), id=f"{kind[0]}{r['id']}" if kind != "task"
                                   else f"t{r['id']}"))
        ol.set_options(opts)
        target = keep if keep and any(e[0] == keep for e in self.entries) else (self.entries[0][0] if self.entries else None)
        if target:
            try:
                ol.highlighted = ol.get_option_index(target)
            except Exception:
                pass
        else:
            self._show(None)

    def _row_text(self, kind: str, r, width: int) -> Text:
        now = time.time()
        top = Text(no_wrap=True)
        if kind == "update":
            top.append("✎ ", Style(color=px.HOTPINK, bold=True))
            top.append(r["title"], Style(color=px.INK, bold=not r["seen"]))
            sub = f"  {ago(_ts(r['created']))}"
        elif kind == "proposal":
            top.append(f"{ICON.get(r['kind'], '✦')} ", Style(color=px.CYAN, bold=True))
            top.append(r["title"], Style(color=px.INK))
            sub = "  " + " · ".join(x for x in [VERB.get(r["kind"], r["kind"]), r["why"]] if x)
        else:
            is_done = r["status"] == "done"
            late = bool(r["due"] and r["due"] < now and not is_done)
            top.append("☑ " if is_done else "☐ ", Style(color=px.GREEN if is_done else px.RED if late else px.INK, bold=True))
            top.append(r["title"], Style(color=px.DIM if is_done else px.INK, strike=is_done))
            sub = "  " + (("OVERDUE " if late else "due ") + st.fmt_when(r["due"]) if r["due"] and not is_done
                          else "done" if is_done else "no due date")
        top.truncate(width, overflow="ellipsis")
        bottom = Text(sub, Style(color=px.DIM), no_wrap=True)
        bottom.truncate(width, overflow="ellipsis")
        return Text("\n").join([top, bottom])

    def _entry(self, oid: str | None) -> tuple[str, str, object] | None:
        if not oid:
            return None
        key = "u" + oid[1:] if oid.startswith("u") else oid
        return next((e for e in self.entries if e[0] == key), None)

    @on(OptionList.OptionHighlighted, "#ag-list")
    def _highlighted(self, e: OptionList.OptionHighlighted) -> None:
        self._show(self._entry(e.option.id))

    @on(OptionList.OptionSelected, "#ag-list")
    def _selected(self, e: OptionList.OptionSelected) -> None:
        entry = self._entry(e.option.id)
        if not entry:
            return
        _key, kind, row = entry
        if kind == "proposal":
            self._do("accept")
        elif kind == "task" and row["status"] == "open":
            self._do("done")
        elif kind == "task":
            self.app.agent_store.complete_task(row["id"], False)
            self.reload(entry[0])

    def _show(self, entry) -> None:
        self.current = entry
        self._paint(entry)

    @work(exclusive=True, group="ag-show")
    async def _paint(self, entry) -> None:
        detail = self.query_one("#ag-detail", VerticalScroll)
        buttons = self.query_one("#ag-buttons", Horizontal)
        await detail.remove_children()
        await buttons.remove_children()
        if entry is None:
            await detail.mount(Static(Text("Nothing from your agent yet. + adds a task of your own.", Style(color=px.DIM))))
            return
        key, kind, row = entry
        if kind == "update":
            head = Text()
            head.append_text(_label("BRIEF", px.HOTPINK))
            head.append(f"  {row['title']}", Style(color=px.INK, bold=True))
            head.append(f"\n{ago(_ts(row['created']))}\n", Style(color=px.DIM))
            await detail.mount(Static(head), Markdown(row["body"]))
            if not row["seen"]:
                self.store.mark_update_seen(row["id"])
            self.app._agent_opened.add(key)
            return
        is_task = kind == "task"
        widgets = [Static(card_head(row, is_task))]
        body = card_body(row, is_task)
        if body is not None:
            widgets.append(Static(body, classes="draft"))
        await detail.mount(*widgets)
        await buttons.mount(*card_buttons(row, is_task))
        self.app._agent_opened.add(key)

    @on(Button.Pressed, "#ag-buttons Button")
    def _pressed(self, e: Button.Pressed) -> None:
        self._do(e.button.id or "")

    def action_act(self, what: str) -> None:
        self._do(what)

    def _do(self, what: str) -> None:
        if not self.current or self.current[1] == "update":
            return
        key, kind, row = self.current
        is_task = kind == "task"
        if not allowed(row, is_task, what):
            return
        # land on the next row once this one goes away
        idx = next((i for i, e in enumerate(self.entries) if e[0] == key), 0)
        nxt = self.entries[idx + 1][0] if idx + 1 < len(self.entries) else (self.entries[idx - 1][0] if idx else None)
        self.app.agent_act(row, is_task, what)
        gone = what in ("accept", "snooze", "dismiss", "done")
        self.reload(nxt if gone else key)

    def action_add_task(self) -> None:
        self.app.action_add_task(lambda: self.reload())

    def action_back(self) -> None:
        self.dismiss()


def hermes_status() -> str:
    """'Hermes: brief Mon 08:30' from Hermes' own job list, if it has PIXELDECK jobs."""
    from pathlib import Path

    try:
        data = json.loads((Path.home() / ".hermes" / "cron" / "jobs.json").read_text())
    except (OSError, ValueError):
        return ""
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        name = str(j.get("name", ""))
        if not name.lower().startswith("pixeldeck") or not j.get("enabled", True):
            continue
        nxt = j.get("next_run_at") or j.get("next_run")
        when = ""
        if nxt:
            from .sources.base import parse_time

            dt = parse_time(str(nxt))
            when = f" {st.fmt_when(dt.timestamp())}" if dt else ""
        out.append(f"{name.split(':', 1)[-1].strip() or name}{when}")
    if not out:
        return ""
    alive = False
    try:
        import os

        pid = int(json.loads((Path.home() / ".hermes" / "gateway_state.json").read_text()).get("pid") or 0)
        if pid:
            os.kill(pid, 0)
            alive = True
    except (OSError, ValueError, TypeError):
        pass
    return "Hermes: " + " · ".join(out) + ("" if alive else " · gateway off, jobs won't run (hermes gateway install)")
