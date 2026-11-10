"""Reply suggestions from a local model (LM Studio's OpenAI-compatible server), in your own tone.

Your tone comes from things you've actually written: recent Slack messages and sent emails.
They're boiled down to a few style notes plus a handful of real examples, kept privately in
~/.config/pixeldeck/assist-style.json, and only ever sent to the configured local server.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import httpx

from .config import CONFIG_DIR, get_value

STYLE_PATH = CONFIG_DIR / "assist-style.json"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".local") or host.endswith(".localhost")


# ── tone ─────────────────────────────────────────────────────────────────────


@dataclass
class Style:
    notes: str = ""
    slack: list[str] = field(default_factory=list)
    email: list[str] = field(default_factory=list)
    built_at: float = 0.0

    @classmethod
    def load(cls) -> Style:
        try:
            return cls(**json.loads(STYLE_PATH.read_text()))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        STYLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STYLE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(STYLE_PATH)


def _strip_quoted(body: str) -> str:
    """Keep what you wrote, drop the thread you replied to and your signature."""
    cut = re.split(r"\n\s*(From: |-----Original|On .{5,80} wrote:|________________|Sent from my)", body)[0]
    lines = [l for l in cut.strip().splitlines() if not l.startswith(">")]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


async def gather_samples(app) -> tuple[list[str], list[str]]:
    """Your own recent Slack messages and sent-email openings (people only, no bots)."""
    slack: list[str] = []
    email: list[str] = []
    sc = getattr(app, "slack", None)
    if sc is not None:
        try:
            await sc.ensure_identity()
            await sc.ensure_users()
            d = await sc.call("search.messages", query=f"from:<@{sc.me}>", sort="timestamp", count=60)
            for m in d.get("messages", {}).get("matches", []):
                t = sc.render_text(m.get("text", "")).strip()
                if 3 <= len(t) <= 280 and not t.startswith(("http", "<")):
                    slack.append(t)
        except Exception:
            pass
    ol = getattr(app, "outlook", None)
    if ol is not None:
        try:
            d = await ol._get(
                "/me/mailFolders/sentitems/messages",
                {"$top": "25", "$select": "body,subject", "$orderby": "sentDateTime desc"},
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            for m in d.get("value", []):
                t = _strip_quoted((m.get("body") or {}).get("content", ""))
                if 10 <= len(t) <= 1200:
                    email.append(t[:600])
        except Exception:
            pass
    return slack[:40], email[:12]


# ── client ───────────────────────────────────────────────────────────────────


class Assist:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.url = (cfg.get("url") or "http://localhost:1234/v1").rstrip("/")
        self.model = cfg.get("model") or ""
        self.style = Style.load()
        self.identity: dict[str, str] = {}  # name / domain filled in from your accounts
        self.last_ms: float | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def allowed(self) -> bool:
        return is_local(self.url) or bool(self.cfg.get("allow_remote"))

    async def models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{self.url}/models")
        ids = [m["id"] for m in r.json().get("data", [])]
        return [i for i in ids if "embed" not in i and "parakeet" not in i and "whisper" not in i]

    async def pick_model(self) -> str:
        """With no model configured, prefer an instruct/chat model over whatever is listed first."""
        ids = await self.models()
        for hint in ("instruct", "chat", "qwen", "llama", "mistral", "gemma"):
            for i in ids:
                if hint in i.lower():
                    return i
        return ids[0] if ids else ""

    async def chat(self, system: str, user: str, max_tokens: int = 60, temperature: float = 0.4) -> str:
        if not self.allowed():
            raise RuntimeError("assist server isn't local; allow_remote is off")
        model = self.model or await self.pick_model()
        t = time.perf_counter()
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.url}/chat/completions", json={
                "model": model, "temperature": temperature, "max_tokens": max_tokens, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
        r.raise_for_status()
        self.last_ms = (time.perf_counter() - t) * 1000
        msg = r.json()["choices"][0]["message"]
        text = msg.get("content") or ""
        return re.sub(r"(?s)<think>.*?</think>", "", text).strip()  # reasoning models

    async def warm(self) -> None:
        """Load the model in LM Studio now so the first real suggestion isn't a 6–14s wait."""
        try:
            await self.chat("Reply with OK.", "ping", max_tokens=2)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)[:200]

    # LM Studio model management (its REST API: /api/v1/models, /load, /unload) ----------

    @property
    def api_root(self) -> str:
        """http://localhost:1234/v1 → http://localhost:1234"""
        return re.sub(r"/v\d+$", "", self.url)

    async def catalog(self) -> list[dict]:
        """Chat-capable models LM Studio has, with size and whether they're loaded."""
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{self.api_root}/api/v1/models")
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            if m.get("type") not in ("llm", "vlm"):
                continue
            if any(x in m["key"].lower() for x in ("embed", "parakeet", "whisper", "ltx")):
                continue  # speech / video / embedding models can't write replies
            out.append({
                "key": m["key"],
                "name": m.get("display_name") or m["key"],
                "params": m.get("params_string") or "",
                "quant": (m.get("quantization") or {}).get("name") if isinstance(m.get("quantization"), dict)
                else (m.get("quantization") or ""),
                "size_gb": (m.get("size_bytes") or 0) / 1e9,
                "instances": [i.get("id") for i in m.get("loaded_instances") or [] if i.get("id")],
                "ctx": min((int((i.get("config") or {}).get("context_length") or 0)
                            for i in m.get("loaded_instances") or []), default=0),
            })
        return out

    async def status(self) -> dict:
        """What the dashboard shows: is the server up, which model, is it loaded."""
        try:
            cat = await self.catalog()
        except Exception as exc:
            return {"up": False, "error": type(exc).__name__, "model": self.model}
        current = self.model or await self.pick_model()
        entry = next((m for m in cat if m["key"] == current), None)
        return {
            "up": True, "model": current, "entry": entry,
            "loaded": [m for m in cat if m["instances"]],
            "current_loaded": bool(entry and entry["instances"]),
        }

    async def load(self, key: str) -> float:
        """Load a model; returns seconds taken. Big models can take a while."""
        t = time.perf_counter()
        async with httpx.AsyncClient(timeout=600) as c:
            body = {"model": key}
            if ctx := int(self.cfg.get("context") or 0):
                body["context_length"] = ctx
            r = await c.post(f"{self.api_root}/api/v1/models/load", json=body)
        if r.status_code >= 400:
            raise RuntimeError(r.json().get("error", {}).get("message", f"HTTP {r.status_code}"))
        return time.perf_counter() - t

    async def unload(self, key: str | None = None) -> int:
        """Unload one model's instances (or every loaded model). Returns how many were unloaded."""
        n = 0
        async with httpx.AsyncClient(timeout=60) as c:
            for m in await self.catalog():
                if key and m["key"] != key:
                    continue
                for inst in m["instances"]:
                    r = await c.post(f"{self.api_root}/api/v1/models/unload", json={"instance_id": inst})
                    n += r.status_code < 400
        return n

    # tone ---------------------------------------------------------------------

    async def learn(self, app) -> Style:
        slack, email = await gather_samples(app)
        if not slack and not email:
            raise RuntimeError("found nothing you wrote (Slack / Microsoft 365 not connected?)")
        sample = "\n".join(f"- {s}" for s in slack[:30])
        sample += "\n\nEMAILS:\n" + "\n---\n".join(email[:6])
        notes = await self.chat(
            "You analyse writing style. Write 6 bullet points, each under 20 words, describing how this "
            "person writes: tone, length, capitalisation, punctuation, greetings/sign-offs, typical phrases. "
            "Plain text, no markdown, no preamble.",
            f"SLACK MESSAGES:\n{sample}", max_tokens=420, temperature=0.2,
        )
        self.style = Style(notes=notes, slack=slack, email=email, built_at=time.time())
        self.style.save()
        return self.style

    def _voice(self, kind: str) -> str:
        s = self.style
        name = self.cfg.get("name") or self.identity.get("name") or "the user"
        notes = s.notes or "Casual, friendly, short. Plain words, no corporate filler."
        examples = (s.slack[:6] if kind == "slack" else s.email[:2]) or s.slack[:5]
        ex = "\n".join(f"• {e}" for e in examples)
        extra = self.cfg.get("tone") or "keep it casual"
        return (f"You write as {name}, replying in their own voice. {extra}.\n"
                f"How they write:\n{notes}\n\nStyle samples (copy the STYLE only):\n{ex}\n\n"
                "Rules: reply to the latest message in the conversation. Stay on its topic. Default to a helpful, "
                "cooperative answer unless the draft signals otherwise. Never copy names, facts, "
                "typos or phrases from the style samples. Don't invent commitments, times or details the draft "
                "doesn't imply. Use slang like 'lol' sparingly, at most once and only if it fits. Never sound like "
                "an assistant. No quotes around the text.")

    # suggestions ----------------------------------------------------------------

    async def suggest(self, kind: str, context: str, draft: str) -> str | None:
        """The text to append after `draft` (or a whole reply if draft is empty)."""
        if not self.enabled:
            return None
        if kind == "slack":
            task = ("Continue the Slack reply draft. Output ONLY the words that come after the draft, "
                    "on one line, short like their messages. If the draft is empty, write the whole reply.")
            max_tokens = 36
        else:
            task = ("Continue the email reply draft. Output ONLY the text that comes after the draft "
                    "(a sentence or two). If the draft is empty, write a short complete reply body without a "
                    "subject line or signature.")
            home = (self.cfg.get("domain") or self.identity.get("domain") or "").lower()
            if home and home not in context.lower().split("reply goes to:")[-1]:
                task += (f" The recipient is outside {home}: stay casual and friendly but no slang, no 'lol', "
                         "no emoji.")
            max_tokens = 110
        user = f"{context}\n\nTheir draft so far: «{draft}»\n\n{task}"
        try:
            out = await self.chat(self._voice(kind), user, max_tokens=max_tokens, temperature=0.35)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)[:200]
            return None
        return clean(out, draft, single_line=kind == "slack")


def clean(out: str, draft: str, single_line: bool) -> str | None:
    s = out.strip().strip("«»\"“”").strip()
    if single_line:
        s = s.splitlines()[0].strip() if s else ""
    # models sometimes repeat the draft before continuing it
    d = draft.strip()
    if d and s.lower().startswith(d.lower()):
        s = s[len(d):]
    elif d:
        tail = d.split()[-1] if d.split() else ""
        if tail and s.lower().startswith(tail.lower() + " ") and len(tail) > 2:
            s = s[len(tail):]
    if not s.strip():
        return None
    if draft and not draft[-1].isspace() and s[0].isalnum():
        s = " " + s.lstrip()
    elif draft and draft[-1].isspace():
        s = s.lstrip().lstrip(",;:")  # "yeah " + ", quick one" would read "yeah , quick one"
        s = s.lstrip()
        if not s:
            return None
    return s

