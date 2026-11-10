"""Shared types for data sources."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Item:
    title: str
    meta: str = ""
    url: str | None = None
    icon: str = "▸"
    tone: str = "primary"  # primary | secondary | accent | success | warning | error | muted
    unread: bool = False
    when: datetime | None = None
    sender: str = ""  # address, for filtering and de-duplication
    ref: dict | None = None  # what the detail page should load ({"kind": ..., ...})
    flash: bool = False  # new and not yet looked at: the tile pulses it
    marks: list[tuple[str, str]] = field(default_factory=list)  # (text in the title, tone) to color
    pulse: bool = False  # the marks breathe green ↔ pink (a deploy in progress)
    rule_after: bool = False  # draw a thin line under this item (groups inside a tile)


@dataclass
class Feed:
    """What a source hands to a panel."""

    items: list[Item] = field(default_factory=list)
    count: int | None = None  # headline number for the panel title / HUD
    note: str | None = None  # setup hint or error, shown instead of / above items
    error: bool = False
    arrived: list[Item] = field(default_factory=list)  # items that showed up since the last sync
    active: int | None = None  # secondary headline (e.g. Linear issues in progress) for the HUD
    extra: dict = field(default_factory=dict)  # source-specific extras (e.g. Linear filter facets)


class SourceError(Exception):
    pass


class NotConfigured(Exception):
    """Raised with a one-line setup hint when a source has no credentials."""


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    # Graph returns 7 fractional digits; fromisoformat accepts at most 6.
    if "." in v:
        head, _, tail = v.partition(".")
        frac = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(frac):]
        v = f"{head}.{frac[:6]}{rest}"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        secs = -secs
        prefix = "in "
        suffix = ""
    else:
        prefix, suffix = "", " ago"
    if secs < 60:
        return "now"
    for size, unit in ((86400 * 7, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{prefix}{int(secs // size)}{unit}{suffix}"
    return "now"


async def run_cmd(*args: str, timeout: float = 30) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise SourceError(f"{args[0]} timed out")
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip().splitlines()
        raise SourceError(msg[-1] if msg else f"{args[0]} exited {proc.returncode}")
    return out.decode(errors="replace")


async def run_json(*args: str, timeout: float = 30):
    return json.loads(await run_cmd(*args, timeout=timeout) or "null")
