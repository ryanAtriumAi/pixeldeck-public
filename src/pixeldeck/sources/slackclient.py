"""A small Slack client for one person, driven by a user token (xoxp-).

Everything here acts as you: list your conversations, read them, send / reply / edit /
delete your own messages, react, start DMs and mark conversations read.

Slack has no single "what's unread" call for apps, so conversation state is refreshed in
the background a few conversations at a time (DMs first), staying well under the
per-method rate limits. Freshly opened conversations are always fetched immediately.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ..config import get_secret
from .base import Feed, Item, NotConfigured, SourceError, ago

API = "https://slack.com/api"


@dataclass
class Convo:
    id: str
    name: str  # "#general", "@dana", "dana, sam, lee"
    kind: str  # channel | private | im | mpim
    user: str | None = None  # the other person in a DM
    last_read: str = "0"
    latest_ts: str = "0"
    latest_text: str = ""
    latest_user: str = ""
    unread: int = 0
    refreshed: float = 0.0
    is_member: bool = True

    @property
    def has_unread(self) -> bool:
        return self.unread > 0 or float(self.latest_ts or 0) > float(self.last_read or 0)

    @property
    def is_dm(self) -> bool:
        return self.kind in ("im", "mpim")


@dataclass
class Message:
    ts: str
    user: str
    text: str
    thread_ts: str | None = None
    reply_count: int = 0
    reactions: list[tuple[str, int, bool]] = field(default_factory=list)  # (name, count, mine)
    edited: bool = False
    subtype: str | None = None
    files: list[str] = field(default_factory=list)

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), tz=timezone.utc)


def _default_db():
    from .slackstore import DB_PATH

    return DB_PATH


class SlackClient:
    def __init__(self, cfg: dict, transport: httpx.AsyncBaseTransport | None = None, token: str | None = None) -> None:
        self.cfg = cfg
        self._transport = transport
        self._token = token
        self.me: str | None = None
        self.team: str | None = None
        self.team_url: str | None = None
        self.scopes: set[str] = set()
        self.users: dict[str, str] = {}
        self.bots: set[str] = {"USLACKBOT"}  # app / bot accounts
        self.hide_apps = bool(cfg.get("hide_apps", True))
        # Slack doesn't expose the sidebar's Starred section to apps, so stars live in config.
        self.starred: set[str] = {s.lower() for s in cfg.get("starred", [])}
        self.convos: dict[str, Convo] = {}
        self._lock = asyncio.Lock()
        self._listed = 0.0
        self._sem = asyncio.Semaphore(4)
        self._prev_latest: dict[str, str] | None = None  # convo -> latest ts at the previous sync
        self.fresh: set[str] = set()  # convos with a new DM / mention you haven't opened yet
        self.arrived: list[Convo] = []
        # DMs quiet for longer than this drop off the lists (still findable by name); 0 = keep all
        self.dm_days = int(cfg.get("dm_days", 7))
        # tests hand in a token + fake transport: keep their state in memory, not your real cache
        from .slackstore import SlackStore

        self.store = SlackStore(cfg.get("db") or (":memory:" if token else _default_db()))
        self._load_store()

    def _load_store(self) -> None:
        """Start from what we knew last time: people, conversations, unread state."""
        st = self.store
        self.me, self.team, self.team_url = st.get("me"), st.get("team"), st.get("team_url")
        users, bots = st.load_users()
        if users and time.time() - float(st.get("users_at", "0")) < 86400:
            self.users, self.bots = users, bots | {"USLACKBOT"}
        for row in st.load_convos():
            self.convos[row["id"]] = Convo(**{**row, "unread": int(row["unread"] or 0),
                                               "refreshed": float(row["refreshed"] or 0)})
        self._listed = float(st.get("listed_at", "0"))
        if self.convos:
            # anything newer than this on the next sync arrived while pixeldeck was closed
            self._prev_latest = {c.id: c.latest_ts for c in self.convos.values() if c.refreshed}

    def recent(self, c: Convo) -> bool:
        """Channels always count; DMs only if unread or active within dm_days."""
        if not c.is_dm or not self.dm_days or c.has_unread:
            return True
        return float(c.latest_ts or 0) >= time.time() - self.dm_days * 86400

    # ── plumbing ─────────────────────────────────────────────────────────────

    def token(self) -> str:
        tok = self._token or get_secret("slack", env="SLACK_TOKEN")
        if not tok:
            raise NotConfigured("connect Slack to see and answer DMs, channels and mentions")
        return tok

    async def call(self, method: str, http: str = "GET", **params) -> dict:
        tok = self.token()
        async with self._sem:
            for attempt in range(3):
                async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
                    if http == "GET":
                        r = await c.get(f"{API}/{method}", params=params, headers={"Authorization": f"Bearer {tok}"})
                    else:
                        r = await c.post(f"{API}/{method}", json=params,
                                         headers={"Authorization": f"Bearer {tok}",
                                                  "Content-Type": "application/json; charset=utf-8"})
                if r.status_code == 429:  # rate limited: Slack says how long to wait
                    await asyncio.sleep(min(float(r.headers.get("Retry-After", "2")), 30))
                    continue
                data = r.json()
                if "x-oauth-scopes" in r.headers:
                    self.scopes = {s.strip() for s in r.headers["x-oauth-scopes"].split(",") if s.strip()}
                if not data.get("ok"):
                    err = data.get("error", "unknown_error")
                    if err == "missing_scope":
                        raise SourceError(f"Slack {method} needs the `{data.get('needed', '?')}` scope "
                                          "(update the app manifest, then reinstall)")
                    raise SourceError(f"Slack {method}: {err}")
                return data
        raise SourceError(f"Slack {method}: still rate limited")

    async def ensure_identity(self) -> None:
        if self.me:
            return
        d = await self.call("auth.test")
        self.me, self.team, self.team_url = d["user_id"], d.get("team_id"), d.get("url")
        self.store.put(me=self.me, team=self.team, team_url=self.team_url)

    async def ensure_users(self) -> None:
        if self.users:
            return
        cursor = None
        while True:
            params = {"limit": 200, **({"cursor": cursor} if cursor else {})}
            try:
                d = await self.call("users.list", **params)
            except SourceError:
                return  # names fall back to IDs
            for u in d.get("members", []):
                prof = u.get("profile") or {}
                self.users[u["id"]] = prof.get("display_name") or u.get("real_name") or u.get("name") or u["id"]
                if u.get("is_bot") or u.get("is_app_user"):
                    self.bots.add(u["id"])
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                self.store.save_users(self.users, self.bots)
                return

    def name(self, uid: str | None) -> str:
        if not uid:
            return "?"
        if uid == self.me:
            return "you"
        return self.users.get(uid, uid)

    def render_text(self, text: str) -> str:
        """Slack mrkdwn → readable plain text (names resolved)."""
        t = re.sub(r"<@(\w+)(?:\|[^>]*)?>", lambda m: "@" + self.users.get(m.group(1), m.group(1)), text or "")
        t = re.sub(r"<#\w+\|([^>]*)>", r"#\1", t)
        t = re.sub(r"<!(here|channel|everyone)[^>]*>", r"@\1", t)
        t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", t)
        t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
        return html.unescape(t)

    def is_app(self, user: str | None, msg: dict | None = None) -> bool:
        """True for messages / people that are apps, bots or Slackbot."""
        if msg and (msg.get("bot_id") or msg.get("subtype") == "bot_message"):
            return True
        return bool(user) and (user in self.bots or user.startswith("B"))

    def app_dm(self, c: Convo) -> bool:
        return c.kind == "im" and self.is_app(c.user)

    def is_starred(self, c: Convo | None) -> bool:
        return bool(c) and (c.id.lower() in self.starred or c.name.lower() in self.starred)

    def group(self, c: Convo | None) -> int:
        """Notification order: 0 DMs, 1 starred channels, 2 everything else."""
        if c and c.is_dm:
            return 0
        return 1 if self.is_starred(c) else 2

    def visible(self, c: Convo) -> bool:
        return not (self.hide_apps and self.app_dm(c))

    def mentions_me(self, text: str) -> bool:
        return bool(self.me and f"<@{self.me}" in (text or ""))

    def label(self, ch: dict) -> str:
        """Readable name for a conversation as Slack search returns it."""
        c = self.convos.get(ch.get("id", ""))
        if c:
            return c.name
        raw = ch.get("name", "") or ""
        if ch.get("is_im") or re.fullmatch(r"[UW][A-Z0-9]{6,}", raw):
            return "@" + self.users.get(raw, raw)
        if ch.get("is_mpim") or raw.startswith("mpdm-"):
            people = [p for p in raw.removeprefix("mpdm-").rsplit("-", 1)[0].split("--") if p]
            return ", ".join(people) or raw
        return "#" + raw

    def deeplink(self, convo: str, ts: str | None = None) -> str | None:
        if not self.team:
            return None
        link = f"slack://channel?team={self.team}&id={convo}"
        return link + (f"&message={ts}" if ts else "")

    # ── conversations ────────────────────────────────────────────────────────

    async def list_convos(self, force: bool = False) -> None:
        if not force and self.convos and time.time() - self._listed < 600:
            return
        await self.ensure_identity()
        await self.ensure_users()
        cursor, seen = None, set()
        while True:
            params = {"types": "public_channel,private_channel,mpim,im", "exclude_archived": "true",
                      "limit": 200, **({"cursor": cursor} if cursor else {})}
            d = await self.call("users.conversations", **params)
            for ch in d.get("channels", []):
                cid = ch["id"]
                seen.add(cid)
                if ch.get("is_im"):
                    kind, name, user = "im", "@" + self.name(ch.get("user")), ch.get("user")
                elif ch.get("is_mpim"):
                    kind, user = "mpim", None
                    raw = ch.get("name", "")
                    name = ", ".join(p for p in raw.removeprefix("mpdm-").rsplit("-", 1)[0].split("--") if p) or raw
                else:
                    kind, user = ("private" if ch.get("is_private") else "channel"), None
                    name = "#" + ch.get("name", cid)
                if ch.get("is_im") and ch.get("is_user_deleted"):
                    continue
                c = self.convos.get(cid)
                if c:
                    c.name, c.kind, c.user = name, kind, user
                else:
                    self.convos[cid] = Convo(cid, name, kind, user)
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        for gone in set(self.convos) - seen:
            del self.convos[gone]
        self._listed = time.time()
        self.store.save_convos(list(self.convos.values()), replace=True)
        self.store.put(listed_at=self._listed)

    async def refresh_convo(self, c: Convo) -> None:
        info, hist = await asyncio.gather(
            self.call("conversations.info", channel=c.id),
            self.call("conversations.history", channel=c.id, limit=8),
            return_exceptions=True,
        )
        if isinstance(info, dict):
            ch = info.get("channel", {})
            c.last_read = ch.get("last_read") or c.last_read
            c.unread = int(ch.get("unread_count_display") or ch.get("unread_count") or 0)
        if isinstance(hist, dict) and hist.get("messages"):
            msgs = hist["messages"]
            if self.hide_apps:
                people = [m for m in msgs if not self.is_app(m.get("user"), m)]
                # app posts don't count as unread; show the newest message from a person
                c.unread = sum(1 for m in people if float(m["ts"]) > float(c.last_read or 0))
                msgs = people or []
            if msgs:
                m = msgs[0]
                c.latest_ts, c.latest_text, c.latest_user = m["ts"], m.get("text", ""), m.get("user") or m.get("bot_id", "")
            elif self.hide_apps:
                c.latest_ts, c.latest_text, c.latest_user = "0", "", ""
        c.refreshed = time.time()

    async def refresh_some(self, budget: int = 10) -> int:
        """Refresh the stalest conversations, DMs first. Returns how many are still never-synced."""
        await self.list_convos()
        order = sorted((c for c in self.convos.values() if self.visible(c)),
                       key=lambda c: (c.refreshed > 0, not c.is_dm, c.refreshed))
        batch = order[:budget]
        await asyncio.gather(*(self.refresh_convo(c) for c in batch), return_exceptions=True)
        self.store.save_convos(batch)
        return sum(1 for c in self.convos.values() if not c.refreshed and self.visible(c))

    def ranked(self, include_old: bool = False) -> list[Convo]:
        """DMs, then starred channels, then the rest; unread first and newest first within each.
        App DMs are left out while hide_apps is on; quiet DMs too unless include_old."""
        return sorted((c for c in self.convos.values() if self.visible(c) and (include_old or self.recent(c))),
                      key=lambda c: (self.group(c), not c.has_unread, -float(c.latest_ts or 0)))

    # ── messages ─────────────────────────────────────────────────────────────

    def _msg(self, m: dict) -> Message:
        return Message(
            ts=m["ts"],
            user=m.get("user") or m.get("bot_id") or "",
            text=m.get("text", ""),
            thread_ts=m.get("thread_ts"),
            reply_count=int(m.get("reply_count") or 0),
            reactions=[(r["name"], int(r.get("count", 0)), bool(self.me and self.me in r.get("users", [])))
                       for r in m.get("reactions", [])],
            edited=bool(m.get("edited")),
            subtype=m.get("subtype"),
            files=[f.get("name") or f.get("title") or "file" for f in m.get("files", [])],
        )

    async def history(self, convo: str, limit: int = 60) -> list[Message]:
        d = await self.call("conversations.history", channel=convo, limit=limit)
        msgs = [self._msg(m) for m in reversed(d.get("messages", []))]  # oldest first
        self.store.save_messages(convo, None, msgs)
        return msgs

    def cached(self, convo: str, thread_ts: str | None = None) -> list[Message]:
        """Messages from the local store, for an instant first paint."""
        return [Message(**m) for m in self.store.load_messages(convo, thread_ts)]

    async def replies(self, convo: str, thread_ts: str, limit: int = 100) -> list[Message]:
        d = await self.call("conversations.replies", channel=convo, ts=thread_ts, limit=limit)
        msgs = [self._msg(m) for m in d.get("messages", [])]
        self.store.save_messages(convo, thread_ts, msgs)
        return msgs

    async def post(self, convo: str, text: str, thread_ts: str | None = None) -> Message:
        params = {"channel": convo, "text": text, **({"thread_ts": thread_ts} if thread_ts else {})}
        d = await self.call("chat.postMessage", http="POST", **params)
        return self._msg(d["message"] | {"ts": d["ts"]})

    async def edit(self, convo: str, ts: str, text: str) -> None:
        await self.call("chat.update", http="POST", channel=convo, ts=ts, text=text)

    async def delete(self, convo: str, ts: str) -> None:
        await self.call("chat.delete", http="POST", channel=convo, ts=ts)

    async def react(self, convo: str, ts: str, emoji: str, remove: bool = False) -> None:
        await self.call("reactions.remove" if remove else "reactions.add", http="POST",
                        channel=convo, timestamp=ts, name=emoji.strip(":"))

    async def mark_read(self, convo: str, ts: str) -> None:
        await self.call("conversations.mark", http="POST", channel=convo, ts=ts)
        c = self.convos.get(convo)
        if c:
            c.last_read, c.unread = ts, 0
            self.store.save_convos([c])

    async def open_dm(self, users: list[str]) -> str:
        d = await self.call("conversations.open", http="POST", users=",".join(users))
        cid = d["channel"]["id"]
        if cid not in self.convos:
            kind = "im" if len(users) == 1 else "mpim"
            name = "@" + self.name(users[0]) if kind == "im" else ", ".join(self.name(u) for u in users)
            self.convos[cid] = Convo(cid, name, kind, users[0] if kind == "im" else None)
        return cid

    async def mentions(self, limit: int = 15) -> list[dict]:
        await self.ensure_identity()
        try:
            d = await self.call("search.messages", query=f"<@{self.me}>", sort="timestamp", count=limit)
        except SourceError:
            return []
        return d.get("messages", {}).get("matches", [])

    # ── dashboard feed ───────────────────────────────────────────────────────

    async def feed(self, budget: int = 10) -> Feed:
        pending = await self.refresh_some(budget)
        mentions = await self.mentions()
        items: list[Item] = []
        seen: set[tuple[str, str]] = set()
        for c in self.ranked():
            if not c.refreshed or c.latest_ts == "0":
                continue
            if not c.has_unread and not c.is_dm and len(items) >= 12:
                continue
            when = datetime.fromtimestamp(float(c.latest_ts), tz=timezone.utc)
            who = self.name(c.latest_user)
            mention = self.mentions_me(c.latest_text)
            unread = c.has_unread
            badge = f" · {c.unread} unread" if c.unread else (" · unread" if unread else "")
            items.append(Item(
                title=f"{c.name}  {self.render_text(c.latest_text)}"[:200],
                meta=f"{who} · {ago(when)}{badge}",
                url=self.deeplink(c.id),
                icon="@" if mention else ("✦" if c.is_dm else "#"),
                tone=("accent" if unread else "muted") if c.is_dm or mention else ("secondary" if unread else "muted"),
                unread=unread,
                when=when,
                ref={"kind": "slack_convo", "id": c.id, "ts": c.latest_ts},
            ))
            seen.add((c.id, c.latest_ts))
        for m in mentions:
            ch = m.get("channel", {})
            if (ch.get("id"), m.get("ts")) in seen:
                continue
            if self.hide_apps and self.is_app(m.get("user"), m):
                continue
            when = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)
            if any(i.when and i.when > when for i in items) and len(items) >= 25:
                continue
            items.append(Item(
                title=f"{self.label(ch)}  {self.render_text(m.get('text', ''))}"[:200],
                meta=f"{m.get('username', '?')} · mentioned you · {ago(when)}",
                url=m.get("permalink"),
                icon="@", tone="accent", when=when,
                ref={"kind": "slack_convo", "id": ch.get("id"), "ts": m["ts"],
                     "thread_ts": (re.search(r"thread_ts=([\d.]+)", m.get("permalink") or "") or [None, None])[1]},
            ))
        # new DMs / mentions since the previous sync pulse until you open them
        self.arrived = []
        current = {c.id: c.latest_ts for c in self.convos.values() if c.refreshed}
        if self._prev_latest is not None:
            for cid, ts in current.items():
                c = self.convos[cid]
                old = self._prev_latest.get(cid)
                if old is not None and float(ts) > float(old) and c.latest_user != self.me \
                        and self.visible(c) and not (self.hide_apps and self.is_app(c.latest_user)) \
                        and (c.is_dm or self.mentions_me(c.latest_text)):
                    self.fresh.add(cid)
                    self.arrived.append(c)
        self._prev_latest = {**(self._prev_latest or {}), **current}
        for cid in list(self.fresh):
            c = self.convos.get(cid)
            if not c or not c.has_unread:
                self.fresh.discard(cid)
        for i in items:
            if (i.ref or {}).get("id") in self.fresh and i.unread:
                i.flash = True
        def order(i: Item):
            c = self.convos.get((i.ref or {}).get("id", ""))
            return (self.group(c), not i.unread, -(i.when.timestamp() if i.when else 0))

        items.sort(key=order)
        for i in items:  # mark starred-channel rows
            c = self.convos.get((i.ref or {}).get("id", ""))
            if c and not c.is_dm and self.is_starred(c) and i.icon == "#":
                i.icon = "★"
        shown = [c for c in self.convos.values() if self.visible(c)]
        unread = sum(1 for c in shown if c.refreshed and c.has_unread and (c.is_dm or c.unread))
        note = f"syncing conversations… {len(shown) - pending}/{len(shown)}" if pending else None
        return Feed(items=items[:30], count=unread, note=note)
