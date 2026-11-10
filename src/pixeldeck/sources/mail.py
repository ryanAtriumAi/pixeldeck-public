"""Mail and calendar: Microsoft 365 (Graph), any IMAP account, and Apple Mail."""

from __future__ import annotations

import asyncio
import email.header
import email.utils
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from ..config import CONFIG_DIR, get_secret
from .base import Feed, Item, NotConfigured, SourceError, ago, parse_time, run_cmd

GRAPH = "https://graph.microsoft.com/v1.0"


def _cache_lock():
    """Exclusive lock shared by every pixeldeck process touching the sign-in cache."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held():
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH.with_suffix(".lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    return held()


def _recover(raw: str) -> str:
    """A damaged cache (e.g. stray bytes after an overlapping write) keeps its first intact JSON
    document; one that can't be read at all is set aside so you're simply asked to sign in again."""
    import json

    try:
        json.loads(raw)
        return raw
    except ValueError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        if isinstance(obj, dict):
            fixed = json.dumps(obj)
            with _cache_lock():
                tmp = CACHE_PATH.with_suffix(".repair.tmp")
                tmp.write_text(fixed)
                os.chmod(tmp, 0o600)
                tmp.replace(CACHE_PATH)
            return fixed
    except ValueError:
        pass
    CACHE_PATH.replace(CACHE_PATH.with_suffix(".corrupt"))
    return "{}"


class NeedsConsent(Exception):
    """A Graph call needs a scope the user hasn't granted yet."""
SCOPES = ["Mail.Read", "Calendars.Read", "User.Read"]
CACHE_PATH = CONFIG_DIR / "msal_cache.bin"


# ── Filter ───────────────────────────────────────────────────────────────────


class MailFilter:
    """Hide mail by sender ("github.com" = that domain and its subdomains,
    "x@y.com" = that address) or by subject substring."""

    def __init__(self, cfg: dict) -> None:
        self.senders = [s.lower().lstrip("@") for s in cfg.get("hide_from", [])]
        self.subjects = [s.lower() for s in cfg.get("hide_subjects", [])]

    def hides(self, addr: str, subject: str = "") -> bool:
        addr = (addr or "").lower()
        domain = addr.rpartition("@")[2]
        for pat in self.senders:
            if "@" in pat:
                if addr == pat:
                    return True
            elif domain == pat or domain.endswith("." + pat):
                return True
        subj = (subject or "").lower()
        return any(p in subj for p in self.subjects)


NO_FILTER = MailFilter({})


# ── Microsoft 365 ────────────────────────────────────────────────────────────


class Outlook:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._app = None
        self._cache = None

    def _msal(self):
        if self._app is None:
            import msal

            self._cache = msal.SerializableTokenCache()
            if CACHE_PATH.exists():
                with _cache_lock():
                    raw = CACHE_PATH.read_text()
                self._cache.deserialize(_recover(raw))
            self._app = msal.PublicClientApplication(
                self.cfg["client_id"], authority=self.cfg["authority"], token_cache=self._cache
            )
        return self._app

    def _save(self) -> None:
        """Write the sign-in cache atomically under a lock, so pixeldeck processes running side by
        side (dashboard, settings, a second window) can't interleave and corrupt it."""
        if self._cache is not None and self._cache.has_state_changed:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _cache_lock():
                tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(self._cache.serialize())
                os.chmod(tmp, 0o600)
                tmp.replace(CACHE_PATH)
            self._cache.has_state_changed = False

    def login(self) -> None:
        """Interactive device-code login, run from `pixeldeck login outlook`."""
        app = self._msal()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"device flow failed: {flow.get('error_description', flow)}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise SystemExit(f"login failed: {result.get('error_description', result)}")
        self._save()
        claims = result.get("id_token_claims", {})
        print(f"signed in as {claims.get('preferred_username', 'unknown')}")

    def token_for(self, scopes: list[str]) -> str | None:
        """Silent token for extra scopes (e.g. Mail.Send); None means the user must consent first."""
        app = self._msal()
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(scopes, account=accounts[0])
        self._save()
        return result.get("access_token") if result else None

    async def reply(self, message_id: str, text: str, reply_all: bool) -> None:
        import html as _html

        token = await asyncio.to_thread(self.token_for, ["Mail.Send"])
        if not token:
            raise NeedsConsent("Mail.Send")
        comment = _html.escape(text).replace("\n", "<br>")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GRAPH}/me/messages/{message_id}/{'replyAll' if reply_all else 'reply'}",
                json={"comment": comment},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                msg = resp.text
            raise SourceError(f"Graph {resp.status_code}: {msg[:160]}")

    def _token(self) -> str:
        app = self._msal()
        accounts = app.get_accounts()
        if not accounts:
            raise NotConfigured("sign in to Microsoft 365")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        self._save()
        if not result or "access_token" not in result:
            raise NotConfigured("Microsoft 365 session expired, sign in again")
        return result["access_token"]

    async def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> dict:
        token = await asyncio.to_thread(self._token)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{GRAPH}{path}", params=params, headers={"Authorization": f"Bearer {token}", **(headers or {})}
            )
        if resp.status_code >= 400:
            raise SourceError(f"Graph {resp.status_code}: {resp.json().get('error', {}).get('message', '')[:80]}")
        return resp.json()

    async def _get_bytes(self, path: str) -> bytes:
        """Raw bytes of a Graph resource, e.g. an attachment's /$value."""
        token = await asyncio.to_thread(self._token)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 400:
            raise SourceError(f"Graph {resp.status_code} fetching attachment")
        return resp.content

    async def inbox(self, flt: MailFilter = NO_FILTER, limit: int = 30) -> Feed:
        # Fetch a deep page so there is still a full list once the noise is filtered out,
        # and count unread from the messages themselves so hidden mail doesn't inflate it.
        unread, msgs = await asyncio.gather(
            self._get(
                "/me/mailFolders/inbox/messages",
                {"$filter": "isRead eq false", "$select": "from,subject", "$top": "500"},
            ),
            self._get(
                "/me/mailFolders/inbox/messages",
                {
                    "$top": "150",
                    "$orderby": "receivedDateTime desc",
                    "$select": "subject,from,receivedDateTime,isRead,webLink,importance,hasAttachments",
                },
            ),
        )

        def addr(m: dict) -> str:
            return (m.get("from") or {}).get("emailAddress", {}).get("address", "")

        count = sum(1 for m in unread.get("value", []) if not flt.hides(addr(m), m.get("subject", "")))
        items = []
        for m in msgs.get("value", []):
            if flt.hides(addr(m), m.get("subject", "")):
                continue
            if len(items) >= limit:
                break
            sender = (m.get("from") or {}).get("emailAddress", {})
            when = parse_time(m["receivedDateTime"])
            flags = " ⚑" if m.get("importance") == "high" else ""
            flags += " ⧉" if m.get("hasAttachments") else ""
            items.append(
                Item(
                    title=(m.get("subject") or "(no subject)") + flags,
                    meta=f"{sender.get('name') or sender.get('address', '?')} · {ago(when)}",
                    url=m.get("webLink"),
                    ref={"kind": "graph_mail", "id": m["id"]},
                    icon="✉" if not m["isRead"] else "·",
                    tone="accent" if not m["isRead"] else "muted",
                    unread=not m["isRead"],
                    when=when,
                    sender=sender.get("address", ""),
                )
            )
        return Feed(items=items, count=count)

    async def calendar_view(self, start: datetime, end: datetime) -> list[dict]:
        """Every event between start and end (UTC times), for the agenda page."""
        data = await self._get(
            "/me/calendarView",
            {
                "startDateTime": start.astimezone(timezone.utc).isoformat(),
                "endDateTime": end.astimezone(timezone.utc).isoformat(),
                "$orderby": "start/dateTime",
                "$top": "250",
                "$select": "id,subject,start,end,isAllDay,location,attendees,organizer,isOnlineMeeting,onlineMeeting,"
                           "webLink,bodyPreview,showAs,isCancelled,responseStatus,isOrganizer,categories",
            },
        )
        return [e for e in data.get("value", []) if not e.get("isCancelled")]

    async def create_event(self, event: dict) -> dict:
        """POST /me/events. Needs Calendars.ReadWrite, asked for the first time you create one."""
        token = await asyncio.to_thread(self.token_for, ["Calendars.ReadWrite"])
        if not token:
            raise NeedsConsent("Calendars.ReadWrite")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{GRAPH}/me/events", json=event, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                msg = resp.text
            raise SourceError(f"Graph {resp.status_code}: {msg[:160]}")
        return resp.json()

    async def agenda(self) -> Feed:
        now = datetime.now(timezone.utc)
        data = await self._get(
            "/me/calendarView",
            {
                "startDateTime": (now - timedelta(minutes=30)).isoformat(),
                "endDateTime": (now + timedelta(hours=36)).isoformat(),
                "$orderby": "start/dateTime",
                "$top": "25",
                "$select": "subject,start,end,location,isOnlineMeeting,onlineMeeting,webLink,isCancelled,showAs",
            },
        )
        items = []
        for ev in data.get("value", []):
            if ev.get("isCancelled") or ev.get("showAs") == "free":
                continue
            start = parse_time(ev["start"]["dateTime"])
            end = parse_time(ev["end"]["dateTime"])
            if end and end < now:
                continue
            live = start and start <= now
            local = start.astimezone() if start else None
            day = "" if local and local.date() == datetime.now().date() else (local.strftime("%a ") if local else "")
            when = f"{day}{local:%H:%M}" if local else "?"
            join = (ev.get("onlineMeeting") or {}).get("joinUrl")
            loc = (ev.get("location") or {}).get("displayName") or ("online" if ev.get("isOnlineMeeting") else "")
            items.append(
                Item(
                    title=f"{when}  {ev.get('subject') or '(busy)'}",
                    meta=("● LIVE NOW · " if live else f"{ago(start)} · ") + (loc or "—"),
                    url=join or ev.get("webLink"),
                    ref={"kind": "graph_event", "id": ev["id"], "join": join},
                    icon="▶" if live else "◷",
                    tone="accent" if live else "secondary",
                    when=start,
                )
            )
        upcoming = sum(1 for i in items if i.when and i.when.date() == now.astimezone().date())
        return Feed(items=items, count=upcoming)


# ── IMAP ─────────────────────────────────────────────────────────────────────


def _decode(value: str | None) -> str:
    if not value:
        return ""
    return str(email.header.make_header(email.header.decode_header(value)))


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_fetch(acct: dict, password: str, flt: MailFilter, limit: int = 15) -> Feed:
    conn = imaplib.IMAP4_SSL(acct["host"], int(acct.get("port", 993)), timeout=20)
    try:
        conn.login(acct["user"], password)
        conn.select("INBOX", readonly=True)
        # Server-side filter: NOT FROM / NOT SUBJECT are substring matches in IMAP SEARCH.
        nots: list[str] = []
        for pat in flt.senders:
            nots += ["NOT", "FROM", _imap_quote(pat)]
        for pat in flt.subjects:
            nots += ["NOT", "SUBJECT", _imap_quote(pat)]
        _, data = conn.uid("search", None, "UNSEEN", *nots)
        unseen = len(data[0].split())
        _, data = conn.uid("search", None, "ALL", *nots)
        uids = data[0].split()[-limit:]
        items = []
        if uids:
            _, rows = conn.uid(
                "fetch", b",".join(uids), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                meta, raw = row
                uid = re.search(rb"UID (\d+)", meta)
                msg = email.message_from_bytes(raw)
                seen = b"\\Seen" in meta
                name, addr = email.utils.parseaddr(_decode(msg.get("From")))
                try:
                    when = email.utils.parsedate_to_datetime(msg.get("Date"))
                except (TypeError, ValueError):
                    when = None
                items.append(
                    Item(
                        title=_decode(msg.get("Subject")) or "(no subject)",
                        meta=f"{name or addr} · {acct.get('name', acct['user'])} · {ago(when)}",
                        url=acct.get("web_url"),
                        ref={"kind": "imap", "acct": acct.get("name", acct["user"]), "uid": uid.group(1).decode() if uid else None},
                        icon="·" if seen else "✉",
                        tone="muted" if seen else "accent",
                        unread=not seen,
                        when=when,
                        sender=addr,
                    )
                )
        items = [i for i in items if not flt.hides(i.sender, i.title)]
        items.sort(key=lambda i: i.when or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return Feed(items=items, count=unseen)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def imap_inbox(acct: dict, flt: MailFilter = NO_FILTER) -> Feed:
    secret = acct.get("secret", f"imap-{acct.get('name', acct['user'])}")
    password = get_secret(secret)
    if not password:
        raise NotConfigured(f"IMAP password for {secret} is missing")
    return await asyncio.to_thread(_imap_fetch, acct, password, flt)


# ── Apple Mail ───────────────────────────────────────────────────────────────

APPLE_MAIL_SCRIPT = """
set out to ""
tell application "Mail"
  set u to count of (messages of inbox whose read status is false__WHERE__)
  set n to count of messages of inbox
  if n > __DEPTH__ then set n to __DEPTH__
  repeat with i from 1 to n
    set m to message i of inbox
    set out to out & (subject of m) & tab & (sender of m) & tab & ((date received of m) as «class isot» as string) & tab & (read status of m) & tab & (message id of m) & linefeed
  end repeat
end tell
return (u as string) & linefeed & out
"""


def _as_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def apple_mail_inbox(flt: MailFilter = NO_FILTER, limit: int = 30) -> Feed:
    where = "".join(f" and sender does not contain {_as_str(p)}" for p in flt.senders)
    where += "".join(f" and subject does not contain {_as_str(p)}" for p in flt.subjects)
    depth = 40 if not where else 90
    script = APPLE_MAIL_SCRIPT.replace("__WHERE__", where).replace("__DEPTH__", str(depth))
    out = await run_cmd("osascript", "-e", script, timeout=90)
    lines = out.splitlines()
    unread = int(lines[0]) if lines and lines[0].isdigit() else None
    items = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        subject, sender, date, read, mid = parts[:5]
        when = parse_time(date)
        if when:
            when = when.replace(tzinfo=None).astimezone()  # isot is local time
        name, addr = email.utils.parseaddr(sender)
        if flt.hides(addr, subject):
            continue
        if len(items) >= limit:
            break
        seen = read == "true"
        items.append(
            Item(
                title=subject or "(no subject)",
                meta=f"{name or addr} · Mail.app · {ago(when)}",
                url=f"message://%3c{quote(mid)}%3e",
                ref={"kind": "apple_mail", "mid": mid},
                icon="·" if seen else "✉",
                tone="muted" if seen else "accent",
                unread=not seen,
                when=when,
                sender=addr,
            )
        )
    return Feed(items=items, count=unread)


# ── Aggregate ────────────────────────────────────────────────────────────────


class Mail:
    def __init__(self, cfg: dict, outlook: Outlook | None) -> None:
        self.cfg = cfg
        self.outlook = outlook
        self.filter = MailFilter(cfg.get("mail", {}))
        from .mailstate import MailState

        self.state = MailState()

    def configured(self) -> bool:
        return bool(self.outlook or self.cfg.get("imap") or self.cfg["apple_mail"].get("enabled"))

    async def inbox(self) -> Feed:
        jobs = []
        flt = self.filter
        if self.outlook:
            jobs.append(self.outlook.inbox(flt))
        jobs += [imap_inbox(a, flt) for a in self.cfg.get("imap", [])]
        if self.cfg["apple_mail"].get("enabled"):
            jobs.append(apple_mail_inbox(flt))
        if not jobs:
            raise NotConfigured("connect Microsoft 365, an IMAP inbox or Apple Mail")
        results = await asyncio.gather(*jobs, return_exceptions=True)
        feeds = [r for r in results if isinstance(r, Feed)]
        errors = [r for r in results if isinstance(r, BaseException)]
        if not feeds and errors:
            raise errors[0]
        # The same account can arrive twice (e.g. Microsoft 365 directly and through
        # Mail.app). Keep the first copy (providers are in preference order). Match on subject
        # and arrival minute: Mail.app often reports Exchange senders without an address.
        def norm(t: str) -> str:
            return t.rstrip(" ⚑⧉").strip().lower()

        seen: set[tuple] = set()
        items, dup_unread, mirror_count = [], 0, 0
        for feed in feeds:
            dups = 0
            for item in feed.items:
                minute = int(item.when.timestamp() // 60) if item.when else None
                keys = {(norm(item.title), minute + d) for d in (-1, 0, 1)} if minute is not None else {(norm(item.title), None)}
                if keys & seen:
                    dups += 1
                    dup_unread += item.unread
                    continue
                seen.add((norm(item.title), minute))
                items.append(item)
            # A source whose mail is mostly copies is a mirror: its unread count is the same mail.
            if feed.items and dups >= len(feed.items) / 2:
                mirror_count += feed.count or 0
        items.sort(key=lambda i: i.when or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        items = items[:30]
        unread_before = sum(i.unread for i in items)
        arrived = self.state.apply(items)
        acked_unread = unread_before - sum(i.unread for i in items)  # opened here, still unread on the server
        mirrored_dups = 0 if not mirror_count else dup_unread  # already excluded with the mirror's count
        count = max(sum(f.count or 0 for f in feeds) - mirror_count - (dup_unread - mirrored_dups) - acked_unread, 0)
        note = f"⚠ {errors[0]}" if errors else None
        return Feed(items=items, count=count, note=note, arrived=arrived)

