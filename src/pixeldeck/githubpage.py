"""The GitHub page (press the review queue, my PRs or inbox tile's number twice): your pull requests, counted.

Cards: REVIEWS (PRs waiting on you, longest wait first), MY PRS (your open PRs, the ones that need you first),
INBOX (notifications; the number is unread), then the PRs you OPENED, MERGED and REVIEWED in the period.
On the INBOX card, e marks the highlighted notification read on GitHub.
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import datetime, timedelta, timezone

from rich.style import Style
from rich.text import Text
from textual import work
from textual.binding import Binding

from . import pixel as px
from .pages import Card, Group, SectionScreen, heading, hbar, kv, spark
from .sources.base import Feed, Item, ago, parse_time
from .sources.github import REASON, ci_state, mine_item, queue_item

PERIODS = [7, 30, 90]
STATE_ICON = {"MERGED": ("◆", "green"), "CLOSED": ("✕", "muted"), "DRAFT": ("◇", "muted")}
TILES = {"reviews": "reviews", "mine": "mine", "inbox": "notifs"}  # card → the dashboard tile it stands for
DIRECT = {"review_requested", "mention", "assign", "security_alert"}  # someone wants you, by name
PARTICIPATING = {"author", "comment", "team_mention"}
WAIT_BUCKETS = [("UNDER 1 DAY", 1, "CYAN"), ("1–3 DAYS", 3, "VIOLET"), ("3–7 DAYS", 7, "YELLOW"),
                ("OVER A WEEK", 10**6, "RED")]  # label, upper bound in days, palette slot (read when drawn)


def _days(since: str | None) -> float:
    at = parse_time(since)
    return (datetime.now(timezone.utc) - at).total_seconds() / 86400 if at else 0.0


def _ts(value: str | None) -> float:
    at = parse_time(value)
    return at.timestamp() if at else 0.0


def _bucket(days: float) -> int:
    """Which WAIT_BUCKETS row a wait of `days` falls in."""
    return next(i for i, (_, top, _) in enumerate(WAIT_BUCKETS) if days < top)


def _span(days: float) -> str:
    if days < 1 / 24:
        return f"{days * 1440:.0f}m"
    return f"{days * 24:.0f}h" if days < 1 else f"{days:.1f}d" if days < 10 else f"{days:.0f}d"


def health(pr: dict) -> str:
    """Where one of your open PRs stands: draft, attention (CI failing, conflict, changes asked), approved, waiting."""
    if pr["isDraft"]:
        return "draft"
    if (ci_state(pr) in ("FAILURE", "ERROR") or pr["mergeable"] == "CONFLICTING"
            or pr.get("reviewDecision") == "CHANGES_REQUESTED"):
        return "attention"
    return "approved" if pr.get("reviewDecision") == "APPROVED" else "waiting"


class GitHubPage(SectionScreen):
    NAME = "GITHUB"
    REFRESH = 300.0
    SIDE_TITLE = "PULL REQUESTS"
    STATE = ("data", "inbox", "days", "read_here")

    BINDINGS = [
        *SectionScreen.BINDINGS,
        Binding("t", "period", "Period 7/30/90d"),
        Binding("e", "mark_read", "Mark read"),
    ]

    def __init__(self, card: str | None = None) -> None:
        super().__init__(card)
        self.TILE = TILES.get(card or "", "reviews")
        self.days = 30
        self.data: dict | None = None
        self.inbox: Feed | None = None
        self.read_here: set[str] = set()  # threads marked read on this page (the API can lag a moment)

    def note(self) -> str:
        return f"last {self.days} days · t changes the period"

    def skeleton(self) -> list[Card]:
        return [
            Card("reviews", "REVIEWS", tone="accent", empty="no one is waiting on you"),
            Card("mine", "MY PRS", tone="secondary", empty="you have no open pull requests"),
            Card("inbox", "INBOX", tone="warning", empty="inbox zero"),
            Card("opened", "OPENED", tone="primary", empty="you opened nothing in this period"),
            Card("merged", "MERGED", tone="green", empty="nothing merged in this period"),
            Card("reviewed", "REVIEWED", tone="secondary", empty="you reviewed nothing in this period"),
        ]

    async def fetch(self) -> list[Card]:
        gh = self.app.gh
        data, inbox = await asyncio.gather(gh.overview(tuple(PERIODS)), gh.notifications(limit=50),
                                           return_exceptions=True)
        if isinstance(data, BaseException):
            raise data
        self.data = data
        if not isinstance(inbox, BaseException):  # the inbox is extra: never lose the page over it
            for it in inbox.items:
                if it.unread and (it.ref or {}).get("thread") in self.read_here:
                    self._set_read(it)
            inbox.count = sum(1 for it in inbox.items if it.unread)
            self.inbox = inbox
        return self._cards()

    # cards ------------------------------------------------------------------------

    def _since(self) -> float:
        return (datetime.now(timezone.utc) - timedelta(days=self.days)).timestamp()

    @staticmethod
    def _done_row(n: dict, tail: str, author: bool) -> Item:
        state = "DRAFT" if n["isDraft"] and n["state"] == "OPEN" else n["state"]
        icon, tone = STATE_ICON.get(state, ("◆", "secondary"))
        who = (n.get("author") or {}).get("login")
        parts = [n["repository"]["nameWithOwner"].split("/")[-1], *([f"@{who}"] if author and who else []),
                 state.lower(), f"+{n['additions']}/-{n['deletions']}", tail]
        return Item(title=f"#{n['number']} {n['title']}", meta=" · ".join(parts), url=n["url"],
                    ref={"kind": "pr", "url": n["url"]}, icon=icon, tone=tone, when=parse_time(n.get("updatedAt")))

    def _cards(self) -> list[Card]:
        d = self.data
        cards = self.skeleton()
        by = {c.key: c for c in cards}
        if self.inbox is not None:
            self._inbox_card(by["inbox"])
        if d is None:
            return cards

        queue = sorted(d["queue"], key=lambda pr: (pr["isDraft"], pr["askedAt"] or ""))  # longest wait first
        rows: list[Item | Group] = []
        for i, (name, _, _) in reversed(list(enumerate(WAIT_BUCKETS))):  # longest wait first
            group = [pr for pr in queue if not pr["isDraft"] and _bucket(_days(pr["askedAt"])) == i]
            if group:
                rows.append(Group(f"WAITING {name} · {len(group)}", ["muted", "primary", "warning", "error"][i]))
                rows += [self._queue_row(pr) for pr in group]
        drafts = [pr for pr in queue if pr["isDraft"]]
        if drafts:
            rows.append(Group(f"DRAFTS · {len(drafts)}"))
            rows += [self._queue_row(pr) for pr in drafts]
        c = by["reviews"]
        c.value, c.rows = str(len(queue)), rows
        live = [pr for pr in queue if not pr["isDraft"]]
        c.hint = f"oldest {_span(_days(live[0]['askedAt']))}" if live else "waiting on you"

        groups = {"attention": [], "approved": [], "waiting": [], "draft": []}
        for pr in d["mine"]:
            groups[health(pr)].append(pr)
        rows = []
        for key, title, tone in (("attention", "NEEDS ATTENTION", "error"), ("approved", "APPROVED", "green"),
                                 ("waiting", "WAITING FOR REVIEW", "secondary"), ("draft", "DRAFTS", "muted")):
            if groups[key]:
                rows.append(Group(f"{title} · {len(groups[key])}", tone))
                rows += [mine_item(pr) for pr in groups[key]]
        c = by["mine"]
        c.value, c.rows = str(len(d["mine"])), rows
        c.hint = f"{len(groups['attention'])} need you · {len(groups['approved'])} approved"

        since = self._since()
        for key, verb in (("opened", "opened"), ("merged", "merged"), ("reviewed", "you")):
            k = d[key]
            nodes = [n for n in k["nodes"] if _ts(n.get(k["field"])) >= since]
            nodes.sort(key=lambda n: n.get(k["field"]) or "", reverse=True)
            total = k["count"].get(self.days, len(nodes))
            c = by[key]
            c.value = str(total)
            c.rows = [Group(f"LATEST {len(nodes)} OF {total}")] if total > len(nodes) else []
            c.rows += [self._done_row(n, (f"you {' + '.join(n.get('how') or ['reviewed'])} · " if key == "reviewed"
                                          else f"{verb} ") + ago(parse_time(n.get(k["field"]))), key == "reviewed")
                       for n in nodes]
            if key == "merged" and (spans := self._merge_times(nodes)):
                c.hint = f"median {_span(statistics.median(spans))} to merge"
            else:
                c.hint = {"opened": "by you", "merged": "yours", "reviewed": "others' PRs"}[key] + f" · {self.days}d"
            if key == "reviewed" and nodes:
                did = {v: sum(1 for n in nodes if v in (n.get("how") or [])) for v in ("merged", "reviewed", "commented")}
                c.hint = f"{did['merged']} merged · {did['commented']} commented"  # the period is in the header
        return cards

    def _queue_row(self, pr: dict) -> Item:
        it = queue_item(pr, pr["why"])
        it.meta = it.meta.rsplit(" · ", 1)[0] + f" · waiting {_span(_days(pr['askedAt']))}"
        return it

    @staticmethod
    def _merge_times(nodes: list[dict]) -> list[float]:
        out = []
        for n in nodes:
            a, b = parse_time(n.get("createdAt")), parse_time(n.get("mergedAt"))
            if a and b:
                out.append((b - a).total_seconds() / 86400)
        return out

    def _inbox_card(self, c: Card) -> None:
        items = self.inbox.items
        rank = lambda it: 0 if it.ref["reason"] in DIRECT else 1 if it.ref["reason"] in PARTICIPATING else 2
        rows: list[Item | Group] = []
        for n, title, tone in ((0, "FOR YOU", "accent"), (1, "PARTICIPATING", "secondary"), (2, "WATCHING", "muted")):
            group = sorted((it for it in items if rank(it) == n),
                           key=lambda it: (not it.unread, -(it.when.timestamp() if it.when else 0)))
            if group:
                rows.append(Group(f"{title} · {len(group)}", tone))
                rows += group
        c.rows = rows
        self._inbox_count(c)

    def _inbox_count(self, c: Card) -> None:
        items = self.inbox.items
        c.value = str(self.inbox.count or 0)
        c.hint = f"unread · {sum(1 for it in items if it.unread and it.ref['reason'] in DIRECT)} for you"

    # side panel -------------------------------------------------------------------

    def side(self, width: int) -> Text | None:
        d = self.data
        if d is None:
            return None
        out = Text()
        mine = d["mine"]
        out.append_text(heading(f"MY OPEN PRS · {len(mine)}"))
        flags = [
            ("CI FAILING", sum(1 for pr in mine if ci_state(pr) in ("FAILURE", "ERROR")), px.RED),
            ("CONFLICTS", sum(1 for pr in mine if pr["mergeable"] == "CONFLICTING"), px.RED),
            ("CHANGES REQ", sum(1 for pr in mine if pr.get("reviewDecision") == "CHANGES_REQUESTED"), px.YELLOW),
            ("APPROVED", sum(1 for pr in mine if pr.get("reviewDecision") == "APPROVED"), px.GREEN),
            ("DRAFTS", sum(1 for pr in mine if pr["isDraft"]), px.DIM),
        ]
        peak = max((n for _, n, _ in flags), default=0)
        for label, n, color in flags:
            out.append_text(hbar(label, n, peak, width, color))
        if stale := sum(1 for pr in mine if _days(pr["updatedAt"]) > 7):
            out.append_text(kv("quiet 7d+", f"{stale} not touched in a week", px.YELLOW))

        live = sorted((pr for pr in d["queue"] if not pr["isDraft"]), key=lambda pr: pr["askedAt"] or "")
        out.append("\n")
        out.append_text(heading(f"WAITING ON YOU · {len(live)}"))
        if live:
            waits = [sum(1 for pr in live if _bucket(_days(pr["askedAt"])) == i) for i in range(len(WAIT_BUCKETS))]
            for (label, _, slot), n in zip(WAIT_BUCKETS, waits):
                out.append_text(hbar(label, n, max(waits), width, getattr(px, slot)))
            first = live[0]
            repo = first["repository"]["nameWithOwner"].split("/")[-1]
            text = f"{_span(_days(first['askedAt']))} · #{first['number']} {repo}"
            out.append_text(kv("oldest", text[:width - 15], px.RED if _days(first["askedAt"]) > 7 else px.INK))
            repos: dict[str, int] = {}
            for pr in live:
                name = pr["repository"]["nameWithOwner"].split("/")[-1]
                repos[name] = repos.get(name, 0) + 1
            ranked = sorted(repos.items(), key=lambda r: -r[1])[:5]
            out.append_text(Text("by repo\n", Style(color=px.DIM)))
            for name, n in ranked:
                out.append_text(hbar(name.upper(), n, ranked[0][1], width, px.HOTPINK, label_w=16))
        else:
            out.append_text(Text("no one is waiting on you\n", Style(color=px.DIM)))

        if self.inbox is not None and self.inbox.items:
            reasons: dict[str, int] = {}
            for it in self.inbox.items:
                if it.unread:
                    reasons[it.ref["reason"]] = reasons.get(it.ref["reason"], 0) + 1
            ranked = sorted(reasons.items(), key=lambda r: (-r[1], r[0]))[:6]
            out.append("\n")
            out.append_text(heading(f"INBOX BY REASON · {self.inbox.count or 0} unread"))
            for reason, n in ranked:
                label, tone = REASON.get(reason, (reason.upper(), "muted"))
                out.append_text(hbar(label, n, ranked[0][1], width, px.TONE.get(tone, px.VIOLET)))

        weekly = d.get("weekly") or {}
        if weekly:
            top = max((v for s in weekly.values() for v in s), default=0)
            out.append("\n")
            out.append_text(heading(f"LAST {len(next(iter(weekly.values())))} WEEKS"))
            if "opened" in weekly:
                out.append_text(spark("opened", weekly["opened"], px.VIOLET, top))
            if "merged" in weekly:
                out.append_text(spark("merged", weekly["merged"], px.GREEN, top))
            if "reviewed" in weekly:
                out.append_text(spark("reviewed", weekly["reviewed"], px.CYAN, top))
        since = self._since()
        merged = [n for n in d["merged"]["nodes"] if _ts(n.get("mergedAt")) >= since]
        if spans := self._merge_times(merged):
            out.append_text(kv("time to merge", f"median {_span(statistics.median(spans))} · {self.days}d"))
        rev = [n for n in d["reviewed"]["nodes"] if _ts(n.get("updatedAt")) >= since]
        did = {v: sum(1 for n in rev if v in (n.get("how") or [])) for v in ("merged", "reviewed", "commented")}
        out.append_text(kv("reviewed", f"{len(rev)} others' PRs · {self.days}d"))
        out.append_text(kv("  merged", str(did["merged"])))
        out.append_text(kv("  commented", str(did["commented"])))
        out.append_text(kv("  formal review", str(did["reviewed"])))
        return out

    # actions ----------------------------------------------------------------------

    def pick_card(self, key: str) -> None:
        super().pick_card(key)
        self.refresh_bindings()  # e only shows on the INBOX card

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "mark_read" and self.card_key != "inbox":
            return False
        return super().check_action(action, parameters)

    def action_period(self) -> None:
        self.days = PERIODS[(PERIODS.index(self.days) + 1) % len(PERIODS)]
        self.set_cards(self._cards())
        self._draw_head()

    def action_mark_read(self) -> None:
        item = self.current()
        if self.card_key != "inbox" or item is None:
            return
        if not item.unread:
            self.notify("already read", title="INBOX", timeout=2)
            return
        thread = (item.ref or {}).get("thread")
        if not thread:
            self.notify("this notification has no thread id; reload and try again", title="INBOX", timeout=4)
            return
        self._mark_read(item, thread)

    @work(group="pg-mark-read")
    async def _mark_read(self, item: Item, thread: str) -> None:
        try:
            await self.app.gh.mark_read(thread)
        except Exception as exc:
            self.notify(str(exc)[:200], title="✖ COULDN'T MARK READ", severity="error", timeout=6)
            return
        self.read_here.add(thread)
        if self.gone():
            return
        if item.unread and self.inbox is not None:
            self._set_read(item)
            self.inbox.count = max((self.inbox.count or 0) - 1, 0)
            self._inbox_count(next(c for c in self.cards if c.key == "inbox"))
            self._paint_cards()
            self.redraw_row(item)
            self.draw_side()
        self.notify(item.title, title="✔ MARKED READ", timeout=3)

    @staticmethod
    def _set_read(item: Item) -> None:
        item.unread = False
        item.icon = "○"
