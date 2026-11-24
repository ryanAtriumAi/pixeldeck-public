"""The CI page (press the CI PIPELINE tile's number twice): is the build healthy, and what is broken now.

Cards: FAILING (workflows whose latest run failed), RUNNING (in progress or queued), RECENT (every run in
the window, by day), PASS RATE (a number only) and, when two or three repos are watched, one board per
repo (the latest run of each workflow). The window is the last 50 runs per repo on the watched branch.
Read-only: nothing here re-runs or cancels a run.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from rich.text import Text

from . import pixel as px
from .pages import Card, Group, SectionScreen, heading, hbar, kv, spark
from .sources.base import Item, ago

PER_REPO = 50
FAILED = {"failure", "timed_out", "startup_failure", "action_required"}
DECISIVE = FAILED | {"success"}  # cancelled / skipped / neutral runs say nothing about health
MAX_REPO_CARDS = 3


def _look(run: dict) -> tuple[str, str]:
    """Icon and tone, as the CI tile picks them."""
    if run["status"] != "completed":
        return "◷", "warning"
    if run["conclusion"] == "success":
        return "✔", "success"
    if run["conclusion"] in ("skipped", "cancelled", "neutral"):
        return "◌", "muted"
    return "✖", "error"


def _secs(run: dict) -> float | None:
    if run["status"] != "completed" or not run["started"] or not run["updated"]:
        return None
    return max((run["updated"] - run["started"]).total_seconds(), 0)


def _dur(secs: float | None) -> str:
    if secs is None:
        return ""
    s = int(secs)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _pct(runs: list[dict]) -> int | None:
    done = [r for r in runs if r["conclusion"] in DECISIVE]
    return round(100 * sum(r["conclusion"] == "success" for r in done) / len(done)) if done else None


def _rate_tone(pct: int | None) -> str:
    return "muted" if pct is None else "green" if pct >= 90 else "warning" if pct >= 70 else "error"


class CIPage(SectionScreen):
    NAME = "CI"
    TILE = "ci"
    REFRESH = 90.0
    SIDE_TITLE = "BUILD HEALTH"

    def __init__(self, card: str | None = None) -> None:
        super().__init__(card)
        self.runs: list[dict] | None = None
        self.errors: dict[str, str] = {}

    def _repos(self) -> list[str]:
        return list(dict.fromkeys(r["repo"] for r in self.runs or []))

    def note(self) -> str:
        branch = self.app.gh.cfg.get("ci_branch", "main")
        if not self.runs:
            return f"on {branch}"
        oldest = min((r["created"] for r in self.runs if r["created"]), default=None)
        return f"on {branch} · last {PER_REPO} runs per repo" + (f" · back to {ago(oldest)}" if oldest else "")

    def skeleton(self) -> list[Card]:
        cards = [
            Card("failing", "FAILING", tone="error", empty="every workflow's latest run passed"),
            Card("running", "RUNNING", tone="warning", empty="nothing running"),
            Card("recent", "RECENT", tone="primary", empty="no runs in the window"),
            Card("rate", "PASS RATE", rows=None),
        ]
        repos = self._repos()
        if 1 < len(repos) <= MAX_REPO_CARDS:
            cards += [Card(f"repo:{r}", r.split("/")[-1].upper()[:16], tone="secondary", empty="no runs")
                      for r in repos]
        return cards

    async def fetch(self) -> list[Card]:
        self.runs, self.errors = await self.app.gh.ci_history(PER_REPO)
        return self._cards()

    # helpers ----------------------------------------------------------------------

    def _workflows(self, runs: list[dict] | None = None) -> dict[tuple[str, str], list[dict]]:
        """(repo, workflow) → its runs, newest first."""
        out: dict[tuple[str, str], list[dict]] = {}
        for r in self.runs if runs is None else runs:
            out.setdefault((r["repo"], r["workflow"]), []).append(r)
        return out

    def _label(self, key: tuple[str, str]) -> str:
        """The workflow's name, with its repo when two repos have a workflow of that name."""
        repo, name = key
        clash = sum(1 for _, n in self._workflows() if n == name) > 1
        return f"{repo.split('/')[-1].split('-')[-1][:5]}·{name}" if clash else name  # "Infra·CI" fits the label

    def _item(self, r: dict, extra: str = "") -> Item:
        icon, tone = _look(r)
        title = r["title"] if r["title"].startswith(r["workflow"]) else f"{r['workflow']}: {r['title']}"
        state = r["conclusion"] or r["status"].replace("_", " ")
        bits = [f"{r['repo'].split('/')[-1]}@{r['branch']}", state]
        if r["status"] == "completed":
            bits.append(_dur(_secs(r)))
        elif r["started"]:
            bits.append(f"started {ago(r['started'])}")
        if (r.get("attempt") or 1) > 1:
            bits.append(f"attempt {r['attempt']}")
        bits += [extra, ago(r["updated"]) if r["status"] == "completed" else "" if r["started"] else ago(r["created"])]
        return Item(title=title, meta=" · ".join(b for b in bits if b), url=r["url"],
                    ref={"kind": "run", "repo": r["repo"], "id": r["id"]}, icon=icon, tone=tone,
                    when=r["created"], pulse=r["status"] != "completed")

    @staticmethod
    def _streak(runs: list[dict]) -> tuple[int, dict | None]:
        """Failures in a row from the newest decisive run, and the first of them (when it broke)."""
        n, first = 0, None
        for r in runs:
            if r["conclusion"] not in DECISIVE:
                continue
            if r["conclusion"] == "success":
                break
            n, first = n + 1, r
        return n, first

    def _failing(self, runs: list[dict] | None = None) -> list[tuple[tuple[str, str], dict, int, dict]]:
        """Workflows whose latest decisive run failed: (key, that run, streak, first failure), longest broken first."""
        out = []
        for key, wf in self._workflows(runs).items():
            n, first = self._streak(wf)
            if n:
                latest = next(r for r in wf if r["conclusion"] in FAILED)
                out.append((key, latest, n, first))
        out.sort(key=lambda t: t[3]["created"].timestamp() if t[3]["created"] else 0)
        return out

    # cards ------------------------------------------------------------------------

    def _cards(self) -> list[Card]:
        cards = self.skeleton()
        if self.runs is None:
            return cards
        by = {c.key: c for c in cards}
        runs = self.runs
        repos = self._repos()
        many = len(repos) > 1

        broken = self._failing()
        c = by["failing"]
        c.value, c.tone = str(len(broken)), "error" if broken else "green"
        c.hint = f"of {len(self._workflows())}"
        c.rows = []
        for repo in repos:
            mine = [b for b in broken if b[0][0] == repo]
            if many and mine:
                c.rows.append(Group(f"{repo.split('/')[-1].upper()} · {len(mine)}", "error"))
            for key, r, n, first in mine:
                since = f"broken since {ago(first['created'])}" if first and first["created"] else ""
                c.rows.append(self._item(r, " · ".join(x for x in (f"{n} in a row" if n > 1 else "", since) if x)))

        live = [r for r in runs if r["status"] != "completed"]
        c = by["running"]
        moving = [r for r in live if r["status"] == "in_progress"]
        waiting = [r for r in live if r["status"] != "in_progress"]
        c.value, c.tone = str(len(live)), "warning" if live else "muted"
        c.hint = f"{len(waiting)} queued"
        c.rows = []
        if moving:
            c.rows += [Group(f"IN PROGRESS · {len(moving)}", "warning"), *(self._item(r) for r in moving)]
        if waiting:
            c.rows += [Group(f"QUEUED · {len(waiting)}", "muted"), *(self._item(r) for r in waiting)]

        c = by["recent"]
        fails = sum(1 for r in runs if r["conclusion"] in FAILED)
        c.value, c.hint = str(len(runs)), f"{fails} failed"
        c.rows = []
        today = datetime.now().astimezone().date()
        days: dict[str, list[dict]] = {}
        for r in runs:
            d = r["created"].astimezone().date() if r["created"] else today
            name = ("TODAY" if d == today else "YESTERDAY" if d == today - timedelta(days=1)
                    else d.strftime("%a %d %b").upper())
            days.setdefault(name, []).append(r)
        for name, rs in days.items():
            bad = sum(1 for r in rs if r["conclusion"] in FAILED)
            title = f"{name} · {len(rs)}" + (f" · {bad} failed" if bad else "")
            c.rows.append(Group(title, "error" if bad else "muted"))
            c.rows += [self._item(r) for r in rs]

        c = by["rate"]
        pct = _pct(runs)
        done = [r for r in runs if r["conclusion"] in DECISIVE]
        c.value, c.tone = (f"{pct}%" if pct is not None else "—"), _rate_tone(pct)
        c.hint = f"{sum(r['conclusion'] == 'success' for r in done)}/{len(done)}"

        for repo in repos:
            c = by.get(f"repo:{repo}")
            if c is None:
                continue
            mine = [r for r in runs if r["repo"] == repo]
            wfs = self._workflows(mine)
            pct = _pct(mine)
            red = {k for k, *_ in self._failing(mine)}
            c.value, c.tone = (f"{pct}%" if pct is not None else "—"), _rate_tone(pct)
            c.hint = f"{len(red)} failing of {len(wfs)}"
            c.rows = []
            groups = [("FAILING", "error", [k for k in wfs if k in red]),
                      ("RUNNING", "warning", [k for k in wfs if k not in red and wfs[k][0]["status"] != "completed"]),
                      ("PASSING", "green", [k for k in wfs if k not in red and wfs[k][0]["status"] == "completed"])]
            for title, tone, keys in groups:
                if keys:
                    c.rows.append(Group(f"{title} · {len(keys)}", tone))
                    for k in sorted(keys, key=lambda k: k[1].lower()):
                        r = wfs[k][0]
                        rate = _pct(wfs[k])
                        c.rows.append(self._item(r, f"{rate}% of {len(wfs[k])}" if rate is not None else ""))
        if self.errors:  # a repo that didn't load says so on RECENT rather than vanishing quietly
            by["recent"].rows[:0] = [Item(title=f"⚠ {repo.split('/')[-1]}: {err}", icon="✖", tone="error")
                                     for repo, err in self.errors.items()]
        return cards

    # side panel -------------------------------------------------------------------

    def side(self, width: int) -> Text | None:
        if not self.runs:
            return None
        out = Text()
        wfs = self._workflows()
        label_w = min(18, max(width // 2 - 2, 8))

        rates = [(self._label(k), _pct(rs), rs) for k, rs in wfs.items()]
        rates = [r for r in rates if r[1] is not None]
        rates.sort(key=lambda r: (r[1], r[0].lower()))
        out.append_text(heading("PASS RATE BY WORKFLOW"))
        for name, pct, _ in rates[:10]:
            color = px.GREEN if pct >= 90 else px.YELLOW if pct >= 70 else px.RED
            out.append_text(hbar(name, pct, 100, width, color, label_w=label_w))
        if len(rates) > 10:
            floor = rates[9][1]
            out.append(f"  +{len(rates) - 10} more at {'100%' if floor == 100 else f'≥ {floor}%'}\n", px.DIM)

        times = [(self._label(k), [s for s in map(_secs, rs) if s is not None]) for k, rs in wfs.items()]
        times = sorted(((n, statistics.median(t)) for n, t in times if t), key=lambda t: -t[1])
        if times:
            out.append("\n")
            out.append_text(heading("MEDIAN DURATION"))
            for name, secs in times[:8]:
                out.append_text(kv(name, _dur(secs), px.CYAN, label_w=label_w))

        flaky = max(((self._label(k), sum(r["conclusion"] in FAILED for r in rs), len(rs)) for k, rs in wfs.items()),
                    key=lambda t: (t[1], -t[2]), default=None)
        out.append("\n")
        out.append_text(heading("MOST FAILURES"))
        if flaky and flaky[1]:
            out.append_text(kv(flaky[0], f"{flaky[1]} of {flaky[2]} runs", px.RED, label_w=label_w))
        else:
            out.append_text(kv("none", "no failures in the window", px.GREEN, label_w=label_w))

        now = datetime.now().astimezone()
        oldest = min((r["created"] for r in self.runs if r["created"]), default=None)
        span = min(14, (now.date() - oldest.astimezone().date()).days + 1) if oldest else 1
        passed, failed = [0] * span, [0] * span
        for r in self.runs:
            if r["created"] and r["conclusion"] in DECISIVE:
                back = (now.date() - r["created"].astimezone().date()).days
                if 0 <= back < span:
                    (passed if r["conclusion"] == "success" else failed)[span - 1 - back] += 1
        top = max(passed + failed, default=0)
        out.append("\n")
        out.append_text(heading(f"BY DAY · LAST {span}D"))
        out.append_text(spark("passed", passed, px.GREEN, top))
        out.append_text(spark("failed", failed, px.RED, top))

        if self.errors:
            out.append("\n")
            out.append_text(heading("NOT LOADED", px.RED))
            for repo, err in self.errors.items():
                out.append_text(kv(repo.split("/")[-1], err, px.RED))
        return out
