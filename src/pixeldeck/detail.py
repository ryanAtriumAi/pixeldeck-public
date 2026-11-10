"""Detail pages: Enter on an item opens it inside PIXELDECK; `o` expands to the browser."""

from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import html
import imaplib
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import attachments
from . import pixel as px
from .config import get_secret
from .sources.base import Item, ago, parse_time, run_cmd, run_json

if TYPE_CHECKING:
    from .app import PixelDeck

MAX_BODY = 30_000  # plain-text sections
MAX_MD = 8_000  # Markdown sections render much slower, keep them tighter


@dataclass
class Section:
    title: str
    md: str | None = None  # rendered as Markdown
    text: str | None = None  # rendered verbatim (email bodies, logs)


@dataclass
class Page:
    kind: str
    title: str
    chips: list[tuple[str, str]] = field(default_factory=list)  # (text, tone)
    sections: list[Section] = field(default_factory=list)
    url: str | None = None
    reply: dict | None = None  # set on email pages: how to answer it
    files: list[dict] = field(default_factory=list)  # email attachments that can be opened (attachments.py)


def _quote(body: str, limit: int = 1500) -> str:
    body = (body or "").strip()
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n\n… _(trimmed, open in browser for the rest)_"
    return "\n".join("> " + line for line in body.splitlines()) or "> _(empty)_"


def clip_md(text: str, n: int, where: str = "Linear") -> str:
    """Trim Markdown at a paragraph break and close a code block left open by the cut."""
    if len(text) <= n:
        return text
    cut = text.rfind("\n\n", 0, n)
    cut = cut if cut > n // 2 else n
    head = text[:cut].rstrip()
    if head.count("```") % 2:
        head += "\n```"
    return head + f"\n\n… _{len(text) - cut:,} more characters: press **o** to read it in {where}_"


def _airy_lists(text: str) -> str:
    """A list whose items wrap over several lines reads better with a blank line between items."""
    lines = text.split("\n")
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if block and max(len(b) for b in block) > 120:
            for i, b in enumerate(block):
                out.append(b)
                if i < len(block) - 1 and re.match(r"\s*([-*+]|\d+\.) ", block[i + 1]):
                    out.append("")
        else:
            out.extend(block)
        block.clear()

    for line in lines:
        if re.match(r"\s*([-*+]|\d+\.) ", line) or (block and line.startswith("  ") and line.strip()):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


_CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)
_LINK = re.compile(r"(!?\[[^\]\n]*\]\([^)\s]+\)|<(?:https?|mailto):[^>\s]+>|https?://[^\s)>\]]+)")


def linear_markdown(text: str, workspace: str | None, gh_repo: str | None = None) -> str:
    """Linear's Markdown, tuned for Textual's renderer. Code is never touched:
    placeholders like <tenant> are escaped (the renderer drops them as HTML), - [ ] / - [x] become
    ☐ / ☑, images become links, and ticket references like ABC-123 link to Linear (and #123 to
    GitHub when gh_repo is given)."""
    out = []
    for i, part in enumerate(_CODE.split(text or "")):
        if i % 2:  # code span or fence
            out.append(part)
            continue
        pieces = _LINK.split(part)
        for j, p in enumerate(pieces):
            if j % 2:  # an existing link / autolink / bare url
                m = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", p)
                out.append(f"[🖼 {m.group(1) or 'image'}]({m.group(2)})" if m else p)
                continue
            p = re.sub(r"(?m)^\*\*([^*\n]{2,60}?):?\*\*:?[ \t]*$", r"#### \1", p)  # **Files:** alone → heading
            p = _airy_lists(p)
            p = re.sub(r"(?m)^(\s*)[-*] \[ \] ", r"\1- ☐ ", p)
            p = re.sub(r"(?m)^(\s*)[-*] \[[xX]\] ", r"\1- ☑ ", p)
            p = re.sub(r"<(/?[A-Za-z][^<>\n]{0,60})>", r"&lt;\1&gt;", p)
            if workspace:
                p = re.sub(r"(?<![\w/\-\[])([A-Z][A-Z0-9]{1,6}-\d+)\b",
                           lambda m: f"[{m.group(1)}]({workspace}/issue/{m.group(1)})", p)
            if gh_repo:
                p = re.sub(r"(?<![\w/&#\[])#(\d{1,6})\b",
                           lambda m: f"[#{m.group(1)}](https://github.com/{gh_repo}/pull/{m.group(1)})", p)
            out.append(p)
    return "".join(out)


def _clip(text: str, n: int = MAX_BODY) -> str:
    return text if len(text) <= n else text[:n] + f"\n\n… ({len(text) - n:,} more characters, open in browser)"


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", "", raw)
    raw = html.unescape(raw)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", raw)).strip()


# ── GitHub ───────────────────────────────────────────────────────────────────

PR_FIELDS = (
    "title,body,author,state,isDraft,number,additions,deletions,changedFiles,files,reviews,"
    "comments,statusCheckRollup,reviewDecision,mergeable,headRefName,baseRefName,labels,createdAt,url"
)


def _check_state(c: dict) -> tuple[str, str]:
    raw = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
    if raw in ("SUCCESS", "NEUTRAL", "SKIPPED"):
        return "✔", "ok"
    if raw in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
        return "✖", "bad"
    return "◷", "wait"


async def pr_page(ref: dict, app: PixelDeck) -> Page:
    d = await run_json("gh", "pr", "view", ref["url"], "--json", PR_FIELDS)
    repo = ref["url"].split("github.com/")[1].split("/pull/")[0]
    state = "DRAFT" if d["isDraft"] and d["state"] == "OPEN" else d["state"]
    checks = d.get("statusCheckRollup") or []
    marks = [(_check_state(c), c.get("name") or c.get("context") or "?", c.get("detailsUrl") or c.get("targetUrl")) for c in checks]
    bad = [m for m in marks if m[0][1] == "bad"]
    wait = [m for m in marks if m[0][1] == "wait"]
    ok = [m for m in marks if m[0][1] == "ok"]
    chips = [
        (state, {"OPEN": "success", "MERGED": "primary", "CLOSED": "error"}.get(state, "muted")),
        (f"{repo.split('/')[-1]} #{d['number']}", "secondary"),
        (f"@{d['author']['login']}", "accent"),
        (f"{d['baseRefName']} ← {d['headRefName']}", "muted"),
        (f"+{d['additions']} −{d['deletions']} · {d['changedFiles']} files", "muted"),
    ]
    if d.get("reviewDecision"):
        chips.append((d["reviewDecision"].replace("_", " "), "success" if d["reviewDecision"] == "APPROVED" else "warning"))
    if checks:
        chips.append((f"✔{len(ok)} ✖{len(bad)} ◷{len(wait)}", "error" if bad else ("warning" if wait else "success")))
    if d.get("mergeable") == "CONFLICTING":
        chips.append(("CONFLICT", "error"))

    sections = [Section("DESCRIPTION", md=_clip(d.get("body") or "_No description._", MAX_MD))]
    if checks:
        lines = [f"- {icon} [{name}]({url})" if url else f"- {icon} {name}" for (icon, _), name, url in bad + wait]
        lines.append(f"- ✔ {len(ok)} passing" if ok else "")
        sections.append(Section(f"CHECKS · {len(checks)}", md="\n".join(l for l in lines if l)))
    latest: dict[str, dict] = {}
    for r in d.get("reviews") or []:
        latest[r["author"]["login"]] = r
    if latest:
        sections.append(
            Section(
                "REVIEWS",
                md="\n\n".join(
                    f"**@{who}** · {r['state'].replace('_', ' ').lower()} · {ago(parse_time(r.get('submittedAt')))}"
                    + (f"\n\n{_quote(r['body'])}" if r.get("body") else "")
                    for who, r in latest.items()
                ),
            )
        )
    comments = (d.get("comments") or [])[-10:]
    if comments:
        sections.append(
            Section(
                f"COMMENTS · {len(d.get('comments') or [])}",
                md="\n\n".join(
                    f"**@{c['author']['login']}** · {ago(parse_time(c['createdAt']))}\n\n{_quote(c['body'])}" for c in comments
                ),
            )
        )
    files = sorted(d.get("files") or [], key=lambda f: -(f["additions"] + f["deletions"]))
    if files:
        sections.append(
            Section(
                f"FILES · {d['changedFiles']}",
                md="\n".join(f"- `{f['path']}` +{f['additions']} −{f['deletions']}" for f in files[:30])
                + (f"\n- … {len(files) - 30} more" if len(files) > 30 else ""),
            )
        )
    return Page("PULL REQUEST", d["title"], chips, sections, d["url"])


async def issue_page(ref: dict, app: PixelDeck) -> Page:
    d = await run_json(
        "gh", "issue", "view", ref["url"], "--json", "title,body,author,state,labels,comments,assignees,createdAt,url,number"
    )
    chips = [
        (d["state"], "success" if d["state"] == "OPEN" else "muted"),
        (f"#{d['number']}", "secondary"),
        (f"@{d['author']['login']}", "accent"),
        *[(f"⚑ {l['name']}", "primary") for l in d.get("labels", [])[:4]],
    ]
    sections = [Section("DESCRIPTION", md=_clip(d.get("body") or "_No description._", MAX_MD))]
    if d.get("comments"):
        sections.append(
            Section(
                f"COMMENTS · {len(d['comments'])}",
                md="\n\n".join(
                    f"**@{c['author']['login']}** · {ago(parse_time(c['createdAt']))}\n\n{_quote(c['body'])}"
                    for c in d["comments"][-10:]
                ),
            )
        )
    return Page("ISSUE", d["title"], chips, sections, d["url"])


async def notification_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    return Page(
        ref.get("type", "NOTIFICATION").upper(),
        item.title,
        [(ref.get("reason", "").upper(), "accent"), (ref.get("repo", ""), "secondary")],
        [Section("ABOUT", md=f"GitHub has no inline view for a **{ref.get('type')}** notification. "
                              "Press **o** to open it in the browser.")],
        item.url,
    )


def _dur(a: str | None, b: str | None) -> str:
    ta, tb = parse_time(a), parse_time(b)
    if not ta or not tb:
        return ""
    s = int((tb - ta).total_seconds())
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


async def run_page(ref: dict, app: PixelDeck) -> Page:
    rid, repo = str(ref["id"]), ref["repo"]
    d = await run_json(
        "gh", "run", "view", rid, "--repo", repo, "--json",
        "name,displayTitle,status,conclusion,headBranch,headSha,event,createdAt,updatedAt,url,jobs,workflowName,number",
    )
    concl = d.get("conclusion") or d.get("status")
    tone = {"success": "success", "failure": "error", "cancelled": "muted"}.get(concl, "warning")
    chips = [
        (concl.upper(), tone),
        (f"{repo.split('/')[-1]}@{d['headBranch']}", "secondary"),
        (d["headSha"][:9], "muted"),
        (d["event"], "muted"),
        (f"#{d['number']} · {ago(parse_time(d['createdAt']))}", "muted"),
    ]
    jobs = []
    for j in d.get("jobs", []):
        icon, _ = _check_state(j)
        jobs.append(f"- {icon} **{j['name']}** {_dur(j.get('startedAt'), j.get('completedAt'))}")
        for st in j.get("steps", []):
            sicon, kind = _check_state(st)
            if kind != "ok":
                jobs.append(f"    - {sicon} {st['name']}")
    sections = [Section(f"JOBS · {len(d.get('jobs', []))}", md="\n".join(jobs) or "_No jobs yet._")]
    if concl == "failure":
        try:
            log = await run_cmd("gh", "run", "view", rid, "--repo", repo, "--log-failed", timeout=60)
            lines = []
            for line in log.splitlines()[-80:]:
                parts = line.split("\t", 2)
                msg = parts[-1]
                msg = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?", "", msg)
                lines.append(msg)
            sections.append(Section("FAILED LOG · LAST 80 LINES", text="\n".join(lines)))
        except Exception as exc:
            sections.append(Section("FAILED LOG", text=f"couldn't fetch the log: {exc}"))
    return Page("CI RUN", f"{d['workflowName']}: {d['displayTitle']}", chips, sections, d["url"])


async def commit_page(ref: dict, app: PixelDeck) -> Page:
    d = await run_json("gh", "api", f"repos/{ref['repo']}/commits/{ref['sha']}")
    c = d["commit"]
    chips = [
        (d["sha"][:9], "secondary"),
        (c["author"]["name"], "accent"),
        (ago(parse_time(c["committer"]["date"])), "muted"),
        (f"+{d['stats']['additions']} −{d['stats']['deletions']}", "muted"),
    ]
    subject, _, rest = c["message"].partition("\n")
    sections = []
    if rest.strip():
        sections.append(Section("MESSAGE", text=rest.strip()))
    for f in d.get("files", [])[:10]:
        patch = f.get("patch") or ""
        sections.append(
            Section(f"{f['filename']} +{f['additions']} −{f['deletions']}", md=f"```diff\n{_clip(patch, 6000)}\n```")
        )
    return Page("COMMIT", subject, chips, sections, d["html_url"])


async def compare_page(ref: dict, app: PixelDeck) -> Page:
    d = await run_json("gh", "api", f"repos/{ref['repo']}/compare/{ref['base']}...{ref['head']}")
    rows, machinery = [], 0
    for c in reversed(d.get("commits", [])):
        subject = c["commit"]["message"].splitlines()[0]
        if re.match(r"chore\((prod|staging)\)", subject):
            machinery += 1
            continue
        subject = re.sub(
            r"\(#(\d+)\)", lambda m: f"([#{m.group(1)}](https://github.com/{ref['repo']}/pull/{m.group(1)}))", subject
        )
        who = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
        rows.append(f"- `{c['sha'][:7]}` {subject} · @{who} · {ago(parse_time(c['commit']['committer']['date']))}")
    chips = [
        (f"{len(rows)} TO SHIP", "accent" if rows else "success"),
        (f"{ref['env'].lower()} @ {ref['base'][:9]}", "secondary"),
        (f"{machinery} release commits skipped", "muted"),
    ]
    body = "\n".join(rows) or f"★ {ref['env'].lower()} is current with {ref['head']}."
    return Page("NOT YET IN " + ref["env"], f"{ref['head']} vs {ref['env'].lower()}", chips,
                [Section("COMMITS, NEWEST FIRST", md=body)], d.get("html_url"))


def _linear_workspace(app: PixelDeck) -> str | None:
    """https://linear.app/<workspace>, read off any Linear item the dashboard already has."""
    try:
        feed = app.query_one("#linear").feed
    except Exception:
        return None
    for it in (feed.items if feed else []):
        if it.url and "/issue/" in it.url:
            return it.url.split("/issue/")[0]
    return None


async def release_page(ref: dict, app: PixelDeck) -> Page:
    d = await run_json("gh", "api", f"repos/{ref['repo']}/releases/tags/{ref['tag']}")
    where = ref.get("where") or {}
    live = bool(ref.get("live"))
    when = parse_time(d.get("published_at") or d.get("created_at"))
    chips = [
        (ref["tag"], "green" if live else "accent"),
        ("pre-release" if d.get("prerelease") else "release", "secondary"),
        (f"released {ago(when)}" if when else "", "muted"),
        (f"by @{(d.get('author') or {}).get('login', '?')}", "muted"),
    ]
    chips += [(f"{name.lower()} {'✔' if ok else '✖'}", "green" if ok else "muted") for name, ok in where.items()]
    body = linear_markdown(d.get("body") or "_No release notes._", _linear_workspace(app), gh_repo=ref["repo"])
    sections = [Section("RELEASE NOTES", md=clip_md(body, 20_000, "GitHub"))]
    assets = d.get("assets") or []
    if assets:
        sections.append(Section("ASSETS", md="\n".join(
            f"- [{a['name']}]({a['browser_download_url']}) · {a.get('size', 0) / 1024:.0f} KB" for a in assets)))
    return Page("RELEASE", d.get("name") or ref["tag"], [c for c in chips if c[0]], sections, d.get("html_url"))


async def pins_page(ref: dict, app: PixelDeck) -> Page:
    rows = ["| deployed | image | from | commit |", "|---|---|---|---|"]
    for p in ref["pins"][:30]:
        when = p["when"].astimezone().strftime("%a %d %b %H:%M") if p["when"] else "?"
        rows.append(f"| {when} ({ago(p['when'])}) | `{p['image']}` | {p.get('tag') or '—'} | [{p['full_sha'][:7]}]({p['url']}) |")
    return Page("DEPLOY HISTORY", f"{ref['env']} pins", [(f"{len(ref['pins'])} pins", "secondary")],
                [Section("NEWEST FIRST", md="\n".join(rows))], None)


async def uptime_page(ref: dict, app: PixelDeck) -> Page:
    hist = list(app.releases.health.get(ref["env"], []))
    sections = []
    if ref.get("health"):
        try:
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "pixeldeck/0.1"}) as c:
                t = time.perf_counter()
                r = await c.get(ref["health"])
                ms = (time.perf_counter() - t) * 1000
            sections.append(Section(f"LIVE · HTTP {r.status_code} · {ms:.0f}ms", text=r.text[:2000]))
        except httpx.HTTPError as exc:
            sections.append(Section("LIVE", text=f"unreachable: {exc}"))
    if hist:
        lat = [ms for ok, ms in hist if ok] or [0]
        rows = [f"pings this session: {len(hist)}   up: {sum(ok for ok, _ in hist)}   "
                f"min {min(lat):.0f}ms   max {max(lat):.0f}ms", ""]
        top = max(lat) or 1
        for ok, ms in hist[-40:]:
            rows.append(f"{'✔' if ok else '✖'} {ms:6.0f}ms {'█' * max(1, round(ms / top * 40))}")
        sections.append(Section("HISTORY (OLDEST FIRST)", text="\n".join(rows)))
    return Page("UPTIME", ref["env"], [(ref.get("health") or "", "muted")], sections, None)


# ── Mail / calendar ──────────────────────────────────────────────────────────

TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}


def _attachments_section(files: list[dict]) -> tuple[list[dict], Section | None]:
    """Every attachment becomes a link that opens just that file; tiny inline images (logos) are hidden."""
    links, plain, logos = [], [], 0
    for f in files:
        if f.get("inline") and attachments.is_image(f["name"], f.get("ctype")) and f.get("size", 0) < attachments.MIN_INLINE:
            logos += 1
        elif f.get("fetchable", True):
            links.append(f)
        elif not f.get("inline"):
            plain.append(f)
    rows = []
    for f in links:
        _, how = attachments.KINDS[attachments.kind_of(f["name"], f.get("ctype"))]
        where = " · pasted in the message" if f.get("inline") else ""
        rows.append(f"- {attachments.md_link(f)} · {f.get('size', 0) / 1024:.0f} KB{where} · _{how}_")
    for f in plain:
        rows.append(f"- {f['name']} · {f.get('size', 0) / 1024:.0f} KB · _{f.get('why') or 'open the email to get it'}_")
    if logos:
        rows.append(f"- _{logos} small inline image{'s' * (logos > 1)} hidden (signature logos, icons)_")
    if not rows:
        return links, None
    title = "ATTACHMENTS · click one, or press a, to open just that file" if links else "ATTACHMENTS"
    return links, Section(title, md="\n".join(rows))


def _files_chip(files: list[dict]) -> tuple[str, str]:
    n = len(files)
    icon = "🖼" if all(attachments.is_image(f["name"], f.get("ctype")) for f in files) else "📎"
    return (f"{icon} {n} attachment{'s' * (n > 1)} · a", "success")


def _addr(r: dict) -> str:
    e = r.get("emailAddress", {})
    return e.get("name") or e.get("address", "?")


async def graph_mail_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    ol = app.outlook
    d = await ol._get(
        f"/me/messages/{ref['id']}",
        {"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments,importance,webLink"},
        headers=TEXT_BODY,
    )
    when = parse_time(d["receivedDateTime"])
    to = [_addr(r) for r in d.get("toRecipients", [])]
    chips = [
        (f"from {_addr(d.get('from') or {})}", "accent"),
        (f"to {', '.join(to[:3])}{' +' + str(len(to) - 3) if len(to) > 3 else ''}", "secondary"),
        (f"{when.astimezone():%a %d %b %H:%M} · {ago(when)}" if when else "", "muted"),
    ]
    if d.get("ccRecipients"):
        chips.append((f"cc {len(d['ccRecipients'])}", "muted"))
    if d.get("importance") == "high":
        chips.append(("⚑ HIGH", "error"))
    sections = [Section("MESSAGE", text=_clip((d.get("body") or {}).get("content", "").strip() or "(empty)"))]
    # Always ask: Outlook says hasAttachments=false when the only images are pasted into the body.
    try:
        att = (await ol._get(f"/me/messages/{ref['id']}/attachments",
                             {"$select": "id,name,size,contentType,isInline"})).get("value", [])
    except Exception:
        att = []
    files = [{"via": "graph", "msg": ref["id"], "att": a.get("id"), "name": a.get("name") or "attachment",
              "ctype": a.get("contentType"), "size": a.get("size") or 0, "inline": bool(a.get("isInline")),
              "subject": d.get("subject") or "",
              "fetchable": "fileAttachment" in (a.get("@odata.type") or ""),
              "why": "attached email or event, open the email to get it" if "itemAttachment" in (a.get("@odata.type") or "")
              else "cloud file link, open the email to get it"} for a in att]
    links, sec = _attachments_section(files)
    if sec:
        sections.append(sec)
    addr = lambda r: (r.get("emailAddress") or {}).get("address", "")
    reply = {
        "via": "graph", "id": ref["id"], "subject": d.get("subject") or "",
        "from": addr(d.get("from") or {}), "to": [addr(r) for r in d.get("toRecipients", [])],
        "cc": [addr(r) for r in d.get("ccRecipients", [])], "web": d.get("webLink"),
        "quote": (d.get("body") or {}).get("content", "")[:3000],
        "me": next(iter(a.get("username", "") for a in ol._msal().get_accounts()), ""),
    }
    if links:
        chips.append(_files_chip(links))
    return Page("EMAIL", d.get("subject") or "(no subject)", chips, sections, d.get("webLink") or item.url, reply,
                links)


async def graph_event_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    d = await app.outlook._get(
        f"/me/events/{ref['id']}",
        {"$select": "subject,body,start,end,location,attendees,organizer,onlineMeeting,webLink,isOnlineMeeting"},
        headers=TEXT_BODY,
    )
    start, end = parse_time(d["start"]["dateTime"]), parse_time(d["end"]["dateTime"])
    chips = [
        (f"{start.astimezone():%a %d %b %H:%M}–{end.astimezone():%H:%M}" if start and end else "?", "accent"),
        (ago(start), "secondary"),
        ((d.get("location") or {}).get("displayName") or ("online" if d.get("isOnlineMeeting") else ""), "muted"),
        (f"by {_addr(d.get('organizer') or {})}", "muted"),
    ]
    resp = {"accepted": "✔", "declined": "✖", "tentativelyAccepted": "?", "none": "·", "notResponded": "·"}
    att = [
        f"- {resp.get((a.get('status') or {}).get('response', 'none'), '·')} {_addr(a)}"
        f"{' (optional)' if a.get('type') == 'optional' else ''}"
        for a in d.get("attendees", [])
    ]
    join = ref.get("join") or (d.get("onlineMeeting") or {}).get("joinUrl")
    sections = []
    if join:
        sections.append(Section("JOIN", md=f"[{join[:80]}…]({join})"))
    if att:
        sections.append(Section(f"ATTENDEES · {len(att)}", md="\n".join(att)))
    body = (d.get("body") or {}).get("content", "").strip()
    if body:
        sections.append(Section("DETAILS", text=_clip(body, 8000)))
    return Page("MEETING", d.get("subject") or "(busy)", chips, sections, join or d.get("webLink"))


def _imap_body(acct: dict, password: str, uid: str) -> tuple[email.message.EmailMessage, str]:
    conn = imaplib.IMAP4_SSL(acct["host"], int(acct.get("port", 993)), timeout=20)
    try:
        conn.login(acct["user"], password)
        conn.select("INBOX", readonly=True)
        _, rows = conn.uid("fetch", uid, "(BODY.PEEK[])")
        raw = next(r[1] for r in rows if isinstance(r, tuple))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    text = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        text = _html_to_text(text)
    return msg, text


def _imap_files(msg: email.message.EmailMessage, uid: str) -> list[dict]:
    """Attachments and pasted-in images of a downloaded message, with their bytes."""
    out = []
    for i, part in enumerate(msg.walk()):
        if part.is_multipart() or part.get_content_maintype() == "text" and not part.get_filename():
            continue
        name = part.get_filename() or f"image-{i}.{part.get_content_subtype()}"
        disp = (part.get_content_disposition() or "").lower()
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            data = b""
        out.append({"via": "imap", "msg": uid, "att": str(i), "name": name, "ctype": part.get_content_type(),
                    "size": len(data), "inline": disp != "attachment",
                    "subject": str(msg.get("Subject") or ""),
                    "data": data})
    return out


async def imap_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    acct = next(a for a in app.cfg.get("imap", []) if a.get("name", a["user"]) == ref["acct"])
    password = get_secret(acct.get("secret", f"imap-{ref['acct']}"))
    msg, text = await asyncio.to_thread(_imap_body, acct, password or "", ref["uid"])
    chips = [(f"from {msg.get('From', '?')}", "accent"), (f"to {msg.get('To', '?')[:60]}", "secondary"),
             (ago(item.when), "muted")]
    reply = {"via": "mailto", "subject": str(msg.get("Subject") or ""), "from": email.utils.parseaddr(str(msg.get("From", "")))[1],
             "to": [], "cc": [], "quote": text[:3000]}
    sections = [Section("MESSAGE", text=_clip(text))]
    links, sec = _attachments_section(_imap_files(msg, ref["uid"]))
    if sec:
        sections.append(sec)
    if links:
        chips.append(_files_chip(links))
    return Page("EMAIL", str(msg.get("Subject") or "(no subject)"), chips, sections, item.url, reply, links)


async def apple_mail_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    mid = ref["mid"].replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Mail"
  set m to first message of inbox whose message id is "{mid}"
  set r to ""
  repeat with x in to recipients of m
    set r to r & (address of x) & ", "
  end repeat
  return r & linefeed & (content of m)
end tell'''
    out = await run_cmd("osascript", "-e", script, timeout=60)
    to, _, body = out.partition("\n")
    chips = [(item.meta.split(" · ")[0], "accent"), (f"to {to.strip(', ')[:60]}", "secondary"), (ago(item.when), "muted")]
    reply = {"via": "apple", "mid": ref["mid"], "subject": item.title, "from": item.sender, "to": [], "cc": [],
             "quote": body.strip()[:3000]}
    return Page("EMAIL", item.title, chips, [Section("MESSAGE", text=_clip(body.strip()))], item.url, reply)


# ── Slack / Linear ───────────────────────────────────────────────────────────


def _slack_text(t: str) -> str:
    t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", t)
    t = re.sub(r"<#\w+\|([^>]+)>", r"#\1", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    return html.unescape(t)


async def slack_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    when = datetime.fromtimestamp(float(ref["ts"]), tz=timezone.utc)
    chips = [(f"@{ref['user']}", "accent"), (ref["where"], "secondary"), (f"{when.astimezone():%a %H:%M} · {ago(when)}", "muted")]
    sections = [Section("MESSAGE", text=_slack_text(ref["text"]))]
    token = get_secret("slack", env="SLACK_TOKEN")
    root = ref.get("thread_ts") or ref["ts"]
    if token and ref.get("channel"):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://slack.com/api/conversations.replies",
                params={"channel": ref["channel"], "ts": root, "limit": 50},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = r.json()
        if data.get("ok") and len(data.get("messages", [])) > 1:
            lines = []
            for m in data["messages"][-20:]:
                t = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)
                mark = "▶ " if m["ts"] == ref["ts"] else ""
                lines.append(f"{mark}{m.get('user', '?')} · {ago(t)}\n{_slack_text(m.get('text', ''))}\n")
            sections.append(Section(f"THREAD · {len(data['messages'])}", text="\n".join(lines)))
        elif not data.get("ok") and data.get("error") == "missing_scope":
            sections.append(Section("THREAD", md="_Add the `channels:history`, `groups:history`, `im:history` "
                                                "and `mpim:history` user scopes to see threads here._"))
    return Page("SLACK", item.title[:120], chips, sections, item.url)


LINEAR_ISSUE = """
query($id: String!) {
  issue(id: $id) {
    identifier title description url priorityLabel dueDate createdAt
    state { name type } assignee { name } creator { name } project { name } cycle { number }
    labels { nodes { name } }
    comments(first: 50) { nodes { body createdAt user { name } } }
  }
}
"""


async def linear_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    key = get_secret("linear", env="LINEAR_API_KEY")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://api.linear.app/graphql",
            json={"query": LINEAR_ISSUE, "variables": {"id": ref["id"]}},
            headers={"Authorization": key or "", "Content-Type": "application/json"},
        )
    d = r.json()["data"]["issue"]
    chips = [
        (d["state"]["name"].upper(), {"started": "accent", "completed": "success"}.get(d["state"]["type"], "secondary")),
        (d["identifier"], "secondary"),
        (d.get("priorityLabel") or "", "warning" if d.get("priorityLabel") in ("Urgent", "High") else "muted"),
        (f"→ {d['assignee']['name']}" if d.get("assignee") else "unassigned", "muted"),
    ]
    if d.get("project"):
        chips.append((d["project"]["name"], "primary"))
    if d.get("dueDate"):
        chips.append((f"due {d['dueDate']}", "warning"))
    chips += [(f"⚑ {l['name']}", "muted") for l in d["labels"]["nodes"][:3]]
    ws = d["url"].split("/issue/")[0] if "/issue/" in d.get("url", "") else None  # https://linear.app/<workspace>
    sections = [Section("DESCRIPTION", md=clip_md(linear_markdown(d.get("description") or "_No description._", ws),
                                                    20_000))]
    comments = sorted(d["comments"]["nodes"], key=lambda c: c["createdAt"])
    if len(comments) > 12:
        sections.append(Section(f"COMMENTS · {len(comments)}",
                                md=f"_{len(comments) - 12} older comments: press **o** to read them in Linear_"))
    for c in comments[-12:]:  # each comment is its own block, so its headings / lists / code render properly
        who = (c.get("user") or {}).get("name", "?")
        sections.append(Section(f"💬 {who} · {ago(parse_time(c['createdAt']))}",
                                md=clip_md(linear_markdown(c["body"], ws), 6_000)))
    return Page("LINEAR ISSUE", d["title"], [c for c in chips if c[0]], sections, d["url"])


LOADERS = {
    "pr": pr_page,
    "issue": issue_page,
    "run": run_page,
    "commit": commit_page,
    "compare": compare_page,
    "pins": pins_page,
    "uptime": uptime_page,
    "release": release_page,
}
async def agent_update_page(ref: dict, app: PixelDeck, item: Item) -> Page:
    return Page("BRIEF", item.title.removeprefix("BRIEF · "), [(item.meta, "muted")], [Section("FROM YOUR AGENT", md=ref.get("body", ""))], None)


ITEM_LOADERS = {
    "agent_update": agent_update_page,
    "notification": notification_page,
    "graph_mail": graph_mail_page,
    "graph_event": graph_event_page,
    "imap": imap_page,
    "apple_mail": apple_mail_page,
    "slack": slack_page,
    "linear": linear_page,
}


async def load(item: Item, app: PixelDeck) -> Page:
    ref = item.ref or {}
    kind = ref.get("kind")
    if kind in LOADERS:
        return await LOADERS[kind](ref, app)
    if kind in ITEM_LOADERS:
        return await ITEM_LOADERS[kind](ref, app, item)
    return Page("ITEM", item.title, [(item.meta, "muted")], [], item.url)


# ── Screen ───────────────────────────────────────────────────────────────────

DETAIL_CSS = f"""
DetailScreen {{ background: {px.VOID}; }}
#d-head {{ height: auto; background: {px.DEEP}; padding: 1 2 0 2; }}
#d-rule {{ height: 1; }}
#d-body {{ background: {px.VOID}; padding: 0 2; scrollbar-color: {px.PURPLE}; scrollbar-size-vertical: 1; }}
#d-body .sec-title {{ margin-top: 1; color: {px.VOID}; background: {px.PURPLE}; text-style: bold; width: auto; padding: 0 1; }}
#d-body .sec-text {{ color: {px.INK}; padding: 1 1; background: {px.DEEP}; }}
#d-body Markdown {{ background: {px.DEEP}; padding: 1 2 0 2; margin: 0; max-width: 112; }}
#d-body .sec-text {{ max-width: 112; }}
#d-body MarkdownParagraph {{ margin: 0 0 1 0; }}
#d-body MarkdownH3, #d-body MarkdownH4 {{ margin: 1 0 0 0; }}
#d-body MarkdownBulletList, #d-body MarkdownOrderedList {{ margin: 0 0 1 0; }}
#d-body MarkdownH1, #d-body MarkdownH2, #d-body MarkdownH3 {{ color: {px.HOTPINK}; background: transparent; text-style: bold; }}
#d-body MarkdownFence {{ background: {px.PANEL}; }}
#d-body MarkdownBlockQuote {{ border-left: outer {px.PURPLE}; background: {px.PANEL}; }}
#d-body MarkdownBlock > .code_inline {{ color: #cbb8ff; background: #221142; }}
#d-body MarkdownBlock > .strong {{ color: {px.INK}; text-style: bold; }}
#d-body MarkdownBlock > .em {{ color: {px.INK}; text-style: italic; }}
#d-body MarkdownBlock > .s {{ color: {px.DIM}; text-style: strike; }}
#d-body MarkdownFence {{ background: #120628; border-left: outer {px.PURPLE}; margin: 1 0; }}
#d-body MarkdownTH {{ color: {px.CYAN}; text-style: bold; }}
#d-body MarkdownTable {{ margin: 1 0; }}
#d-body MarkdownHorizontalRule {{ color: {px.SHADOW}; }}
#d-body MarkdownBullet {{ color: {px.HOTPINK}; }}
#d-body MarkdownH1, #d-body MarkdownH2 {{ margin: 1 0 0 0; }}
#d-loading {{ padding: 2 2; }}
#d-main {{ height: 1fr; }}
#d-toc {{ width: 34; display: none; background: {px.DEEP}; border-left: tall {px.SHADOW}; border-title-color: {px.CYAN};
          border-title-style: bold; padding: 0 1; scrollbar-size-vertical: 1; }}
#d-toc.-on {{ display: block; }}
#d-toc > .option-list--option-highlighted {{ background: #3a1670; }}
#d-actions {{ height: 3; background: {px.DEEP}; padding: 0 1; }}
#d-actions Button {{ margin-right: 1; min-width: 12; height: 3; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
#d-actions Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
#d-actions .spacer {{ width: 1fr; }}
#d-pos {{ width: auto; content-align: right middle; height: 3; color: {px.DIM}; padding: 0 1; }}
"""


class DetailScreen(Screen):
    CSS = DETAIL_CSS
    BINDINGS = [
        Binding("escape,backspace,q", "back", "Back"),
        Binding("o", "open", "Open in browser"),
        Binding("c", "copy", "Copy link"),
        Binding("n,right", "step(1)", "Next"),
        Binding("p,left", "step(-1)", "Prev"),
        Binding("j,down", "scroll(1)", show=False),
        Binding("k,up", "scroll(-1)", show=False),
        Binding("space,pagedown", "page(1)", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("right_square_bracket", "heading(1)", "Next heading"),
        Binding("left_square_bracket", "heading(-1)", "Prev heading"),
        Binding("r", "reply(False)", "Reply"),
        Binding("R", "reply(True)", "Reply all"),
        Binding("a,i", "attachments", "Attachments"),
    ]

    def __init__(self, items: list[Item], index: int, source: str, draft: str | None = None) -> None:
        super().__init__()
        self.items, self.index, self.source = items, index, source
        self.page: Page | None = None
        self.draft = draft  # an agent's proposed reply: open the composer with it once the email loads

    @property
    def item(self) -> Item:
        return self.items[self.index]

    def compose(self) -> ComposeResult:
        from .app import Divider

        yield Static(id="d-head")
        yield Divider(id="d-rule")
        with Horizontal(id="d-main"):
            yield VerticalScroll(id="d-body")
            yield OptionList(id="d-toc")
        with Horizontal(id="d-actions"):
            yield Button("◀ BACK", id="back")
            yield Button("◀ PREV", id="prev")
            yield Button("NEXT ▶", id="next")
            yield Static(classes="spacer")
            yield Button("REPLY", id="reply")
            yield Button("REPLY ALL", id="reply-all")
            yield Button("📎 ATTACHMENTS a", id="files")
            yield Static(id="d-pos")
            yield Button("COPY LINK", id="copy")
            yield Button("OPEN IN BROWSER ↗", id="open", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    # rendering ----------------------------------------------------------------

    def _head(self, kind: str, title: str, chips: list[tuple[str, str]]) -> Text:
        out = Text()
        out.append(f" {kind} ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        out.append(f"  {self.source}", Style(color=px.DIM))
        out.append("\n")
        out.append(title, Style(color=px.INK, bold=True))
        out.append("\n")
        for text, tone in chips:
            if not text:
                continue
            color = px.TONE.get(tone, px.INK)
            out.append(f" {text} ", Style(color=px.VOID if tone != "muted" else px.INK, bgcolor=color if tone != "muted" else px.SHADOW, bold=tone != "muted"))
            out.append(" ")
        return out

    def _sync_nav(self) -> None:
        self.query_one("#prev", Button).disabled = self.index == 0
        self.query_one("#next", Button).disabled = self.index >= len(self.items) - 1
        self.query_one("#d-pos", Static).update(f"{self.index + 1}/{len(self.items)}")
        self.query_one("#open", Button).disabled = not self._url()
        can_reply = bool(self.page and self.page.reply)
        self.query_one("#reply", Button).display = can_reply
        self.query_one("#reply-all", Button).display = can_reply
        n = len(self.page.files) if self.page else 0
        btn = self.query_one("#files", Button)
        btn.display = bool(n)
        btn.label = f"📎 ATTACHMENTS ({n}) a" if n > 1 else "📎 ATTACHMENT a"
        self.refresh_bindings()

    @work(exclusive=True)
    async def _load(self) -> None:
        item = self.item
        self.page = None
        viewed = getattr(self.app, "item_viewed", None)
        if viewed:
            viewed(item)  # opening it is acknowledging it
        self._sync_nav()
        self.query_one("#d-head", Static).update(self._head("LOADING", item.title, [(item.meta, "muted")]))
        body = self.query_one("#d-body", VerticalScroll)
        await body.remove_children()
        spinner = Static(id="d-loading")
        await body.mount(spinner)
        tick = 0

        def spin() -> None:
            nonlocal tick
            tick += 1
            bar = Text()
            for i in range(24):
                on = (i - tick) % 24 < 6
                bar.append("▰" if on else "▱", Style(color=px.gradient_at(i / 23) if on else px.SHADOW))
            bar.append("  LOADING", Style(color=px.DIM))
            spinner.update(bar)

        timer = self.set_interval(0.08, spin)
        try:
            page = await load(item, self.app)
        except Exception as exc:
            page = Page("ERROR", item.title, [(item.meta, "muted")],
                        [Section("COULDN'T LOAD THIS", text=f"{type(exc).__name__}: {exc}\n\nPress o to open it in the browser.")],
                        item.url)
        finally:
            timer.stop()
        if item is not self.item:  # user moved on while this was loading
            return
        self.page = page
        self.query_one("#d-head", Static).update(self._head(page.kind, page.title, page.chips))
        await body.remove_children()
        widgets = []
        for sec in page.sections:
            widgets.append(Static(f" {sec.title} ", classes="sec-title"))
            if sec.md is not None:
                widgets.append(Markdown(sec.md))
            else:
                widgets.append(Static(Text(sec.text or ""), classes="sec-text"))
        if not widgets:
            widgets.append(Static(Text("Nothing more to show here. Press o to open it in the browser.", px.DIM)))
        await body.mount_all(widgets)
        body.scroll_home(animate=False)
        body.focus()
        self._sync_nav()
        self.call_after_refresh(self._build_toc)
        if self.draft and page.reply:
            self.call_after_refresh(self.action_reply, False)

    # contents panel -------------------------------------------------------------

    def _headings(self) -> list[tuple[int, str, object]]:
        """(level, text, widget) for every heading on the page, section titles included, in order."""
        out = []
        body = self.query_one("#d-body", VerticalScroll)
        for w in body.query("Static.sec-title, MarkdownH1, MarkdownH2, MarkdownH3, MarkdownH4"):
            if "sec-title" in w.classes:
                out.append((1, str(w.render()).strip(), w))
            else:
                level = int(type(w).__name__[-1])
                content = getattr(w, "_content", None)
                text = (content.plain if hasattr(content, "plain") else str(content or "")).strip()
                out.append((level + 1, text, w))
        return out

    def _build_toc(self) -> None:
        toc = self.query_one("#d-toc", OptionList)
        heads = [h for h in self._headings() if h[1]]
        self._heads = heads
        long_page = sum(1 for lvl, _, _ in heads if lvl > 1) >= 4
        toc.set_class(long_page, "-on")
        if not long_page:
            return
        toc.border_title = " ON THIS PAGE "
        opts = []
        top = min((lvl for lvl, _, _ in heads if lvl > 1), default=2)  # indent relative to the biggest heading used
        width = 30
        for i, (lvl, text, _) in enumerate(heads):
            depth = 0 if lvl == 1 else min(1 + lvl - top, 2)
            t = Text(no_wrap=True)
            t.append("  " * depth + ("▸ " if lvl == 1 else "· "), Style(color=px.HOTPINK if lvl == 1 else px.DIM))
            t.append(text, Style(color=px.INK if depth <= 1 else px.DIM, bold=depth <= 1))
            t.truncate(width, overflow="ellipsis")  # one line per entry
            opts.append(Option(t, id=f"h{i}"))
        toc.set_options(opts)

    @on(OptionList.OptionSelected, "#d-toc")
    def _toc_jump(self, e: OptionList.OptionSelected) -> None:
        _, _, w = self._heads[int(e.option.id[1:])]
        self.query_one("#d-body", VerticalScroll).scroll_to_widget(w, top=True, animate=False)

    def action_heading(self, delta: int) -> None:
        """] / [ : jump to the next / previous heading."""
        body = self.query_one("#d-body", VerticalScroll)
        heads = getattr(self, "_heads", None) or self._headings()

        def y_in_page(w) -> int:
            # off-screen widgets have no screen region, so add up offsets inside the page instead
            y, node = 0, w
            while node is not None and node is not body:
                y += node.virtual_region.y
                node = node.parent
            return y

        here = body.scroll_y
        spots = sorted((y_in_page(w), i) for i, (_, _, w) in enumerate(heads))
        pick = (next((i for y, i in spots if y > here + 1), None) if delta > 0
                else next((i for y, i in reversed(spots) if y < here - 1), None))
        if pick is not None:
            body.scroll_to_widget(heads[pick][2], top=True, animate=False)

    # actions ------------------------------------------------------------------

    def _url(self) -> str | None:
        return (self.page.url if self.page and self.page.url else None) or self.item.url

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.dismiss(self.index)

    @on(Button.Pressed, "#open")
    def action_open(self) -> None:
        if url := self._url():
            subprocess.Popen(["open", url])

    @on(Button.Pressed, "#copy")
    def action_copy(self) -> None:
        if url := self._url():
            subprocess.run(["pbcopy"], input=url.encode())
            self.notify(url, title="COPIED", timeout=3)

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        self.action_step(1)

    @on(Button.Pressed, "#prev")
    def _prev(self) -> None:
        self.action_step(-1)

    def action_step(self, delta: int) -> None:
        new = self.index + delta
        if 0 <= new < len(self.items):
            self.index = new
            self._load()

    def action_scroll(self, delta: int) -> None:
        self.query_one("#d-body", VerticalScroll).scroll_relative(y=delta * 2, animate=False)

    def action_page(self, delta: int) -> None:
        body = self.query_one("#d-body", VerticalScroll)
        body.scroll_relative(y=delta * max(body.size.height - 2, 1), animate=False)

    def action_reload(self) -> None:
        self._load()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "reply":
            return bool(self.page and self.page.reply)  # hidden off email pages
        if action == "attachments":
            return bool(self.page and self.page.files)
        return True

    @on(Button.Pressed, "#files")
    def action_attachments(self) -> None:
        files = self.page.files if self.page else []
        if files:
            self.app.push_screen(AttachmentPicker(files))

    @on(Button.Pressed, "#reply")
    def _reply_btn(self) -> None:
        self.action_reply(False)

    @on(Button.Pressed, "#reply-all")
    def _reply_all_btn(self) -> None:
        self.action_reply(True)

    def action_reply(self, reply_all: bool) -> None:
        if not (self.page and self.page.reply):
            return
        from .compose import ComposeScreen

        draft, self.draft = self.draft, None
        def done(sent: bool | None) -> None:
            if sent:
                self.notify(f"to {self.page.reply.get('from', '')}", title="✉ REPLY SENT" if self.page.reply["via"] == "graph"
                            else "✉ REPLY OPENED", timeout=4)

        self.app.push_screen(ComposeScreen(self.page.reply, reply_all, draft), done)


class AttachmentPicker(ModalScreen[None]):
    """Pick an attachment: Enter opens it on its own, f shows it in Finder instead."""

    DEFAULT_CSS = f"""
    AttachmentPicker {{ align: center middle; background: {px.VOID} 70%; }}
    AttachmentPicker > Vertical {{ width: 96; max-width: 95%; height: auto; max-height: 80%; background: {px.DEEP};
                                   border: panel {px.HOTPINK}; border-title-color: {px.VOID}; border-title-style: bold;
                                   padding: 1 2; }}
    AttachmentPicker OptionList {{ height: auto; max-height: 20; background: {px.DEEP}; border: none; margin-top: 1; }}
    AttachmentPicker OptionList > .option-list--option-highlighted {{ background: #3a1670; }}
    AttachmentPicker OptionList:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
    """
    BINDINGS = [Binding("escape,q,a", "close", "Close"), Binding("o", "in_app", "Open in its app"),
                Binding("f", "finder", "Show in Finder")]

    def __init__(self, files: list[dict]) -> None:
        super().__init__()
        self.files = files

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = f" 📎 {len(self.files)} ATTACHMENT{'S' * (len(self.files) > 1)} "
            yield Static(Text("Enter opens one on its own · o opens it in its app (Word, Excel…) · f shows it in Finder · "
                              "esc closes", Style(color=px.DIM)))
            opts = []
            for i, f in enumerate(self.files):
                icon, how = attachments.KINDS[attachments.kind_of(f["name"], f.get("ctype"))]
                t = Text(no_wrap=True, overflow="ellipsis")
                t.append(f"{icon} ")
                name = f["name"] if len(f["name"]) <= 44 else f["name"][:43] + "…"
                t.append(f"{name:<44}", Style(color=px.INK, bold=True))
                t.append(f" {f.get('size', 0) / 1024:6.0f} KB  ", Style(color=px.DIM))
                t.append(how, Style(color=px.CYAN if "browser" in how else px.DIM))
                opts.append(Option(t, id=str(i)))
            yield OptionList(*opts)

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def _pick(self, e: OptionList.OptionSelected) -> None:
        self.app.open_url(attachments.register(self.files[int(e.option.id)]))

    def action_finder(self) -> None:
        i = self.query_one(OptionList).highlighted
        if i is not None:
            self.app.open_attachment(attachments.register(self.files[i]), reveal=True)

    def action_in_app(self) -> None:
        """Open in Word / Excel / Preview… (editable). Things that could run code still only go to Finder."""
        i = self.query_one(OptionList).highlighted
        if i is not None:
            self.app.open_attachment(attachments.register(self.files[i]), in_app=True)

    def action_close(self) -> None:
        self.dismiss(None)
