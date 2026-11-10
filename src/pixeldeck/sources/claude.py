"""Claude Code usage (from local session logs) and plan limits (from the status line).

Limits: Claude Code hands every status-line script a `rate_limits` object (5-hour and
7-day windows: `used_percentage`, `resets_at`). PIXELDECK can wrap the existing status-line
command: the wrapper saves `rate_limits` to a file, then pipes the same payload to the
original command untouched (Orca's hook keeps working). No credentials are read.

Usage: every assistant message in ~/.claude/projects/**/*.jsonl carries token counts.
Files are read incrementally (only new bytes) so this stays cheap.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import CONFIG_DIR

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
SETTINGS = CLAUDE_DIR / "settings.json"
PROJECTS = CLAUDE_DIR / "projects"
LIMITS_FILE = CONFIG_DIR / "claude-limits.json"
WRAPPER = CONFIG_DIR / "claude-statusline.sh"
ORIGINAL = CONFIG_DIR / "claude-statusline.original.json"


# ── status-line wrapper ──────────────────────────────────────────────────────


def _settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def wrapper_installed() -> bool:
    cmd = ((_settings().get("statusLine") or {}).get("command")) or ""
    return str(WRAPPER) in cmd


def install_wrapper() -> str:
    """Point Claude Code's status line at our wrapper, keeping the old command behind it."""
    settings = _settings()
    current = settings.get("statusLine")
    if current and str(WRAPPER) not in (current.get("command") or ""):
        ORIGINAL.parent.mkdir(parents=True, exist_ok=True)
        ORIGINAL.write_text(json.dumps(current, indent=2))
    jq = shutil.which("jq")
    save = (
        f"printf '%s' \"$payload\" | '{jq}' -c '{{rate_limits, model: .model.display_name, at: now}}' > \"$tmp\""
        if jq
        else f"printf '%s' \"$payload\" > \"$tmp\""
    )
    WRAPPER.write_text(
        f"""#!/bin/sh
# PIXELDECK: keep a copy of Claude Code's rate_limits, then run the original status line.
payload=$(cat)
case "$payload" in
  *'"rate_limits"'*)
    tmp="{LIMITS_FILE}.$$"
    umask 077
    {save} && mv "$tmp" "{LIMITS_FILE}" || rm -f "$tmp"
    ;;
esac
orig="{ORIGINAL}"
if [ -f "$orig" ]; then
  cmd=$('{shutil.which("python3") or "/usr/bin/python3"}' -c 'import json,sys; print(json.load(open(sys.argv[1])).get("command",""))' "$orig" 2>/dev/null)
  if [ -n "$cmd" ]; then
    printf '%s' "$payload" | /bin/sh -c "$cmd"
  fi
fi
"""
    )
    WRAPPER.chmod(0o755)
    backup = SETTINGS.with_name("settings.json.pixeldeck-bak")
    if SETTINGS.exists() and not backup.exists():
        shutil.copy2(SETTINGS, backup)
    keep = {k: v for k, v in (current or {}).items() if k not in ("type", "command")}
    settings["statusLine"] = {"type": "command", "command": f'/bin/sh "{WRAPPER}"', **keep}
    tmp = SETTINGS.with_suffix(".json.pixeldeck-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(SETTINGS)
    return "status line wrapped; limits appear after Claude Code's next status update"


def uninstall_wrapper() -> str:
    settings = _settings()
    if ORIGINAL.exists():
        settings["statusLine"] = json.loads(ORIGINAL.read_text())
    else:
        settings.pop("statusLine", None)
    tmp = SETTINGS.with_suffix(".json.pixeldeck-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(SETTINGS)
    return "original status line restored"


# ── limits ───────────────────────────────────────────────────────────────────


@dataclass
class Window:
    name: str
    pct: float
    resets: datetime | None


@dataclass
class Limits:
    windows: list[Window]
    spend: str | None
    model: str | None
    age: float  # seconds since Claude Code last wrote it


def _reset_time(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


LABELS = {"five_hour": "5H", "seven_day": "7D", "seven_day_opus": "7D OPUS", "seven_day_sonnet": "7D SONNET"}


def read_limits() -> Limits | None:
    try:
        data = json.loads(LIMITS_FILE.read_text())
        age = time.time() - LIMITS_FILE.stat().st_mtime
    except (OSError, ValueError):
        return None
    rl = data.get("rate_limits") or {}
    windows = []
    for key, val in rl.items():
        if not isinstance(val, dict) or "used_percentage" not in val:
            continue
        windows.append(Window(LABELS.get(key, key.replace("_", " ").upper()), float(val["used_percentage"] or 0),
                              _reset_time(val.get("resets_at"))))
    order = list(LABELS.values())
    windows.sort(key=lambda w: order.index(w.name) if w.name in order else 99)
    spend = None
    sl = rl.get("spend_limit")
    if isinstance(sl, dict) and sl.get("limit_usd"):
        spend = f"${sl.get('used_usd', 0):,.2f} / ${sl['limit_usd']:,.2f} {sl.get('period', '')}".strip()
    model = data.get("model")
    if isinstance(model, dict):
        model = model.get("display_name")
    return Limits(windows, spend, model, age)


# ── usage from session logs ──────────────────────────────────────────────────


@dataclass
class Usage:
    five_hour: int = 0
    today: int = 0
    week: int = 0
    output_week: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    sessions_today: int = 0
    msgs_5h: int = 0


class UsageScanner:
    def __init__(self) -> None:
        self._offsets: dict[Path, int] = {}
        self._events: dict[str, tuple[float, str, int, int, str]] = {}  # msg id -> (ts, model, total, out, session)

    def _ingest(self, path: Path) -> None:
        size = path.stat().st_size
        start = self._offsets.get(path, 0)
        if size < start:  # file was rewritten
            start = 0
        if size == start:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
        # only consume complete lines; a half-written last line is read next time
        end = chunk.rfind(b"\n") + 1
        self._offsets[path] = start + end
        for raw in chunk[:end].splitlines():
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            msg = d.get("message") or {}
            u = msg.get("usage")
            if d.get("type") != "assistant" or not isinstance(u, dict):
                continue
            ts = d.get("timestamp")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except (AttributeError, ValueError):
                continue
            total = sum(int(u.get(k) or 0) for k in
                        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            key = msg.get("id") or d.get("requestId") or f"{path}:{ts}"
            self._events[key] = (when, msg.get("model") or "?", total, int(u.get("output_tokens") or 0),
                                 d.get("sessionId") or path.stem)

    def scan(self) -> Usage:
        cutoff = time.time() - 8 * 86400
        if PROJECTS.exists():
            for path in PROJECTS.rglob("*.jsonl"):
                try:
                    if path.stat().st_mtime >= cutoff:
                        self._ingest(path)
                except OSError:
                    continue
        now = time.time()
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        u = Usage()
        sessions: set[str] = set()
        for key, (ts, model, total, out, sess) in list(self._events.items()):
            if ts < now - 7 * 86400:
                del self._events[key]
                continue
            u.week += total
            u.output_week += out
            if model.startswith("<"):  # <synthetic> placeholder messages
                continue
            name = model.replace("claude-", "").split("-2")[0]
            u.by_model[name] = u.by_model.get(name, 0) + total
            if ts >= now - 5 * 3600:
                u.five_hour += total
                u.msgs_5h += 1
            if ts >= midnight:
                u.today += total
                sessions.add(sess)
        u.sessions_today = len(sessions)
        return u


def fmt_tokens(n: int) -> str:
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


def until(dt: datetime | None) -> str:
    if not dt:
        return ""
    s = int((dt - datetime.now(timezone.utc)).total_seconds())
    if s <= 0:
        return "now"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d{h}h" if d else (f"{h}h{m:02d}m" if h else f"{m}m")
