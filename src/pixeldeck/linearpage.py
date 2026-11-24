"""The Linear page (press the Linear tile's number twice): what's on your plate, counted.

Cards: OUTSTANDING (open work, urgent first), NEW (assigned to you and not opened yet), then what you
were ASSIGNED, CREATED and CLOSED in the period, and your BACKLOG. Picking a card lists its tickets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual.binding import Binding

from . import pixel as px
from .pages import Card, Group, SectionScreen, heading, hbar, kv, spark
from .sources.base import Item, ago, parse_time
from .sources.chat import PRIORITY_NAMES, STATE_ORDER, issue_item

PERIODS = [7, 30, 90]
FETCH_DAYS = 90  # one fetch covers every period and the 8-week chart
PRIORITY_TONE = {1: "error", 2: "accent", 3: "secondary", 4: "primary", 0: "muted"}
PRIORITY_COLOR = {1: px.RED, 2: px.HOTPINK, 3: px.CYAN, 4: px.VIOLET, 0: px.DIM}


def _prio(n: dict) -> int:
    return n["priority"] or 9  # no priority sorts last


class LinearPage(SectionScreen):
    NAME = "LINEAR"
    TILE = "linear"
    REFRESH = 300.0
    SIDE_TITLE = "WORKLOAD"

    BINDINGS = [
        *SectionScreen.BINDINGS,
        Binding("t", "period", "Period 7/30/90d"),
        Binding("s", "search", "Search Linear"),
    ]

    def __init__(self, card: str | None = None) -> None:
        super().__init__(card)
        self.days = 30
        self.stats: dict | None = None
        self.fresh: list[dict] = []  # assigned to me lately and not opened yet
        self.team_backlog: tuple[int, bool] | None = None

    def note(self) -> str:
        return f"last {self.days} days · t changes the period"

    def skeleton(self) -> list[Card]:
        return [
            Card("outstanding", "OUTSTANDING", tone="accent", empty="nothing outstanding"),
            Card("new", "NEW", tone="warning", empty="nothing new assigned to you"),
            Card("assigned", "ASSIGNED", tone="secondary", empty="nothing assigned in this period"),
            Card("created", "CREATED", tone="primary", empty="you created nothing in this period"),
            Card("closed", "CLOSED", tone="green", empty="nothing closed in this period"),
            Card("backlog", "BACKLOG", tone="primary", empty="your backlog is empty"),
        ]

    async def fetch(self) -> list[Card]:
        linear = self.app.linear
        self.stats = await linear.stats(FETCH_DAYS)
        try:
            self.fresh = await linear.newly_assigned(self.app.linear_state.ack)
        except Exception:  # the NEW card is extra: never lose the page over it
            self.fresh = []
        try:
            self.team_backlog = await linear.team_backlog()
        except Exception:
            self.team_backlog = None
        return self._cards()

    # cards ------------------------------------------------------------------------

    def _since(self) -> float:
        return (datetime.now(timezone.utc) - timedelta(days=self.days)).timestamp()

    @staticmethod
    def _row(n: dict, tail: str, show_assignee: bool = False) -> Item:
        it = issue_item(n, show_assignee=show_assignee)
        it.meta = it.meta.rsplit(" · ", 1)[0] + f" · {tail}"
        return it

    def _cards(self) -> list[Card]:
        s = self.stats
        cards = self.skeleton()
        if s is None:
            return cards
        by = {c.key: c for c in cards}
        since = self._since()
        plus = lambda key: "+" if s["capped"].get(key) else ""
        acked = self.app.linear_state.ack

        active = [n for n in s["open"] if n["state"]["type"] != "backlog"]
        active.sort(key=lambda n: (_prio(n), STATE_ORDER.get(n["state"]["type"], 9), n.get("dueDate") or "9999",
                                   n["updatedAt"][::-1]))
        rows: list[Item | Group] = []
        counts = {p: sum(1 for n in active if (n["priority"] or 0) == p) for p, _ in PRIORITY_NAMES}
        for p, name in PRIORITY_NAMES:  # urgent → low, then no priority
            if counts[p]:
                rows.append(Group(f"{name.upper()} · {counts[p]}", PRIORITY_TONE[p]))
                rows += [issue_item(n, show_assignee=False) for n in active if (n["priority"] or 0) == p]
        c = by["outstanding"]
        c.value, c.rows = f"{len(active)}{plus('open')}", rows
        c.hint = f"{counts[1]} urgent · {counts[2]} high"

        fresh = [n for n in self.fresh if n["assignedAt"] > acked.get(n["identifier"], 0)]
        c = by["new"]
        c.value, c.hint = str(len(fresh)), "not opened yet"
        c.rows = []
        for n in fresh:
            it = self._row(n, "assigned " + ago(datetime.fromtimestamp(n["assignedAt"], timezone.utc)))
            it.flash = True
            it.ref["assigned_at"] = n["assignedAt"]
            c.rows.append(it)

        got = [n for n in s["assigned"] if n["assignedAt"] >= since]
        c = by["assigned"]
        c.value, c.hint = f"{len(got)}{plus('assigned')}", f"to you · {self.days}d"
        c.rows = [self._row(n, "assigned " + ago(datetime.fromtimestamp(n["assignedAt"], timezone.utc))) for n in got]

        made = [n for n in s["created"] if (parse_time(n["createdAt"]) or s["since"]).timestamp() >= since]
        made.sort(key=lambda n: n["createdAt"], reverse=True)
        c = by["created"]
        c.value, c.hint = f"{len(made)}{plus('created')}", f"by you · {self.days}d"
        c.rows = [self._row(n, "created " + ago(parse_time(n["createdAt"])), show_assignee=True) for n in made]

        done = [n for n in s["closed"] if (parse_time(n.get("completedAt")) or s["since"]).timestamp() >= since]
        done.sort(key=lambda n: n.get("completedAt") or "", reverse=True)
        c = by["closed"]
        c.value, c.hint = f"{len(done)}{plus('closed')}", f"yours · {self.days}d"
        c.rows = [self._row(n, "closed " + ago(parse_time(n.get("completedAt")))) for n in done]

        parked = [n for n in s["open"] if n["state"]["type"] == "backlog"]
        parked.sort(key=lambda n: (_prio(n), n["updatedAt"][::-1]))
        c = by["backlog"]
        c.value = f"{len(parked)}{plus('open')}"
        c.rows = [issue_item(n, show_assignee=False) for n in parked]
        if self.team_backlog:
            n, more = self.team_backlog
            c.hint = f"team {n}{'+' if more else ''}"
        else:
            c.hint = "yours"
        return cards

    # side panel -------------------------------------------------------------------

    def side(self, width: int) -> Text | None:
        s = self.stats
        if s is None:
            return None
        out = Text()
        active = [n for n in s["open"] if n["state"]["type"] != "backlog"]
        counts = {p: sum(1 for n in active if (n["priority"] or 0) == p) for p, _ in PRIORITY_NAMES}
        peak = max(counts.values(), default=0)
        out.append_text(heading("OUTSTANDING BY PRIORITY"))
        for p, name in PRIORITY_NAMES:
            out.append_text(hbar(name.upper(), counts[p], peak, width, PRIORITY_COLOR[p]))

        states: dict[str, list] = {}
        for n in active:
            states.setdefault(n["state"]["name"], [n["state"]["type"], 0])[1] += 1
        ranked = sorted(states.items(), key=lambda kv_: (STATE_ORDER.get(kv_[1][0], 9), -kv_[1][1]))[:6]
        out.append("\n")
        out.append_text(heading("BY STATUS"))
        speak = max((v[1] for _, v in ranked), default=0)
        tone = {"started": px.HOTPINK, "unstarted": px.CYAN, "triage": px.YELLOW}
        for name, (kind, count) in ranked:
            out.append_text(hbar(name.upper(), count, speak, width, tone.get(kind, px.VIOLET)))

        now = datetime.now(timezone.utc)
        weeks = 8

        def buckets(nodes: list[dict], field: str) -> list[int]:
            out_ = [0] * weeks
            for n in nodes:
                at = parse_time(n.get(field))
                if at:
                    back = int((now - at).total_seconds() // (7 * 86400))
                    if 0 <= back < weeks:
                        out_[weeks - 1 - back] += 1
            return out_

        made, done = buckets(s["created"], "createdAt"), buckets(s["closed"], "completedAt")
        top = max(made + done, default=0)
        out.append("\n")
        out.append_text(heading(f"LAST {weeks} WEEKS"))
        out.append_text(spark("created", made, px.VIOLET, top))
        out.append_text(spark("closed", done, px.GREEN, top))
        net = sum(made) - sum(done)
        out.append_text(kv("net", f"{net:+d}  " + ("created more than you closed" if net > 0 else
                                                  "closed more than you created" if net < 0 else "even"),
                           px.YELLOW if net > 0 else px.GREEN, label_w=8))

        today = now.astimezone().date().isoformat()
        week = (now.astimezone().date() + timedelta(days=7)).isoformat()
        due = [n for n in s["open"] if n.get("dueDate")]
        late = sum(1 for n in due if n["dueDate"] < today)
        soon = sum(1 for n in due if today <= n["dueDate"] <= week)
        oldest = min(active, key=lambda n: n["createdAt"], default=None)
        out.append("\n")
        out.append_text(heading("DATES"))
        out.append_text(kv("overdue", str(late), px.RED if late else px.DIM))
        out.append_text(kv("due in 7 days", str(soon), px.YELLOW if soon else px.DIM))
        if oldest:
            age = ago(parse_time(oldest["createdAt"])).removesuffix(" ago")
            out.append_text(kv("oldest open", f"{age} · {oldest['identifier']}"))
        if self.team_backlog:
            n, more = self.team_backlog
            out.append_text(kv("team backlog", f"{n}{'+' if more else ''}"))
        return out

    # actions ----------------------------------------------------------------------

    def after_view(self, item: Item | None) -> None:
        if self.stats is not None:  # opening a NEW ticket takes it off the NEW card
            self.set_cards(self._cards())

    def action_period(self) -> None:
        self.days = PERIODS[(PERIODS.index(self.days) + 1) % len(PERIODS)]
        self.set_cards(self._cards())
        self._draw_head()

    def action_search(self) -> None:
        self.app.action_linear_search()
