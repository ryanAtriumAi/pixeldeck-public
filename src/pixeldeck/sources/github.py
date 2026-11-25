"""GitHub via the `gh` CLI, so it reuses whatever account `gh auth` has active."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone

from .base import Feed, Item, NotConfigured, ago, parse_time, run_cmd, run_json

PR_QUERY = """
query {
  viewer {
    login
    pullRequests(first: 50, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url updatedAt isDraft mergeable reviewDecision
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
  reviews: search(query: "is:open is:pr review-requested:@me archived:false", type: ISSUE, first: 50) {
    nodes { ...queuePr }
  }
  assigned: search(query: "is:open is:pr assignee:@me -author:@me archived:false", type: ISSUE, first: 50) {
    nodes { ...queuePr }
  }
}

fragment queuePr on PullRequest {
  number title url updatedAt isDraft additions deletions
  author { login }
  repository { nameWithOwner }
}
"""

# The GitHub page's open PRs and review queue: PR_QUERY plus the dates and review requests the page charts.
OVERVIEW_QUERY = """
query {
  viewer {
    login
    pullRequests(first: 50, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url createdAt updatedAt isDraft mergeable reviewDecision
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
  reviews: search(query: "is:open is:pr review-requested:@me archived:false", type: ISSUE, first: 50) {
    nodes { ...queuePr }
  }
  assigned: search(query: "is:open is:pr assignee:@me -author:@me archived:false", type: ISSUE, first: 50) {
    nodes { ...queuePr }
  }
}

fragment queuePr on PullRequest {
  number title url createdAt updatedAt isDraft additions deletions
  author { login }
  repository { nameWithOwner }
  timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 5) {
    nodes { ... on ReviewRequestedEvent { createdAt requestedReviewer { ... on User { login } } } }
  }
}
"""

DONE_FRAGMENT = """
fragment donePr on PullRequest {
  number title url state isDraft createdAt updatedAt mergedAt closedAt additions deletions
  author { login }
  repository { nameWithOwner }
}
"""

CI_ICON = {
    "SUCCESS": ("✔", "success"),
    "FAILURE": ("✖", "error"),
    "ERROR": ("✖", "error"),
    "PENDING": ("◷", "warning"),
    "EXPECTED": ("◷", "warning"),
}

REASON = {
    "review_requested": ("REVIEW", "accent"),
    "mention": ("@YOU", "accent"),
    "team_mention": ("@TEAM", "secondary"),
    "author": ("AUTHOR", "primary"),
    "comment": ("COMMENT", "primary"),
    "assign": ("ASSIGN", "warning"),
    "ci_activity": ("CI", "error"),
    "state_change": ("STATE", "muted"),
    "subscribed": ("WATCH", "muted"),
    "security_alert": ("SECURITY", "error"),
}


def queue_item(pr: dict, why: str = "") -> Item:
    """A PR waiting on your review, as the review-queue tile lists it."""
    when = parse_time(pr["updatedAt"])
    repo = pr["repository"]["nameWithOwner"].split("/")[-1]
    size = f"+{pr['additions']}/-{pr['deletions']}"
    parts = [repo, f"@{(pr.get('author') or {}).get('login', '?')}", *([why] if why else []), size, ago(when)]
    return Item(
        title=f"#{pr['number']} {pr['title']}",
        meta=" · ".join(parts),
        url=pr["url"],
        ref={"kind": "pr", "url": pr["url"]},
        icon="◇" if pr["isDraft"] else "◆",
        tone="muted" if pr["isDraft"] else "accent",
        when=when,
    )


def ci_state(pr: dict) -> str:
    """The last commit's check rollup (SUCCESS, FAILURE, PENDING, ...), "" when it has no checks."""
    commits = pr["commits"]["nodes"]
    return ((commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}).get("state", "")


def mine_item(pr: dict) -> Item:
    """One of your open PRs: CI icon, then draft / conflict / review state."""
    when = parse_time(pr["updatedAt"])
    icon, tone = CI_ICON.get(ci_state(pr), ("·", "muted"))
    flags = []
    if pr["isDraft"]:
        flags.append("draft")
    if pr["mergeable"] == "CONFLICTING":
        flags.append("CONFLICT")
        tone = "error"
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        flags.append("approved")
    elif decision == "CHANGES_REQUESTED":
        flags.append("changes requested")
        tone = "warning" if tone != "error" else tone
    meta = " · ".join([pr["repository"]["nameWithOwner"].split("/")[-1], *flags, ago(when)])
    return Item(title=f"#{pr['number']} {pr['title']}", meta=meta, url=pr["url"], icon=icon, tone=tone,
                when=when, ref={"kind": "pr", "url": pr["url"]})


def _merge_queue(data: dict) -> list[tuple[dict, str]]:
    """Review requests plus PRs assigned to you, each once, newest update first."""
    asked: dict[str, tuple[dict, str]] = {}
    for key, why in (("reviews", ""), ("assigned", "assigned")):
        for pr in data[key]["nodes"]:
            if pr and pr.get("url") and pr["url"] not in asked:
                asked[pr["url"]] = (pr, why)
    return sorted(asked.values(), key=lambda pw: pw[0]["updatedAt"], reverse=True)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check() -> None:
    if not shutil.which("gh"):
        raise NotConfigured("install the GitHub CLI and run `gh auth login`")


class GitHub:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.login: str | None = None
        self._pr_repos: list[str] = []
        # Empty list = everything you can see.
        self.repos = {r.lower() for r in cfg.get("repos", [])}
        self.inbox_repos = {r.lower() for r in cfg.get("inbox_repos", [])}

    def _watched(self, full_name: str) -> bool:
        return not self.repos or full_name.lower() in self.repos

    async def pull_requests(self) -> tuple[Feed, Feed]:
        """Returns (review queue, my open PRs) from a single GraphQL call."""
        _check()
        data = (await run_json("gh", "api", "graphql", "-f", f"query={PR_QUERY}"))["data"]
        self.login = data["viewer"]["login"]

        # The queue is PRs asking for you (review requested, or assigned to you), so it covers every repo,
        # not just github.repos: a request on another repo still needs you.
        queue = [queue_item(pr, why) for pr, why in _merge_queue(data)]
        mine = [mine_item(pr) for pr in self._my_prs(data)]

        return (
            Feed(items=queue, count=len(queue)),
            Feed(items=mine, count=len(mine)),
        )

    def _my_prs(self, data: dict) -> list[dict]:
        """Your open PRs in the watched repos (remembers their repos for the CI tile)."""
        prs = [pr for pr in data["viewer"]["pullRequests"]["nodes"] if self._watched(pr["repository"]["nameWithOwner"])]
        self._pr_repos = list(dict.fromkeys(pr["repository"]["nameWithOwner"] for pr in prs))
        return prs

    async def overview(self, periods: tuple[int, ...] = (7, 30, 90), weeks: int = 8) -> dict:
        """Everything the GitHub page shows: the review queue (with when you were asked), your open PRs, the PRs
        you opened / merged / reviewed over the longest period (up to 100 rows each), exact counts for every
        period, and opened / merged counts per week. Four GraphQL calls at once: as one query the searches run
        back to back and GitHub gives up after 10 seconds."""
        _check()
        now = datetime.now(timezone.utc)
        longest = max(periods)
        since = {d: _stamp(now - timedelta(days=d)) for d in periods}
        kinds = {  # card → (search, sort, the field that dates a row)
            "opened": ("is:pr author:@me created:{}", "created-desc", "createdAt"),
            "merged": ("is:pr author:@me is:merged merged:{}", "updated-desc", "mergedAt"),
        }

        def count(alias: str, q: str) -> str:
            return f'{alias}: search(query: "{q}", type: ISSUE, first: 0) {{ issueCount }}'

        def query(kind: str) -> str:
            q, order, _ = kinds[kind]
            parts = [f'rows: search(query: "{q.format(">=" + since[longest])} sort:{order}", type: ISSUE, '
                     "first: 100) { issueCount nodes { ...donePr } }"]
            parts += [count(f"d{d}", q.format(">=" + since[d])) for d in periods if d != longest]
            for w in range(weeks):  # exact weekly counts (w0 is the last 7 days), so a busy quarter charts right
                end = now - timedelta(days=7 * w, seconds=1)
                span = f"{_stamp(now - timedelta(days=7 * (w + 1)))}..{_stamp(end)}"
                parts.append(count(f"w{w}", q.format(span)))
            return "query {\n  " + "\n  ".join(parts) + "\n}\n" + DONE_FRAGMENT

        async def call(q: str) -> dict:
            return (await run_json("gh", "api", "graphql", "-f", f"query={q}", timeout=45))["data"]

        data, reviewed, *done = await asyncio.gather(call(OVERVIEW_QUERY), self.reviewed(longest),
                                                     *(call(query(k)) for k in kinds))
        self.login = login = data["viewer"]["login"]

        queue = []
        for pr, why in _merge_queue(data):
            asks = [e for e in (pr.get("timelineItems") or {}).get("nodes") or [] if e and e.get("createdAt")]
            for_me = [e for e in asks if ((e.get("requestedReviewer") or {}).get("login") or "") == login]
            asked = (for_me or asks or [{}])[-1].get("createdAt") or pr.get("createdAt")
            queue.append({**pr, "why": why, "askedAt": asked})
        out: dict = {"login": login, "queue": queue, "mine": self._my_prs(data), "weekly": {}}
        for (kind, (_, _, field)), d in zip(kinds.items(), done):
            counts = {p: d[f"d{p}"]["issueCount"] for p in periods if p != longest}
            counts[longest] = d["rows"]["issueCount"]
            nodes = [n for n in d["rows"]["nodes"] if n and n.get("url")]
            out[kind] = {"nodes": nodes, "count": counts, "field": field}
            if "w0" in d:
                out["weekly"][kind] = [d[f"w{w}"]["issueCount"] for w in reversed(range(weeks))]
        stamps = [parse_time(n["updatedAt"]) for n in reviewed]
        out["reviewed"] = {"nodes": reviewed, "field": "updatedAt",
                           "count": {p: sum(1 for t in stamps if t and t >= now - timedelta(days=p)) for p in periods}}
        out["weekly"]["reviewed"] = [sum(1 for t in stamps if t and now - timedelta(days=7 * (w + 1)) <= t
                                         < now - timedelta(days=7 * w)) for w in reversed(range(weeks))]
        return out

    async def _me(self) -> str:
        if not self.login:
            self.login = (await run_cmd("gh", "api", "user", "--jq", ".login")).strip()
        return self.login

    async def reviewed(self, days: int = 90) -> list[dict]:
        """Other people's PRs you reviewed, commented on or merged, updated in the last `days`, newest first.
        `reviewed-by:` alone misses most of it: it only counts formal reviews, and a lot of reviewing ends in a
        comment or a merge. GitHub search can't ask who merged, so this lists the PRs you're involved in and
        checks each one. Searches return at most 100 rows, so it asks week by week (all at once), splitting
        any stretch with more. Each PR gets `how`: the things you did, e.g. ["merged", "commented"]."""
        _check()
        me = await self._me()
        now = datetime.now(timezone.utc)

        async def span(lo: datetime, hi: datetime) -> list[dict]:
            r = f"{_stamp(lo)}..{_stamp(hi - timedelta(seconds=1))}"
            q = ("query {\n"
                 f'  all: search(query: "is:pr involves:@me -author:@me updated:{r}", type: ISSUE, first: 100) {{\n'
                 "    issueCount nodes { ...donePr ... on PullRequest { mergedBy { login } "
                 f'reviews(author: "{me}", first: 1) {{ totalCount }} }} }} }}\n'
                 f'  said: search(query: "is:pr commenter:@me -author:@me updated:{r}", type: ISSUE, first: 100) {{\n'
                 "    nodes { ... on PullRequest { url } } }\n}\n" + DONE_FRAGMENT)
            d = (await run_json("gh", "api", "graphql", "-f", f"query={q}", timeout=45))["data"]
            if d["all"]["issueCount"] > 100 and hi - lo > timedelta(hours=6):
                mid = lo + (hi - lo) / 2  # too many for one page: ask for each half
                a, b = await asyncio.gather(span(lo, mid), span(mid, hi))
                return a + b
            said = {n["url"] for n in d["said"]["nodes"] if n and n.get("url")}
            out = []
            for n in d["all"]["nodes"]:
                if not n or not n.get("url"):
                    continue
                how = [verb for verb, yes in (("merged", (n.get("mergedBy") or {}).get("login") == me),
                                              ("reviewed", (n.get("reviews") or {}).get("totalCount")),
                                              ("commented", n["url"] in said)) if yes]
                if how:  # involves:@me also matches PRs you were only asked to review or mentioned in
                    out.append({**n, "how": how})
            return out

        edges = [now - timedelta(days=min(7 * w, days)) for w in range(-(-days // 7) + 1)]
        parts = await asyncio.gather(*(span(lo, hi) for hi, lo in zip(edges, edges[1:])))
        seen: dict[str, dict] = {}
        for n in (n for part in parts for n in part):
            seen.setdefault(n["url"], n)
        return sorted(seen.values(), key=lambda n: n["updatedAt"], reverse=True)

    async def mark_read(self, thread: str) -> None:
        """Marks one notification thread read on GitHub."""
        _check()
        await run_cmd("gh", "api", "-X", "PATCH", f"notifications/threads/{thread}")

    async def notifications(self, limit: int = 30) -> Feed:
        _check()
        rows = await run_json("gh", "api", "notifications?per_page=50")
        items = []
        for n in rows:
            subj = n["subject"]
            repo = n["repository"]["full_name"]
            if self.inbox_repos and repo.lower() not in self.inbox_repos:
                continue
            if self.cfg.get("inbox_reasons") and n["reason"] not in self.cfg["inbox_reasons"]:
                continue
            label, tone = REASON.get(n["reason"], (n["reason"].upper(), "muted"))
            when = parse_time(n["updated_at"])
            items.append(
                Item(
                    title=subj["title"],
                    meta=f"{label} · {repo.split('/')[-1]} · {subj['type']} · {ago(when)}",
                    url=_html_url(subj.get("url"), repo),
                    ref={
                        "kind": {"PullRequest": "pr", "Issue": "issue"}.get(subj["type"], "notification"),
                        "url": _html_url(subj.get("url"), repo),
                        "type": subj["type"],
                        "reason": n["reason"],
                        "repo": repo,
                        "thread": n["id"],
                    },
                    icon="●" if n["unread"] else "○",
                    tone=tone,
                    unread=n["unread"],
                    when=when,
                )
            )
        return Feed(items=items[:limit], count=sum(1 for i in items if i.unread))

    async def ci_runs(self) -> Feed:
        _check()
        repos = self.cfg.get("ci_repos") or self.cfg.get("repos", [])[:8] or self._pr_repos[:3]
        if not repos:
            return Feed(note="set github.ci_repos in config.toml to watch CI")
        branch = self.cfg.get("ci_branch", "main")

        async def one(repo: str) -> list[Item]:
            data = await run_json(
                "gh", "api", f"repos/{repo}/actions/runs?per_page=6&branch={branch}"
            )
            out = []
            for run in data.get("workflow_runs", []):
                when = parse_time(run["updated_at"])
                status, concl = run["status"], run.get("conclusion")
                if status != "completed":
                    icon, tone = "◷", "warning"
                elif concl == "success":
                    icon, tone = "✔", "success"
                elif concl in ("skipped", "cancelled", "neutral"):
                    icon, tone = "◌", "muted"
                else:
                    icon, tone = "✖", "error"
                title = run.get("display_title") or run["name"]
                if not title.startswith(run["name"]):
                    title = f"{run['name']}: {title}"
                out.append(
                    Item(
                        title=title,
                        meta=f"{repo.split('/')[-1]}@{run['head_branch']} · {concl or status} · {ago(when)}",
                        url=run["html_url"],
                        ref={"kind": "run", "repo": repo, "id": run["id"]},
                        icon=icon,
                        tone=tone,
                        when=when,
                    )
                )
            return out

        results = await asyncio.gather(*(one(r) for r in repos), return_exceptions=True)
        items = [i for r in results if isinstance(r, list) for i in r]
        items.sort(key=lambda i: i.when or 0, reverse=True)
        failing = sum(1 for i in items if i.tone == "error")
        return Feed(items=items[:20], count=failing)

    async def ci_history(self, per_repo: int = 50) -> tuple[list[dict], dict[str, str]]:
        """The CI page's deeper window: the last `per_repo` runs on the watched branch of each CI repo.

        Returns (runs newest first, {repo: error} for repos that failed to load). Read-only.
        """
        _check()
        repos = self.cfg.get("ci_repos") or self.cfg.get("repos", [])[:8] or self._pr_repos[:3]
        if not repos:
            raise NotConfigured("set github.ci_repos in config.toml to watch CI")
        branch = self.cfg.get("ci_branch", "main")

        async def one(repo: str) -> list[dict]:
            data = await run_json("gh", "api", f"repos/{repo}/actions/runs?per_page={per_repo}&branch={branch}")
            return [
                {
                    "repo": repo, "id": r["id"], "workflow": r["name"], "title": r.get("display_title") or r["name"],
                    "status": r["status"], "conclusion": r.get("conclusion"), "branch": r["head_branch"],
                    "event": r.get("event", ""), "sha": r.get("head_sha", ""), "url": r["html_url"],
                    "created": parse_time(r.get("created_at")), "started": parse_time(r.get("run_started_at")),
                    "updated": parse_time(r.get("updated_at")), "attempt": r.get("run_attempt", 1),
                }
                for r in data.get("workflow_runs", [])
            ]

        results = await asyncio.gather(*(one(r) for r in repos), return_exceptions=True)
        errors = {repo: str(res)[:120] for repo, res in zip(repos, results) if isinstance(res, BaseException)}
        if len(errors) == len(repos):
            raise next(res for res in results if isinstance(res, BaseException))
        runs = [run for res in results if isinstance(res, list) for run in res]
        runs.sort(key=lambda r: r["created"].timestamp() if r["created"] else 0, reverse=True)
        return runs, errors


def _html_url(api_url: str | None, repo: str) -> str:
    if not api_url:
        return f"https://github.com/{repo}"
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    return url.replace("/pulls/", "/pull/").replace("/commits/", "/commit/")


async def list_repos() -> list[tuple[str, str, bool]]:
    """(full_name, pushed_at, archived) for every repo the active gh account can see, newest push first."""
    out = await run_cmd(
        "gh", "api", "--paginate",
        "user/repos?per_page=100&affiliation=owner,collaborator,organization_member&sort=pushed",
        "--jq", ".[] | [.full_name, .pushed_at, .archived] | @tsv",
        timeout=60,
    )
    rows = []
    for line in out.splitlines():
        name, pushed, archived = (line.split("\t") + ["", ""])[:3]
        rows.append((name, pushed, archived == "true"))
    rows.sort(key=lambda r: r[1] or "", reverse=True)
    return rows
