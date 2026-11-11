"""Slack mentions/DMs and Linear issues. Both read a token from env or keychain."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..config import get_secret
from .base import Feed, Item, NotConfigured, SourceError, ago, parse_time


class Slack:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.user_id: str | None = None

    async def _call(self, client: httpx.AsyncClient, token: str, method: str, **params) -> dict:
        resp = await client.get(
            f"https://slack.com/api/{method}", params=params, headers={"Authorization": f"Bearer {token}"}
        )
        data = resp.json()
        if not data.get("ok"):
            raise SourceError(f"slack {method}: {data.get('error')}")
        return data

    async def mentions(self) -> Feed:
        token = get_secret("slack", env="SLACK_TOKEN")
        if not token:
            raise NotConfigured("connect Slack to see mentions and DMs")
        async with httpx.AsyncClient(timeout=20) as client:
            if not self.user_id:
                self.user_id = (await self._call(client, token, "auth.test"))["user_id"]
            queries = [q.replace("<@ME>", f"<@{self.user_id}>") for q in self.cfg.get("queries", [])]
            results = await asyncio.gather(
                *(
                    self._call(client, token, "search.messages", query=q, sort="timestamp", count=15)
                    for q in queries
                )
            )
        seen: set[str] = set()
        items = []
        for res in results:
            for m in res.get("messages", {}).get("matches", []):
                key = f"{m['channel']['id']}:{m['ts']}"
                if key in seen:
                    continue
                seen.add(key)
                when = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)
                chan = m["channel"]
                where = "DM" if chan.get("is_im") else ("group DM" if chan.get("is_mpim") else f"#{chan.get('name', '?')}")
                text = " ".join((m.get("text") or "").split())
                items.append(
                    Item(
                        title=text[:140] or "(attachment)",
                        meta=f"@{m.get('username', '?')} · {where} · {ago(when)}",
                        url=m.get("permalink"),
                        ref={
                            "kind": "slack",
                            "text": m.get("text") or "",
                            "user": m.get("username", "?"),
                            "where": where,
                            "channel": chan.get("id"),
                            "ts": m["ts"],
                            "thread_ts": (re.search(r"thread_ts=([\d.]+)", m.get("permalink") or "") or [None, None])[1],
                        },
                        icon="✦" if chan.get("is_im") else "#",
                        tone="accent" if chan.get("is_im") else "secondary",
                        when=when,
                    )
                )
        items.sort(key=lambda i: i.when, reverse=True)
        day = datetime.now(timezone.utc).timestamp() - 86400
        recent = sum(1 for i in items if i.when and i.when.timestamp() > day)
        return Feed(items=items[:25], count=recent)


LINEAR_QUERY = """
query($filter: IssueFilter, $after: String) {
  issues(first: 100, after: $after, filter: $filter, orderBy: updatedAt) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier title url priority updatedAt dueDate
      state { name type }
      assignee { name }
    }
  }
}
"""

PRIORITY = {1: ("‼", "error"), 2: ("!", "accent"), 3: ("▪", "secondary"), 4: ("·", "muted")}
STATE_TONE = {"started": "accent", "unstarted": "secondary", "backlog": "muted", "triage": "warning"}
STATE_ORDER = {"started": 0, "triage": 1, "unstarted": 2, "backlog": 3}
PRIORITY_LABEL = {1: "urgent", 2: "high", 3: "medium", 4: "low"}
PRIORITY_NAMES = [(1, "urgent"), (2, "high"), (3, "medium"), (4, "low"), (0, "no priority")]

# which issues the tile lists
SCOPES = {
    "assigned": ("assigned to me", {"assignee": {"isMe": {"eq": True}}}),
    "created": ("created by me", {"creator": {"isMe": {"eq": True}}}),
    "subscribed": ("I'm subscribed to", {"subscribers": {"some": {"isMe": {"eq": True}}}}),
    "team": ("whole team(s)", {}),
}
STATES = [("started", "in progress"), ("unstarted", "todo"), ("triage", "triage"), ("backlog", "backlog")]
SORTS = {"status": "status, then priority", "priority": "priority", "updated": "recently updated", "due": "due date"}


SEARCH_QUERY = """
query($term: String!, $filter: IssueFilter, $first: Int) {
  searchIssues(term: $term, first: $first, filter: $filter) {
    totalCount
    nodes {
      identifier title url priority updatedAt dueDate
      state { name type }
      assignee { name }
      team { key }
    }
  }
}
"""

ISSUE_BY_ID = """
query($id: String!) {
  issue(id: $id) {
    identifier title url priority updatedAt dueDate
    state { name type }
    assignee { name }
    team { key }
  }
}
"""

# tickets assigned to me lately, with the history that says when (Linear has no assignedAt field)
NEWLY_ASSIGNED = """
query($filter: IssueFilter) {
  viewer { id }
  issues(first: 100, filter: $filter, orderBy: updatedAt) {
    nodes {
      identifier title url priority updatedAt dueDate
      state { name type }
      assignee { name }
      history(first: 50) { nodes { createdAt toAssigneeId } }
    }
  }
}
"""

TICKET_ID = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{1,6})-(\d+)\s*$")


def issue_item(n: dict, show_assignee: bool = True) -> Item:
    """One Linear issue as a tile/search row."""
    icon, tone = PRIORITY.get(n["priority"], ("·", "muted"))
    when = parse_time(n["updatedAt"])
    bits = [n["state"]["name"]]
    if n["priority"] in PRIORITY_LABEL:
        bits.append(PRIORITY_LABEL[n["priority"]])
    if n.get("dueDate"):
        bits.append(f"due {n['dueDate']}")
    if show_assignee:
        bits.append(f"→ {(n.get('assignee') or {}).get('name', 'unassigned')}")
    bits.append(ago(when))
    done = n["state"]["type"] in ("completed", "canceled")
    return Item(
        title=f"{n['identifier']} {n['title']}",
        meta=" · ".join(bits),
        url=n["url"],
        ref={"kind": "linear", "id": n["identifier"]},
        icon="✔" if n["state"]["type"] == "completed" else "✖" if n["state"]["type"] == "canceled" else icon,
        tone="muted" if done else "error" if n["priority"] == 1 else STATE_TONE.get(n["state"]["type"], tone),
        when=when,
    )


class Linear:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    async def _gql(self, query: str, variables: dict) -> dict:
        key = get_secret("linear", env="LINEAR_API_KEY")
        if not key:
            raise NotConfigured("connect Linear to search it")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.linear.app/graphql",
                                     json={"query": query, "variables": variables},
                                     headers={"Authorization": key, "Content-Type": "application/json"})
        data = resp.json()
        if data.get("errors"):
            raise SourceError(f"linear: {data['errors'][0].get('message', '')[:120]}")
        return data["data"]

    async def search(self, term: str, open_only: bool = False, mine: bool = False,
                     first: int = 50) -> tuple[list[Item], int]:
        """Linear's own search (titles, descriptions, ids). A ticket id like ABC-123 is looked up
        directly and put first. Returns (items, how many Linear matched before the toggles)."""
        flt: dict = {}
        if open_only:
            flt["state"] = {"type": {"nin": ["completed", "canceled"]}}
        if mine:
            flt["assignee"] = {"isMe": {"eq": True}}
        jobs = [self._gql(SEARCH_QUERY, {"term": term, "first": first, "filter": flt or None})]
        m = TICKET_ID.match(term)
        if m:
            jobs.append(self._gql(ISSUE_BY_ID, {"id": f"{m.group(1).upper()}-{m.group(2)}"}))
        res = await asyncio.gather(*jobs, return_exceptions=True)
        if isinstance(res[0], BaseException) and not (m and not isinstance(res[-1], BaseException)):
            raise res[0]
        found = res[0]["searchIssues"] if not isinstance(res[0], BaseException) else {"nodes": [], "totalCount": 0}
        nodes = list(found["nodes"])
        if m and not isinstance(res[-1], BaseException) and res[-1].get("issue"):
            exact = res[-1]["issue"]
            nodes = [exact] + [n for n in nodes if n["identifier"] != exact["identifier"]]
        return [issue_item(n) for n in nodes], found.get("totalCount") or len(nodes)

    def states(self) -> list[str]:
        if self.cfg.get("states"):
            return list(self.cfg["states"])
        # older configs: include_backlog / include_triage flags
        out = ["started", "unstarted"]
        if self.cfg.get("include_triage", True):
            out.append("triage")
        if self.cfg.get("include_backlog", False):
            out.append("backlog")
        return out

    def _filter(self) -> dict:
        scope = self.cfg.get("scope", "assigned")
        flt: dict = {"state": {"type": {"in": self.states()}}, **SCOPES.get(scope, SCOPES["assigned"])[1]}
        teams = [t.upper() for t in self.cfg.get("teams") or []]
        if teams:
            flt["team"] = {"key": {"in": teams}}
        elif scope == "team":
            raise NotConfigured("pick team keys (Settings → Linear → TEAMS) to list whole teams")
        return flt

    async def newly_assigned(self, acked: dict[str, float]) -> list[dict]:
        """Open tickets assigned to me in the last `new_days` that I haven't opened since, newest
        first. Each node gets `assignedAt` (epoch seconds)."""
        days = int(self.cfg.get("new_days", 14) or 0)
        if days <= 0:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days)
        data = await self._gql(NEWLY_ASSIGNED, {"filter": {
            "assignee": {"isMe": {"eq": True}},
            "state": {"type": {"nin": ["completed", "canceled"]}},
            "updatedAt": {"gte": since.isoformat()},  # assigning a ticket updates it
        }})
        me = data["viewer"]["id"]
        out = []
        for n in data["issues"]["nodes"]:
            stamps = [parse_time(h["createdAt"]) for h in n["history"]["nodes"] if h.get("toAssigneeId") == me]
            at = max((t for t in stamps if t), default=None)
            if at and at >= since and at.timestamp() > acked.get(n["identifier"], 0):
                n["assignedAt"] = at.timestamp()
                out.append(n)
        out.sort(key=lambda n: n["assignedAt"], reverse=True)
        return out

    async def issues(self, acked: dict[str, float] | None = None) -> Feed:
        """The tile's tickets. With `acked` (ticket id -> when it was opened), tickets assigned to me
        lately and not opened since come first with the NEW tag, even if the filter would hide them."""
        key = get_secret("linear", env="LINEAR_API_KEY")
        if not key:
            raise NotConfigured("connect Linear to see your issues")
        limit = int(self.cfg.get("limit", 100))
        nodes: list[dict] = []
        after = None
        total_more = False
        async with httpx.AsyncClient(timeout=30) as client:
            while True:  # page through everything up to a safety cap, then sort locally
                resp = await client.post(
                    "https://api.linear.app/graphql",
                    json={"query": LINEAR_QUERY, "variables": {"filter": self._filter(), "after": after}},
                    headers={"Authorization": key, "Content-Type": "application/json"},
                )
                data = resp.json()
                if data.get("errors"):
                    raise SourceError(f"linear: {data['errors'][0].get('message', '')[:80]}")
                page = data["data"]["issues"]
                nodes += page["nodes"]
                if not page["pageInfo"]["hasNextPage"]:
                    break
                if len(nodes) >= 1000:
                    total_more = True
                    break
                after = page["pageInfo"]["endCursor"]
        # facets for the filter popup: every state name / priority in what came back, with counts
        facets: dict = {"states": {}, "priorities": {}}
        for n in nodes:
            st = n["state"]
            key = st["name"]
            entry = facets["states"].setdefault(key, {"type": st["type"], "count": 0})
            entry["count"] += 1
            facets["priorities"][n["priority"] or 0] = facets["priorities"].get(n["priority"] or 0, 0) + 1
        # the filter: hide state names, keep only chosen priorities
        hidden = {h.lower() for h in self.cfg.get("hide_states") or []}
        keep_prio = {int(p) for p in self.cfg.get("priorities") or []}
        fetched = len(nodes)
        nodes = [n for n in nodes if n["state"]["name"].lower() not in hidden
                 and (not keep_prio or (n["priority"] or 0) in keep_prio)]
        sort = self.cfg.get("sort", "status")
        prio = lambda n: n["priority"] or 9  # 0 = no priority, sorts last
        if sort == "priority":
            nodes.sort(key=lambda n: (prio(n), STATE_ORDER.get(n["state"]["type"], 9)))
        elif sort == "updated":
            nodes.sort(key=lambda n: n["updatedAt"], reverse=True)
        elif sort == "due":
            nodes.sort(key=lambda n: (n.get("dueDate") or "9999", prio(n)))
        else:
            nodes.sort(key=lambda n: (STATE_ORDER.get(n["state"]["type"], 9), prio(n), n["updatedAt"][::-1]))
        scope = self.cfg.get("scope", "assigned")
        fresh: list[dict] = []
        if acked is not None:
            try:
                fresh = await self.newly_assigned(acked)
            except Exception:  # NEW tags are extra; never lose the tile over them
                fresh = []
        fresh_ids = {n["identifier"] for n in fresh}
        nodes = [n for n in nodes if n["identifier"] not in fresh_ids]
        items = []
        for n in fresh:
            it = issue_item(n, show_assignee=scope != "assigned")
            it.flash = True
            it.ref["assigned_at"] = n["assignedAt"]
            it.meta = it.meta.rsplit(" · ", 1)[0] + " · assigned " + \
                ago(datetime.fromtimestamp(n["assignedAt"], timezone.utc))
            items.append(it)
        items += [issue_item(n, show_assignee=scope != "assigned") for n in nodes[:max(limit - len(items), 0)]]
        nodes = fresh + nodes
        active = sum(1 for n in nodes if n["state"]["type"] == "started")
        notes = []
        if hidden or keep_prio:
            parts = []
            if hidden:
                parts.append("hiding " + ", ".join(sorted(h.title() for h in hidden)))
            if keep_prio:
                parts.append("only " + "/".join(dict(PRIORITY_NAMES)[p] for p in sorted(keep_prio, key=lambda p: p or 9)))
            notes.append(f"{' · '.join(parts)} ({max(fetched - len(nodes), 0)} filtered) · f to change")
        if len(nodes) > len(items):
            notes.append(f"showing {len(items)} of {len(nodes)}{'+' if total_more else ''} · raise the limit in Settings")
        return Feed(items=items, count=len(nodes), note=" · ".join(notes) or None, active=active,
                    extra={"facets": facets})
