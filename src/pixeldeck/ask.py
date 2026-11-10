"""Ask: a chat with the local model (LM Studio) that searches every connected source for you.

The model gets read-only tools, one per connector: the dashboard tiles as they are right now,
GitHub (PRs and issues in the repos you monitor), Microsoft 365 mail and calendar, Slack and
Linear, plus `read` to open any hit in full. Every hit gets a short id (s3, m1…) that the
answer cites and the chat screen links. Nothing here can send, change or delete anything.

Everything goes only to the configured local server (the same one reply assist uses).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

from .sources.base import Item, ago, parse_time, run_json

if TYPE_CHECKING:
    from .app import PixelDeck

RESULT_CHARS = 2800  # one tool result, before older ones get trimmed to fit the context
ANSWER_TOKENS = 1100
MAX_ROUNDS = 5  # tool rounds per question; the last round has to answer
TILES = {  # dashboard tile key → what it holds
    "reviews": "PRs waiting for your review",
    "mine": "your open PRs (CI, conflicts, review state)",
    "notifs": "GitHub notifications",
    "releases": "latest release and what each env runs",
    "mail": "inbox",
    "slack": "Slack DMs, channels and mentions",
    "agenda": "calendar, next 36 hours",
    "linear": "Linear issues assigned to you",
    "ci": "CI runs",
}
SOURCE_ICON = {"g": "◆", "m": "✉", "s": "#", "l": "▲", "c": "◷", "d": "▸"}


def _one_line(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _local(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%a %b %d %H:%M") if dt else "?"


@dataclass
class Hit:
    id: str
    item: Item
    source: str  # "GitHub", "Mail"…


@dataclass
class Turn:
    """One question and its answer, as the chat screen shows it."""

    question: str
    answer: str = ""
    trace: list[str] = field(default_factory=list)  # "⌕ Slack “staging deploy” · 12 hits"
    cited: list[str] = field(default_factory=list)  # hit ids, in the order the answer cites them
    seen: list[str] = field(default_factory=list)  # every hit id this turn's searches returned
    error: str | None = None
    secs: float = 0.0


# ── tools ────────────────────────────────────────────────────────────────────


def _fn(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


class Toolbox:
    """The read-only tools the model can call, and every hit they returned (by id)."""

    def __init__(self, app: PixelDeck) -> None:
        self.app = app
        self.hits: dict[str, Hit] = {}
        self._n: dict[str, int] = {}

    def _hit(self, prefix: str, item: Item, source: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        hid = f"{prefix}{self._n[prefix]}"
        self.hits[hid] = Hit(hid, item, source)
        return hid

    # which sources are connected ------------------------------------------------

    def sources(self) -> list[str]:
        app, out = self.app, ["dashboard", "GitHub"]
        if app.outlook:
            out += ["Microsoft 365 mail", "calendar"]
        from .config import get_secret

        if get_secret("slack", env="SLACK_TOKEN"):
            out.append("Slack")
        if get_secret("linear", env="LINEAR_API_KEY"):
            out.append("Linear")
        return out

    def specs(self) -> list[dict]:
        have = self.sources()
        q = {"query": {"type": "string", "description": "words to search for"}}
        out = [
            _fn("dashboard", "What PIXELDECK's tiles show right now: review queue, your PRs, GitHub inbox, "
                "releases/deploys per env, inbox, Slack, agenda, Linear, CI. Best for 'what needs my attention', "
                "'what's deployed where', 'what's next'.",
                {"tile": {"type": "string", "enum": list(TILES), "description": "one tile; leave out for all"}}),
            _fn("search_github", "Search pull requests and issues in the user's GitHub repos (titles and bodies). "
                "GitHub search syntax works: is:open, is:merged, author:login, label:x.",
                {**q, "type": {"type": "string", "enum": ["pr", "issue", "any"]}}, ["query"]),
        ]
        if "Microsoft 365 mail" in have:
            out.append(_fn("search_mail", "Search email (subject, body, people). KQL works: from:name, "
                           "subject:words, received>=2026-11-01.", q, ["query"]))
            out.append(_fn("calendar", "Calendar events between two dates, optionally only those matching words "
                           "(subject, location, organiser, attendees).",
                           {"start": {"type": "string", "description": "YYYY-MM-DD, default today"},
                            "end": {"type": "string", "description": "YYYY-MM-DD inclusive, default start + 7 days"},
                            "query": {"type": "string"}}))
        if "Slack" in have:
            out.append(_fn("search_slack", "Search Slack messages the user can see. Slack modifiers work: "
                           "from:@name, in:#channel, after:2026-11-01, during:week.", q, ["query"]))
        if "Linear" in have:
            out.append(_fn("search_linear", "Search Linear tickets (titles, descriptions, ids like ABC-123).",
                           {**q, "open_only": {"type": "boolean"}, "mine": {"type": "boolean",
                            "description": "only tickets assigned to the user"}}, ["query"]))
        out.append(_fn("read", "Open one hit in full by its id (an email's body, a ticket's description and "
                       "comments, a PR's description and checks, a Slack thread).",
                       {"id": {"type": "string", "description": "hit id like s3 or m1"}}, ["id"]))
        return out

    async def call(self, name: str, args: dict) -> tuple[str, str, list[str]]:
        """Run a tool. Returns (text for the model, one-line trace for the screen, hit ids)."""
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"unknown tool {name}", f"✖ unknown tool {name}", []
        try:
            text, trace, ids = await fn(**{k: v for k, v in args.items() if v not in (None, "")})
        except TypeError as exc:
            return f"bad arguments: {exc}", f"✖ {name}: bad arguments", []
        except Exception as exc:  # a source being down shouldn't end the answer
            msg = f"{type(exc).__name__}: {exc}"[:200]
            return f"error: {msg}", f"✖ {name}: {msg}", []
        if len(text) > RESULT_CHARS:
            text = text[:RESULT_CHARS].rsplit("\n", 1)[0] + "\n… (more results cut)"
        return text, trace, ids

    def _lines(self, prefix: str, source: str, items: list[Item], line: Callable[[Item], str]) -> tuple[str, list[str]]:
        ids, rows = [], []
        for it in items:
            hid = self._hit(prefix, it, source)
            ids.append(hid)
            rows.append(f"[{hid}] {line(it)}")
        return "\n".join(rows), ids

    # dashboard -------------------------------------------------------------------

    async def t_dashboard(self, tile: str | None = None) -> tuple[str, str, list[str]]:
        from .app import Tile

        blocks, ids = [], []
        for t in self.app.query(Tile):
            if tile and t.key != tile:
                continue
            items = t.feed.items if t.feed else []
            head = f"## {t.label} ({TILES.get(t.key, '')})"
            if t.feed is None:
                blocks.append(f"{head}\n(not loaded or not connected)")
                continue
            if t.feed.note:
                head += f"\nnote: {t.feed.note}"
            text, got = self._lines("d", t.label.title(), items[: 12 if tile else 6],
                                    lambda i: f"{_one_line(i.title, 110)} · {_one_line(i.meta, 90)}")
            more = f"\n(+{len(items) - len(got)} more)" if len(items) > len(got) else ""
            blocks.append(f"{head}\n{text or '(empty)'}{more}")
            ids += got
        return "\n\n".join(blocks) or "no such tile", f"▸ dashboard{' · ' + tile if tile else ''}", ids

    # GitHub ------------------------------------------------------------------------

    async def t_search_github(self, query: str, type: str = "any") -> tuple[str, str, list[str]]:
        gh = self.app.gh
        q = query
        if type in ("pr", "issue") and f"is:{type}" not in q:
            q += f" is:{type}"
        if "repo:" not in q:
            if gh.repos:
                q += " " + " ".join(f"repo:{r}" for r in sorted(gh.repos))
            else:
                q += " involves:@me"
        d = await run_json("gh", "api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=12")
        items = []
        for n in d.get("items", []):
            pr = n.get("pull_request")
            state = "merged" if pr and pr.get("merged_at") else n["state"]
            when = parse_time(n["updated_at"])
            repo = n["repository_url"].rsplit("/", 1)[-1]
            items.append(Item(
                title=f"{'PR' if pr else 'issue'} #{n['number']} {n['title']}",
                meta=f"{repo} · {state} · @{(n.get('user') or {}).get('login', '?')} · updated {ago(when)}",
                url=n["html_url"], when=when, icon="◆" if pr else "●",
                tone="muted" if state != "open" else "accent",
                ref={"kind": "pr" if pr else "issue", "url": n["html_url"]},
            ))
        text, ids = self._lines("g", "GitHub", items, lambda i: f"{i.title} · {i.meta}")
        return (text or "no matches"), f"⌕ GitHub “{query}” · {d.get('total_count', 0)} hits", ids

    # mail + calendar ------------------------------------------------------------------

    async def t_search_mail(self, query: str) -> tuple[str, str, list[str]]:
        ol = self.app.outlook
        if not ol:
            return "mail isn't connected", "✖ mail isn't connected", []
        term = query.replace('"', "").replace("\\", "")
        d = await ol._get("/me/messages", {
            "$search": f'"{term}"', "$top": "12",
            "$select": "subject,from,receivedDateTime,bodyPreview,webLink,isRead,hasAttachments,toRecipients",
        })
        items, previews = [], {}
        for m in d.get("value", []):
            s = (m.get("from") or {}).get("emailAddress", {})
            when = parse_time(m.get("receivedDateTime"))
            it = Item(title=m.get("subject") or "(no subject)",
                      meta=f"{s.get('name') or s.get('address', '?')} · {ago(when)}",
                      url=m.get("webLink"), when=when, icon="✉", tone="secondary", sender=s.get("address", ""),
                      ref={"kind": "graph_mail", "id": m["id"]})
            items.append(it)
            previews[id(it)] = _one_line(m.get("bodyPreview") or "", 170)
        items.sort(key=lambda i: i.when or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        text, ids = self._lines("m", "Mail", items, lambda i: (
            f"{_local(i.when)} · from {i.meta.split(' · ')[0]} <{i.sender}> · {_one_line(i.title, 100)}\n"
            f"    {previews[id(i)]}"))
        return (text or "no matches"), f"⌕ mail “{query}” · {len(ids)} hits", ids

    async def t_calendar(self, start: str | None = None, end: str | None = None,
                         query: str | None = None) -> tuple[str, str, list[str]]:
        ol = self.app.outlook
        if not ol:
            return "calendar isn't connected", "✖ calendar isn't connected", []
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

        def day(s: str | None, default: datetime) -> datetime:
            if not s:
                return default
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=today.tzinfo)

        a = day(start, today)
        b = day(end, a + timedelta(days=6)) + timedelta(days=1)
        if b <= a:
            b = a + timedelta(days=1)
        if (b - a).days > 62:
            b = a + timedelta(days=62)
        events = await ol.calendar_view(a, b)
        words = [w for w in (query or "").lower().split() if w]
        items, extra = [], {}
        for ev in events:
            people = [x.get("emailAddress", {}) for x in ev.get("attendees") or []]
            org = (ev.get("organizer") or {}).get("emailAddress", {})
            loc = (ev.get("location") or {}).get("displayName") or ""
            hay = " ".join([ev.get("subject") or "", loc, org.get("name", ""), org.get("address", ""),
                            *[p.get("name", "") + " " + p.get("address", "") for p in people]]).lower()
            if words and not all(w in hay for w in words):
                continue
            s, e = parse_time(ev["start"]["dateTime"]), parse_time(ev["end"]["dateTime"])
            join = (ev.get("onlineMeeting") or {}).get("joinUrl")
            span = "all day" if ev.get("isAllDay") else f"{_local(s)}–{e.astimezone():%H:%M}" if s and e else "?"
            it = Item(title=ev.get("subject") or "(busy)", meta=span, when=s, icon="◷", tone="secondary",
                      url=join or ev.get("webLink"), ref={"kind": "graph_event", "id": ev["id"], "join": join})
            items.append(it)
            mine = (ev.get("responseStatus") or {}).get("response", "")
            extra[id(it)] = " · ".join(x for x in [
                f"at {loc}" if loc else "", f"by {org.get('name', '')}" if org.get("name") else "",
                f"{len(people)} invited" if people else "", "Teams" if join else "",
                f"you: {mine}" if mine and mine != "none" else ""] if x)
        text, ids = self._lines("c", "Calendar", items[:30],
                                lambda i: f"{i.meta} · {_one_line(i.title, 90)} · {extra[id(i)]}")
        span = f"{a:%b %d}–{(b - timedelta(days=1)):%b %d}"
        more = f"\n(+{len(items) - 30} more)" if len(items) > 30 else ""
        return (text + more if text else f"no events {span}"), \
            f"◷ calendar {span}{' “' + query + '”' if query else ''} · {len(items)} events", ids

    # Slack ---------------------------------------------------------------------------------

    async def t_search_slack(self, query: str) -> tuple[str, str, list[str]]:
        sc = self.app.slack
        await sc.ensure_identity()
        await sc.ensure_users()
        d = await sc.call("search.messages", query=query, count=15, sort="score")
        found = d.get("messages", {})
        items = []
        for m in found.get("matches", []):
            if sc.hide_apps and sc.is_app(m.get("user"), m):
                continue
            ch = m.get("channel", {})
            when = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)
            text = sc.render_text(m.get("text", ""))
            who = sc.name(m.get("user")) if m.get("user") else m.get("username", "?")
            where = sc.label(ch)
            thread = (re.search(r"thread_ts=([\d.]+)", m.get("permalink") or "") or [None, None])[1]
            items.append(Item(
                title=_one_line(f"{where}  {text}", 200), meta=f"{who} · {ago(when)}", url=m.get("permalink"),
                when=when, icon="#", tone="secondary",
                ref={"kind": "slack", "user": who, "where": where, "text": m.get("text", ""), "ts": m["ts"],
                     "channel": ch.get("id"), "thread_ts": thread},
            ))
        text, ids = self._lines("s", "Slack", items, lambda i: (
            f"{_local(i.when)} · {i.ref['where']} · {i.ref['user']}: {_one_line(sc.render_text(i.ref['text']), 230)}"))
        total = found.get("total", len(items))
        return (text or "no matches"), f"⌕ Slack “{query}” · {total} hits", ids

    # Linear ---------------------------------------------------------------------------------

    async def t_search_linear(self, query: str, open_only: bool = False, mine: bool = False) -> tuple[str, str, list[str]]:
        items, total = await self.app.linear.search(query, open_only=bool(open_only), mine=bool(mine), first=12)
        text, ids = self._lines("l", "Linear", items, lambda i: f"{_one_line(i.title, 110)} · {i.meta}")
        flags = " · open" * bool(open_only) + " · mine" * bool(mine)
        return (text or "no matches"), f"⌕ Linear “{query}”{flags} · {len(items)} hits", ids

    # read ------------------------------------------------------------------------------------

    async def t_read(self, id: str) -> tuple[str, str, list[str]]:
        hid = id.strip().strip("[]").lower()
        hit = self.hits.get(hid)
        if hit is None:
            return f"no hit called {id}", f"✖ read {id}: unknown id", []
        item = hit.item
        ref = item.ref or {}
        if ref.get("kind") == "slack_convo":  # a dashboard Slack row: read the conversation's latest messages
            sc = self.app.slack
            msgs = await sc.history(ref["id"], limit=15)
            body = "\n".join(f"{_local(m.when)} {sc.name(m.user)}: {_one_line(sc.render_text(m.text), 300)}"
                             for m in msgs)
            return f"{item.title}\n{body}", f"⎘ read {hid} · {_one_line(item.title, 50)}", [hid]
        from .detail import load

        page = await load(item, self.app)
        parts = [page.title, " · ".join(c for c, _ in page.chips if c)]
        for s in page.sections:
            body = s.text if s.text is not None else (s.md or "")
            body = re.sub(r"\]\(pixeldeck-att:[^)]*\)", "]", body)  # attachment links mean nothing to the model
            parts.append(f"## {s.title}\n{body.strip()}")
        text = "\n".join(p for p in parts if p)
        if len(text) > RESULT_CHARS:
            text = text[:RESULT_CHARS] + "\n… (cut)"
        return text, f"⎘ read {hid} · {_one_line(item.title, 50)}", [hid]


# ── the conversation ──────────────────────────────────────────────────────────


Event = Callable[[str, str], None]  # (kind, data): think / tool / text / reset


class AskSession:
    """A conversation with the local model. Survives closing the chat until ctrl+n."""

    def __init__(self, app: PixelDeck) -> None:
        self.app = app
        self.tools = Toolbox(app)
        self.messages: list[dict] = []  # everything after the system prompt
        self.turns: list[Turn] = []
        self.ctx = 8192
        self.model = ""

    # model + context ---------------------------------------------------------------

    async def prepare(self) -> None:
        a = self.app.assist
        if not a.allowed():
            raise RuntimeError("the model server isn't local and assist.allow_remote is off")
        self.model = a.model or await a.pick_model()
        if not self.model:
            raise RuntimeError("LM Studio has no chat model; download one, then press l to load it")
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{a.api_root}/api/v1/models")
            for m in r.json().get("models", []):
                if m.get("key") == self.model and m.get("loaded_instances"):
                    self.ctx = int(m["loaded_instances"][0].get("config", {}).get("context_length") or self.ctx)
                    break
        except Exception:
            pass

    def system(self) -> str:
        now = datetime.now().astimezone()
        name = self.app.assist.cfg.get("name") or self.app.assist.identity.get("name") or "the user"
        return (
            f"You are the assistant inside PIXELDECK, {name}'s work dashboard. Now: {now:%A %d %B %Y, %H:%M %Z}.\n"
            f"Connected: {', '.join(self.tools.sources())}.\n"
            "Rules:\n"
            "- Questions about their work, people, tickets, PRs, mail, meetings or deploys: search with the tools "
            "first, never answer from memory. If a search misses, try other words or another source before giving up.\n"
            "- Cite each fact with its hit id in brackets, e.g. [s3] or [m1]. Only use ids the tools returned.\n"
            "- Use read on a hit when its snippet isn't enough.\n"
            "- You can only read. You can't send, reply, post, create, change or delete anything; if asked, say "
            "so and point to the hit they can open.\n"
            "- Answer short: a few lines or a tight list, Markdown, no tables unless asked. If nothing turned up, "
            "say what you searched."
        )

    def _budget_chars(self) -> int:
        return int((self.ctx - ANSWER_TOKENS - 150) * 3.0)

    def _fit(self, messages: list[dict], tools: list[dict]) -> list[dict]:
        """Trim older tool results (then older turns) so the request fits the model's context."""
        def size() -> int:
            return len(json.dumps(messages)) + len(json.dumps(tools)) + len(self.system())

        budget = self._budget_chars()
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=0)
        for i, m in enumerate(messages):  # 1) older turns' tool results
            if size() <= budget:
                return messages
            if i < last_user and m["role"] == "tool" and len(m["content"]) > 80:
                m["content"] = "(older result, trimmed to save room)"
        while size() > budget and last_user > 0:  # 2) drop the oldest turn
            nxt = next((i for i, m in enumerate(messages) if m["role"] == "user" and i > 0), None)
            if nxt is None or nxt >= last_user:
                break
            del messages[:nxt]
            last_user -= nxt
        while size() > budget:  # 3) halve this turn's biggest result
            tools_now = [m for m in messages[last_user:] if m["role"] == "tool" and len(m["content"]) > 400]
            if not tools_now:
                break
            big = max(tools_now, key=lambda m: len(m["content"]))
            big["content"] = big["content"][: len(big["content"]) // 2] + "\n… (cut to fit)"
        return messages

    # streaming one model call -----------------------------------------------------------

    async def _complete(self, messages: list[dict], tools: list[dict] | None, emit: Event) -> tuple[str, list[dict]]:
        a = self.app.assist
        body = {"model": self.model, "messages": [{"role": "system", "content": self.system()}, *messages],
                "temperature": 0.2, "max_tokens": ANSWER_TOKENS, "stream": True, "reasoning_effort": "low"}
        if tools:
            body["tools"] = tools
        content, calls = "", {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as c:
            async with c.stream("POST", f"{a.url}/chat/completions", json=body) as r:
                if r.status_code >= 400:
                    raw = (await r.aread()).decode(errors="replace")
                    try:
                        raw = json.loads(raw).get("error", {}).get("message", raw) if raw.startswith("{") else raw
                    except ValueError:
                        pass
                    raise RuntimeError(f"LM Studio {r.status_code}: {str(raw)[:200]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(str(chunk["error"].get("message", chunk["error"]))[:200])
                    for ch in chunk.get("choices", []):
                        d = ch.get("delta") or {}
                        if d.get("reasoning_content") or d.get("reasoning"):
                            emit("think", d.get("reasoning_content") or d.get("reasoning"))
                        if d.get("content"):
                            content += d["content"]
                            emit("text", d["content"])
                        for tc in d.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
        return content, [calls[k] for k in sorted(calls)]

    # one question -------------------------------------------------------------------------

    async def ask(self, question: str, emit: Event) -> Turn:
        turn = Turn(question)
        self.turns.append(turn)
        t0 = time.perf_counter()
        qmsg = {"role": "user", "content": question}
        try:
            if not self.model:
                await self.prepare()
            self.messages.append(qmsg)
            tools = self.tools.specs()
            for rnd in range(MAX_ROUNDS):
                last = rnd == MAX_ROUNDS - 1
                use = None if last else tools
                msgs = self._fit(self.messages, use or [])
                if last:  # out of tool rounds: answer with what's there
                    msgs = msgs + [{"role": "user", "content": "Answer now with what you found."}]
                content, calls = await self._complete(msgs, use, emit)
                content = re.sub(r"(?s)<think>.*?</think>", "", content).strip()
                if not calls:
                    turn.answer = content
                    self.messages.append({"role": "assistant", "content": content})
                    break
                emit("reset", "")  # text before tool calls is preamble, not the answer
                self.messages.append({"role": "assistant", "content": content or None, "tool_calls": [
                    {"id": c["id"] or f"call_{rnd}_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for i, c in enumerate(calls)]})
                for i, c in enumerate(calls):
                    try:
                        c["parsed"] = json.loads(c["args"] or "{}")
                    except ValueError:
                        c["parsed"] = {}
                    emit("tool", f"{c['name']} {json.dumps(c['parsed'], ensure_ascii=False)}")
                results = await asyncio.gather(*(self.tools.call(c["name"], c["parsed"]) for c in calls))
                for i, (c, (text, trace, ids)) in enumerate(zip(calls, results)):
                    turn.trace.append(trace)
                    turn.seen += [h for h in ids if h not in turn.seen]
                    emit("traced", trace)
                    self.messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{rnd}_{i}",
                                          "content": text})
        except (Exception, asyncio.CancelledError) as exc:
            stopped = isinstance(exc, asyncio.CancelledError)
            turn.error = "stopped" if stopped else (f"{exc}"[:300] or type(exc).__name__)
            # keep the history consistent: a question with no answer is dropped with its tool calls
            at = next((i for i, m in enumerate(self.messages) if m is qmsg), None)
            if at is not None:
                del self.messages[at:]
            if stopped:
                turn.secs = time.perf_counter() - t0
                raise
        turn.secs = time.perf_counter() - t0
        known = self.tools.hits
        turn.cited = [h for h in dict.fromkeys(m.lower() for m in re.findall(r"\[([a-z]\d+)\]", turn.answer, re.I))
                      if h in known]
        return turn

    def sources_for(self, turn: Turn) -> list[Hit]:
        """What the chat lists under an answer: what it cites, else everything its searches found."""
        ids = turn.cited or turn.seen
        return [self.tools.hits[h] for h in ids if h in self.tools.hits][:40]


def link_citations(answer: str, known: dict) -> str:
    """[s3] → a Markdown link the chat screen opens; unknown ids stay plain text."""
    def sub(m: re.Match) -> str:
        hid = m.group(1).lower()
        return f"[\\[{hid}\\]](pixeldeck-hit:{hid})" if hid in known else m.group(0)

    return re.sub(r"\[([a-zA-Z]\d+)\](?!\()", sub, answer)
