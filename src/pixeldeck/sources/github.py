"""GitHub via the `gh` CLI, so it reuses whatever account `gh auth` has active."""

from __future__ import annotations

import asyncio
import shutil

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
        queue = []
        asked: dict[str, tuple[dict, str]] = {}
        for key, why in (("reviews", ""), ("assigned", "assigned")):
            for pr in data[key]["nodes"]:
                if pr and pr.get("url") and pr["url"] not in asked:
                    asked[pr["url"]] = (pr, why)
        for pr, why in sorted(asked.values(), key=lambda pw: pw[0]["updatedAt"], reverse=True):
            when = parse_time(pr["updatedAt"])
            repo = pr["repository"]["nameWithOwner"].split("/")[-1]
            size = f"+{pr['additions']}/-{pr['deletions']}"
            parts = [repo, f"@{(pr.get('author') or {}).get('login', '?')}", *([why] if why else []), size, ago(when)]
            queue.append(
                Item(
                    title=f"#{pr['number']} {pr['title']}",
                    meta=" · ".join(parts),
                    url=pr["url"],
                    ref={"kind": "pr", "url": pr["url"]},
                    icon="◇" if pr["isDraft"] else "◆",
                    tone="muted" if pr["isDraft"] else "accent",
                    when=when,
                )
            )

        mine = []
        repos: list[str] = []
        for pr in data["viewer"]["pullRequests"]["nodes"]:
            when = parse_time(pr["updatedAt"])
            full = pr["repository"]["nameWithOwner"]
            if not self._watched(full):
                continue
            if full not in repos:
                repos.append(full)
            commits = pr["commits"]["nodes"]
            rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
            icon, tone = CI_ICON.get(rollup.get("state", ""), ("·", "muted"))
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
            meta = " · ".join([full.split("/")[-1], *flags, ago(when)])
            mine.append(
                Item(title=f"#{pr['number']} {pr['title']}", meta=meta, url=pr["url"], icon=icon, tone=tone,
                     when=when, ref={"kind": "pr", "url": pr["url"]})
            )
        self._pr_repos = repos

        return (
            Feed(items=queue, count=len(queue)),
            Feed(items=mine, count=len(mine)),
        )

    async def notifications(self) -> Feed:
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
                    },
                    icon="●" if n["unread"] else "○",
                    tone=tone,
                    unread=n["unread"],
                    when=when,
                )
            )
        return Feed(items=items[:30], count=sum(1 for i in items if i.unread))

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
