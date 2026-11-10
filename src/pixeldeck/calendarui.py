"""Agenda page: a 5-day calendar, event details, and creating events — keyboard or mouse."""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Footer, Label, OptionList, Static
from textual.widgets.option_list import Option

from . import pixel as px
from .config import get_value, set_value
from .sources.base import ago, parse_time
from .widgets import TextBox, TextInput

DAYS = 5
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# ── parsing what you type ────────────────────────────────────────────────────


def parse_day(s: str, today: date | None = None) -> date | None:
    """today / tomorrow / fri / +2 / 11/7 / 2026-11-07 / nov 7"""
    today = today or date.today()
    s = s.strip().lower()
    if s in ("", "today", "tod", "now"):
        return today
    if s in ("tomorrow", "tmr", "tmrw", "tom"):
        return today + timedelta(days=1)
    if re.fullmatch(r"\+\d{1,3}", s):
        return today + timedelta(days=int(s[1:]))
    for i, name in enumerate(WEEKDAYS):
        if s.startswith(name[:3]) and len(s) <= 9:
            ahead = (i - today.weekday()) % 7
            return today + timedelta(days=ahead)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(*map(int, m.groups()))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
        out = date(y, mo, d)
        return out if out >= today or m.group(3) else date(y + 1, mo, d)
    m = re.fullmatch(r"([a-z]{3})[a-z]* (\d{1,2})", s)
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    if m and m.group(1) in months:
        out = date(today.year, months.index(m.group(1)) + 1, int(m.group(2)))
        return out if out >= today else date(today.year + 1, out.month, out.day)
    return None


def parse_clock(s: str) -> tuple[int, int] | None:
    """9 / 9:30 / 930 / 2pm / 2:15pm / 14:00 / noon"""
    s = s.strip().lower().replace(" ", "")
    if s in ("noon", "midday"):
        return 12, 0
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm|a|p)?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap and ap.startswith("p") and h < 12:
        h += 12
    if ap and ap.startswith("a") and h == 12:
        h = 0
    if not ap and 1 <= h <= 6:  # "3" during a workday means 3pm
        h += 12
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_duration(s: str) -> timedelta | None:
    """30 / 30m / 1h / 1h30 / 1.5h / 90m"""
    s = s.strip().lower().replace(" ", "")
    if not s:
        return timedelta(minutes=30)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)h(?:(\d+)m?)?", s)
    if m:
        return timedelta(hours=float(m.group(1)), minutes=int(m.group(2) or 0))
    m = re.fullmatch(r"(\d+)(?:m|min|mins)?", s)
    return timedelta(minutes=int(m.group(1))) if m else None


# ── event helpers ────────────────────────────────────────────────────────────


def _when(e: dict, key: str) -> datetime:
    raw = e[key]["dateTime"]
    if e.get("isAllDay"):  # all-day events are calendar dates, not instants: don't shift them by timezone
        return datetime.fromisoformat(raw[:19]).astimezone()
    return parse_time(raw).astimezone()


def ev_start(e: dict) -> datetime:
    return _when(e, "start")


def ev_end(e: dict) -> datetime:
    return _when(e, "end")


def _dur(a: datetime, b: datetime) -> str:
    m = int((b - a).total_seconds() // 60)
    return f"{m // 60}h{m % 60:02d}" if m >= 60 and m % 60 else (f"{m // 60}h" if m >= 60 else f"{m}m")


def place(e: dict) -> str:
    """A short 'where' for a row: meeting links become their service, Teams isn't said twice."""
    loc = ((e.get("location") or {}).get("displayName") or "").strip()
    low = loc.lower()
    if not loc or (e.get("isOnlineMeeting") and "teams" in low):
        return ""
    for needle, label in (("slack.com/huddle", "Slack huddle"), ("zoom.us", "Zoom"), ("meet.google", "Google Meet"),
                          ("teams.microsoft", "Teams"), ("webex", "Webex")):
        if needle in low:
            return label
    if low.startswith(("http://", "https://")):
        return re.sub(r"^https?://(www\.)?", "", loc).split("/")[0]
    return loc


def overlaps(events: list[dict], start: datetime, end: datetime) -> list[dict]:
    return [e for e in events if not e.get("isAllDay") and ev_start(e) < end and ev_end(e) > start
            and e.get("showAs") not in ("free",)]


CAL_CSS = f"""
AgendaScreen {{ background: {px.VOID}; }}
#cal-head {{ height: 2; background: {px.DEEP}; padding: 0 1; }}
#cal-days {{ height: 3fr; padding: 0 1; }}
.cal-day {{ width: 1fr; height: 1fr; margin-right: 1; }}
.cal-day > Label {{ width: 1fr; background: {px.PANEL}; color: {px.INK}; text-style: bold; padding: 0 1; }}
.cal-day.-today > Label {{ background: {px.HOTPINK}; color: {px.VOID}; }}
.cal-day OptionList {{ height: 1fr; background: {px.DEEP}; border: tall {px.SHADOW}; scrollbar-size-vertical: 1; }}
.cal-day OptionList:focus {{ border: tall {px.HOTPINK}; }}
.cal-day OptionList > .option-list--option-highlighted {{ background: #3a1670; }}
.cal-day OptionList:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
#cal-bottom {{ height: 2fr; margin: 1 1 0 1; border: panel {px.CYAN}; border-title-color: {px.VOID};
               border-title-style: bold; background: {px.DEEP}; }}
#cal-detail {{ height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }}
#cal-actions {{ height: 3; padding: 0 1; }}
#cal-actions Button {{ margin-right: 1; min-width: 12; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
#cal-actions Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
"""


class AgendaScreen(Screen):
    CSS = CAL_CSS
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("n", "new", "New event"),
        Binding("j", "join", "Join"),
        Binding("o", "open", "Open in Outlook"),
        Binding("c", "copy", "Copy link", show=False),
        Binding("w", "weekends", "Weekends on/off"),
        Binding("comma,less_than_sign", "shift(-5)", "◀ 5 days"),
        Binding("full_stop,greater_than_sign", "shift(5)", "5 days ▶"),
        Binding("t", "today", "Today"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("left", "column(-1)", show=False),
        Binding("right", "column(1)", show=False),
    ]

    def __init__(self, focus_id: str | None = None, prefill: dict | None = None) -> None:
        super().__init__()
        self.focus_id = focus_id
        self.prefill = prefill  # an agent's proposed event: open NEW EVENT filled in once the week loads
        self.day_offset = 0  # days from today
        self.events: list[dict] = []
        self.days: list[date] = []
        self.selected: dict | None = None
        self._bodies: dict[str, str] = {}

    @property
    def outlook(self):
        return self.app.outlook

    def _span(self) -> list[date]:
        skip = bool(get_value("agenda.skip_weekends"))
        d, out = date.today() + timedelta(days=self.day_offset), []
        while len(out) < DAYS:
            if not (skip and d.weekday() >= 5):
                out.append(d)
            d += timedelta(days=1)
        return out

    def compose(self) -> ComposeResult:
        yield Static(id="cal-head")
        with Horizontal(id="cal-days"):
            for i in range(DAYS):
                with Vertical(classes="cal-day", id=f"col-{i}"):
                    yield Label("", id=f"lbl-{i}")
                    yield OptionList(id=f"day-{i}")
        with Vertical(id="cal-bottom") as bottom:
            bottom.border_title = " EVENT "
            yield VerticalScroll(Static("", id="cal-detail-text"), id="cal-detail")
            with Horizontal(id="cal-actions"):
                yield Button("JOIN  j", id="b-join", variant="primary")
                yield Button("OPEN IN OUTLOOK  o", id="b-open")
                yield Button("COPY LINK  c", id="b-copy")
                yield Button("NEW EVENT  n", id="b-new")
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    # data ---------------------------------------------------------------------

    @work(exclusive=True, group="load")
    async def _load(self, select_id: str | None = None) -> None:
        self.days = self._span()
        head = Text()
        head.append(" ◷ AGENDA ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        head.append(f"  {self.days[0]:%a %d %b} – {self.days[-1]:%a %d %b}", Style(color=px.INK, bold=True))
        head.append("   loading…", Style(color=px.DIM))
        head.append("\n←/→ day · ↑/↓ event · n new event · j join · o open in Outlook · , / . move 5 days · "
                    "t today · w weekends · esc back", Style(color=px.DIM))
        self.query_one("#cal-head", Static).update(head)
        if not self.outlook:
            self._detail(Text("Connect Microsoft 365 in Settings (s) to see your calendar.", Style(color=px.YELLOW)))
            return
        start = datetime.combine(self.days[0], datetime.min.time()).astimezone()
        end = datetime.combine(self.days[-1] + timedelta(days=1), datetime.min.time()).astimezone()
        try:
            self.events = await self.outlook.calendar_view(start, end)
        except Exception as exc:
            self._detail(Text(f"✖ {exc}", Style(color=px.RED)))
            return
        head = Text(head.plain.split("   loading…")[0], Style(color=px.INK, bold=True))
        head.stylize(Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True), 0, 10)
        head.append(f"   {len(self.events)} events", Style(color=px.DIM))
        head.append("\n←/→ day · ↑/↓ event · n new event · j join · o open in Outlook · , / . move 5 days · "
                    "t today · w weekends · esc back", Style(color=px.DIM))
        self.query_one("#cal-head", Static).update(head)
        self._fill(select_id or self.focus_id)
        self.focus_id = None
        if self.prefill:
            prefill, self.prefill = self.prefill, None
            self.call_after_refresh(self.action_new, prefill)

    def _fill(self, select_id: str | None) -> None:
        now = datetime.now().astimezone()
        target: tuple[int, int] | None = None
        first_upcoming: tuple[int, int] | None = None
        for i, day in enumerate(self.days):
            col = self.query_one(f"#col-{i}")
            col.set_class(day == date.today(), "-today")
            label = f"{day:%a}".upper()
            if day == date.today():
                label = f"TODAY · {label}"
            elif day == date.today() + timedelta(days=1):
                label = f"TMRW · {label}"
            todays = sorted((e for e in self.events if ev_start(e).date() == day or
                             (e.get("isAllDay") and ev_start(e).date() <= day < ev_end(e).date())),
                            key=lambda e: (not e.get("isAllDay"), ev_start(e)))
            self.query_one(f"#lbl-{i}", Label).update(f"{label} {day:%d %b} · {len(todays)}")
            lst = self.query_one(f"#day-{i}", OptionList)
            opts = []
            for e in todays:
                opts.append(Option(self._row(e, now), id=f"{i}|{e['id']}"))
                if e["id"] == select_id:
                    target = (i, len(opts) - 1)
                if first_upcoming is None and not e.get("isAllDay") and ev_end(e) > now:
                    first_upcoming = (i, len(opts) - 1)
            if not opts:
                opts.append(Option(Text("— free —", Style(color=px.DIM, italic=True)), disabled=True))
            lst.set_options(opts)
        col, row = target or first_upcoming or (0, 0)
        lst = self.query_one(f"#day-{col}", OptionList)
        if lst.option_count and not lst.get_option_at_index(row).disabled:
            lst.highlighted = row
            lst.scroll_to_highlight()
        lst.focus()
        self._show_highlighted(lst)

    def _row(self, e: dict, now: datetime) -> Text:
        s, en = ev_start(e), ev_end(e)
        live = s <= now < en
        past = en <= now
        tentative = e.get("showAs") == "tentative" or (e.get("responseStatus") or {}).get("response") == "tentativelyAccepted"
        t = Text(no_wrap=True, overflow="ellipsis")
        if e.get("isAllDay"):
            t.append("ALL DAY ", Style(color=px.VOID, bgcolor=px.PURPLE, bold=True))
        else:
            t.append(f"{s:%H:%M} ", Style(color=px.HOTPINK if live else (px.DIM if past else px.CYAN), bold=True))
        t.append(("▶ " if live else "") + (e.get("subject") or "(busy)"),
                 Style(color=px.DIM if past else px.INK, bold=not past, italic=tentative))
        t.append("\n")
        bits = [] if e.get("isAllDay") else [_dur(s, en)]
        if e.get("isOnlineMeeting"):
            bits.append("Teams")
        if place(e):
            bits.append(place(e))
        if tentative:
            bits.append("tentative")
        t.append("   " + " · ".join(bits), Style(color=px.DIM))
        return t

    # selection / details --------------------------------------------------------

    def _event_for(self, lst: OptionList) -> dict | None:
        opt = lst.highlighted_option
        if not opt or not opt.id:
            return None
        eid = opt.id.split("|", 1)[1]
        return next((e for e in self.events if e["id"] == eid), None)

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, e: OptionList.OptionHighlighted) -> None:
        self._show_highlighted(e.option_list)

    @on(OptionList.OptionSelected)
    def _selected(self, e: OptionList.OptionSelected) -> None:
        self._show_highlighted(e.option_list)  # focus stays in the grid; j / o / c act on the selection

    def _show_highlighted(self, lst: OptionList) -> None:
        self.selected = self._event_for(lst)
        e = self.selected
        self.query_one("#b-join", Button).disabled = not self._join_url()
        self.query_one("#b-open", Button).disabled = not e
        self.query_one("#b-copy", Button).disabled = not (self._join_url() or (e or {}).get("webLink"))
        if not e:
            day = self.days[int(lst.id.split("-")[1])] if lst.id and self.days else date.today()
            self._detail(Text(f"Nothing on {day:%a %d %b}. Press n to schedule something.", Style(color=px.DIM)))
            return
        self._detail(self._details(e, self._bodies.get(e["id"])))
        if e["id"] not in self._bodies:
            self._fetch_body(e["id"])

    @work(exclusive=True, group="body")
    async def _fetch_body(self, eid: str) -> None:
        await asyncio.sleep(0.25)  # don't fetch bodies while you're scrolling past events
        try:
            d = await self.outlook._get(f"/me/events/{eid}", {"$select": "body"},
                                        headers={"Prefer": 'outlook.body-content-type="text"'})
        except Exception:
            return
        body = re.sub(r"\n{3,}", "\n\n", ((d.get("body") or {}).get("content") or "").strip())
        self._bodies[eid] = body
        if self.selected and self.selected["id"] == eid:
            self._detail(self._details(self.selected, body))

    def _details(self, e: dict, body: str | None) -> Text:
        s, en = ev_start(e), ev_end(e)
        t = Text()
        t.append((e.get("subject") or "(busy)") + "\n", Style(color=px.HOTPINK, bold=True))
        when = f"{s:%a %d %b} · all day" if e.get("isAllDay") else f"{s:%a %d %b · %H:%M}–{en:%H:%M} ({_dur(s, en)})"
        t.append(when, Style(color=px.INK, bold=True))
        t.append(f"   {ago(s)}\n", Style(color=px.DIM))
        loc = ((e.get("location") or {}).get("displayName") or "").strip()
        if loc or e.get("isOnlineMeeting"):
            t.append("where  ", Style(color=px.CYAN, bold=True))
            full = loc if len(loc) <= 90 else loc[:87] + "…"
            t.append(" · ".join(x for x in ("Teams" if e.get("isOnlineMeeting") else "",
                                            full if place(e) else "") if x) + "\n")
        org = ((e.get("organizer") or {}).get("emailAddress") or {})
        t.append("from   ", Style(color=px.CYAN, bold=True))
        t.append(("you" if e.get("isOrganizer") else (org.get("name") or org.get("address") or "?")) + "\n")
        resp = (e.get("responseStatus") or {}).get("response", "none")
        if not e.get("isOrganizer"):
            t.append("you    ", Style(color=px.CYAN, bold=True))
            t.append({"accepted": "accepted ✔", "tentativelyAccepted": "tentative ?", "declined": "declined ✖",
                      "notResponded": "haven't replied", "none": "—"}.get(resp, resp) + "\n")
        atts = e.get("attendees") or []
        if atts:
            mark = {"accepted": ("✔", px.CYAN), "declined": ("✖", px.RED), "tentativelyAccepted": ("?", px.YELLOW)}
            t.append(f"people ({len(atts)})  ", Style(color=px.CYAN, bold=True))
            for i, a in enumerate(atts[:16]):
                st = (a.get("status") or {}).get("response", "none")
                sym, col = mark.get(st, ("·", px.DIM))
                name = (a.get("emailAddress") or {}).get("name") or (a.get("emailAddress") or {}).get("address", "?")
                t.append(f"{sym} ", Style(color=col, bold=True))
                t.append(name + ("   " if i < len(atts[:16]) - 1 else ""), Style(color=px.INK))
            if len(atts) > 16:
                t.append(f"  +{len(atts) - 16} more", Style(color=px.DIM))
            t.append("\n")
        t.append("\n")
        text = body if body is not None else (e.get("bodyPreview") or "")
        t.append(text.strip() or "(no description)", Style(color=px.INK if text else px.DIM))
        return t

    def _detail(self, text: Text) -> None:
        self.query_one("#cal-detail-text", Static).update(text)
        self.query_one("#cal-detail", VerticalScroll).scroll_home(animate=False)

    def _join_url(self) -> str | None:
        return ((self.selected or {}).get("onlineMeeting") or {}).get("joinUrl")

    # keyboard -------------------------------------------------------------------

    def _current_col(self) -> int:
        f = self.focused
        if isinstance(f, OptionList) and f.id and f.id.startswith("day-"):
            return int(f.id.split("-")[1])
        return -1

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "column":
            return True if self._current_col() >= 0 else False  # ←/→ only steer the day grid
        return True

    def action_column(self, delta: int) -> None:
        cur = self._current_col()
        nxt = max(0, min(DAYS - 1, cur + delta))
        if nxt == cur:
            if 0 <= cur and (cur + delta) in (-1, DAYS):
                self.action_shift(delta)  # past the edge: move the window a day
            return
        here = self.query_one(f"#day-{cur}", OptionList).highlighted or 0
        lst = self.query_one(f"#day-{nxt}", OptionList)
        lst.focus()
        if lst.option_count and not lst.get_option_at_index(min(here, lst.option_count - 1)).disabled:
            lst.highlighted = min(here, lst.option_count - 1)
        self._show_highlighted(lst)

    def action_shift(self, days: int) -> None:
        self.day_offset += days
        self._load()

    def action_today(self) -> None:
        self.day_offset = 0
        self._load()

    def action_weekends(self) -> None:
        set_value("agenda.skip_weekends", not bool(get_value("agenda.skip_weekends")))
        self.notify("weekends " + ("hidden" if get_value("agenda.skip_weekends") else "shown"), timeout=2)
        self._load()

    def action_reload(self) -> None:
        self._bodies.clear()
        self._load(self.selected["id"] if self.selected else None)

    @on(Button.Pressed, "#b-join")
    def action_join(self) -> None:
        if url := self._join_url():
            subprocess.Popen(["open", url])

    @on(Button.Pressed, "#b-open")
    def action_open(self) -> None:
        if self.selected and self.selected.get("webLink"):
            subprocess.Popen(["open", self.selected["webLink"]])

    @on(Button.Pressed, "#b-copy")
    def action_copy(self) -> None:
        link = self._join_url() or (self.selected or {}).get("webLink")
        if link:
            subprocess.run(["pbcopy"], input=link.encode())
            self.notify(link[:80], title="COPIED", timeout=3)

    @on(Button.Pressed, "#b-new")
    def _new_btn(self) -> None:
        self.action_new()

    def action_new(self, prefill: dict | None = None) -> None:
        col = max(self._current_col(), 0)
        day = self.days[col] if self.days else date.today()

        def done(created: dict | None) -> None:
            if created:
                self.notify(created.get("subject", "event"), title="✔ ON YOUR CALENDAR", timeout=4)
                start = parse_time(created["start"]["dateTime"]).astimezone().date()
                if start not in self.days:  # jump the window to the new event
                    self.day_offset = (start - date.today()).days
                self._load(created.get("id"))

        self.app.push_screen(NewEventScreen(day, self.events, prefill), done)

    def action_back(self) -> None:
        self.dismiss()


# ── new event ────────────────────────────────────────────────────────────────

NEW_CSS = f"""
NewEventScreen {{ align: center middle; background: {px.VOID} 70%; }}
NewEventScreen > VerticalScroll {{ width: 96; max-width: 95%; height: auto; max-height: 92%; background: {px.DEEP};
                                   border: panel {px.HOTPINK}; border-title-color: {px.VOID}; border-title-style: bold;
                                   padding: 1 2; }}
NewEventScreen .row {{ height: auto; }}
NewEventScreen .cell {{ width: 1fr; height: auto; margin-right: 1; }}
NewEventScreen Label {{ color: {px.CYAN}; text-style: bold; margin-top: 1; }}
NewEventScreen Input {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
NewEventScreen Input:focus {{ border: tall {px.HOTPINK}; }}
NewEventScreen Input.-bad {{ border: tall {px.RED}; }}
NewEventScreen TextArea {{ height: 6; background: {px.PANEL}; border: tall {px.PURPLE}; }}
NewEventScreen TextArea:focus {{ border: tall {px.HOTPINK}; }}
NewEventScreen Checkbox {{ background: {px.DEEP}; border: none; margin-top: 1; }}
#ne-preview {{ margin-top: 1; height: auto; }}
#ne-buttons {{ height: auto; margin-top: 1; align-horizontal: right; }}
#ne-buttons Button {{ margin-left: 1; min-width: 14; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
#ne-buttons Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
"""


class NewEventScreen(ModalScreen[dict | None]):
    CSS = NEW_CSS
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "create", "Create")]

    def __init__(self, day: date, events: list[dict], prefill: dict | None = None) -> None:
        super().__init__()
        self.day, self.events = day, events
        self.prefill = prefill or {}
        self.armed = False
        self.plan: dict | None = None

    def _default_start(self) -> str:
        now = datetime.now()
        if self.day == now.date():
            t = now + timedelta(minutes=30 - now.minute % 30)
            start = t.replace(second=0, microsecond=0)
        else:
            start = datetime.combine(self.day, datetime.min.time()).replace(hour=9)
        for _ in range(20):  # skip past anything already booked
            s = start.astimezone()
            if not overlaps(self.events, s, s + timedelta(minutes=30)):
                break
            start += timedelta(minutes=30)
        return f"{start:%H:%M}"

    def compose(self) -> ComposeResult:
        with VerticalScroll() as box:
            box.border_title = " ✚ NEW EVENT "
            yield Label("TITLE")
            yield TextInput(placeholder="what is it?", id="ne-title")
            with Horizontal(classes="row"):
                with Vertical(classes="cell"):
                    yield Label("DAY")
                    yield TextInput(value=self.day.isoformat(), placeholder="today · tomorrow · fri · 11/14", id="ne-day")
                with Vertical(classes="cell"):
                    yield Label("START")
                    yield TextInput(value=self._default_start(), placeholder="9:30 · 2pm", id="ne-start")
                with Vertical(classes="cell"):
                    yield Label("LENGTH")
                    yield TextInput(value="30m", placeholder="30m · 1h · 1h30", id="ne-dur")
            yield Label("INVITE (emails, comma separated — leave empty for just you)")
            yield TextInput(placeholder="dana@example.com, sam@…", id="ne-people")
            yield Label("WHERE")
            yield TextInput(placeholder="room, address or leave empty", id="ne-where")
            yield Checkbox("Teams meeting (adds a join link)", value=False, id="ne-teams")
            yield Label("NOTES")
            yield TextBox(id="ne-notes", soft_wrap=True)
            yield Static("", id="ne-preview")
            with Horizontal(id="ne-buttons"):
                yield Button("CANCEL", id="ne-cancel")
                yield Button("CREATE  ctrl+s", id="ne-create", variant="primary")

    def on_mount(self) -> None:
        p = self.prefill
        for key, wid in (("title", "#ne-title"), ("day", "#ne-day"), ("start", "#ne-start"), ("length", "#ne-dur"),
                         ("invitees", "#ne-people"), ("where", "#ne-where")):
            if p.get(key):
                self.query_one(wid, TextInput).value = str(p[key])
        if p.get("notes"):
            self.query_one("#ne-notes", TextBox).load_text(str(p["notes"]))
        self.query_one("#ne-title", TextInput).focus()
        self._preview()

    @on(TextInput.Changed)
    @on(Checkbox.Changed)
    def _changed(self) -> None:
        self._disarm()
        self._preview()

    def _people(self) -> list[str]:
        raw = self.query_one("#ne-people", TextInput).value
        return [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]

    def _preview(self) -> None:
        """Parse as you type; show exactly what will be created and anything it clashes with."""
        out = Text()
        q = lambda i: self.query_one(i, TextInput)
        day = parse_day(q("#ne-day").value)
        clock = parse_clock(q("#ne-start").value)
        dur = parse_duration(q("#ne-dur").value)
        for wid, ok in (("#ne-day", day), ("#ne-start", clock), ("#ne-dur", dur)):
            q(wid).set_class(not ok, "-bad")
        people = self._people()
        bad_people = [p for p in people if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p)]
        q("#ne-people").set_class(bool(bad_people), "-bad")
        title = q("#ne-title").value.strip()
        if not (day and clock and dur):
            out.append("✖ ", Style(color=px.RED, bold=True))
            out.append("can't read the " + ", ".join(n for n, ok in (("day", day), ("start", clock), ("length", dur))
                                                        if not ok), Style(color=px.RED))
            self.plan = None
        else:
            start = datetime.combine(day, datetime.min.time()).replace(hour=clock[0], minute=clock[1]).astimezone()
            end = start + dur
            teams = self.query_one("#ne-teams", Checkbox).value
            out.append(" WILL CREATE ", Style(color=px.VOID, bgcolor=px.CYAN, bold=True))
            out.append(f" {title or '(no title)'}", Style(color=px.INK, bold=True))
            out.append(f"\n{start:%a %d %b · %H:%M}–{end:%H:%M} ({_dur(start, end)})", Style(color=px.INK))
            extra = []
            if people:
                extra.append(f"invites {len(people)}: {', '.join(people[:4])}{'…' if len(people) > 4 else ''}")
            if teams:
                extra.append("Teams link")
            loc = q("#ne-where").value.strip()
            if loc:
                extra.append(loc)
            if extra:
                out.append("  ·  " + "  ·  ".join(extra), Style(color=px.DIM))
            if start < datetime.now().astimezone():
                out.append("\n⚠ that's in the past", Style(color=px.YELLOW))
            clash = overlaps(self.events, start, end)
            if clash:
                out.append("\n⚠ overlaps: " + ", ".join(f"{ev_start(c):%H:%M} {c.get('subject', '')}" for c in clash[:3]),
                           Style(color=px.YELLOW))
            if bad_people:
                out.append(f"\n✖ not an email: {', '.join(bad_people)}", Style(color=px.RED))
            self.plan = None if bad_people or not title else {
                "subject": title,
                "start": {"dateTime": start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end.astimezone(timezone.utc).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
                "attendees": [{"emailAddress": {"address": p}, "type": "required"} for p in people],
                **({"location": {"displayName": loc}} if loc else {}),
                **({"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"} if teams else {}),
                "body": {"contentType": "text", "content": self.query_one("#ne-notes", TextBox).text},
            }
            if not title:
                out.append("\n✖ needs a title", Style(color=px.RED))
        self.query_one("#ne-preview", Static).update(out)

    def _disarm(self) -> None:
        if self.armed:
            self.armed = False
            self.query_one("#ne-create", Button).label = "CREATE  ctrl+s"

    @on(Button.Pressed, "#ne-create")
    def action_create(self) -> None:
        self._preview()
        if not self.plan:
            self.notify("fix the red fields first", severity="warning", timeout=3)
            return
        people = self.plan["attendees"]
        if people and not self.armed:  # invites go out the moment it's created: ask once more
            self.armed = True
            self.query_one("#ne-create", Button).label = f"PRESS AGAIN · INVITES {len(people)}"
            return
        self._create(dict(self.plan))

    @work(exclusive=True)
    async def _create(self, plan: dict) -> None:
        from .sources.mail import NeedsConsent

        btn = self.query_one("#ne-create", Button)
        btn.disabled = True
        btn.label = "CREATING…"
        try:
            created = await self.app.outlook.create_event(plan)
        except NeedsConsent:
            from .setup import OutlookScreen

            btn.disabled = False
            btn.label = "CREATE  ctrl+s"
            self.armed = False
            self.notify("Creating events needs one more Microsoft permission. Approve it, then press CREATE again.",
                        title="CALENDAR PERMISSION", timeout=8)
            self.app.push_screen(OutlookScreen(extra_scopes=["Calendars.ReadWrite"]))
            return
        except Exception as exc:
            btn.disabled = False
            btn.label = "CREATE  ctrl+s"
            self.armed = False
            self.notify(str(exc)[:200], title="✖ NOT CREATED", severity="error", timeout=8)
            return
        self.dismiss(created)

    @on(Button.Pressed, "#ne-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
