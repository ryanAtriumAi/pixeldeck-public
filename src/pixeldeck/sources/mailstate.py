"""Which emails are new (arrived since PIXELDECK last looked) and which you've acknowledged.

Kept in ~/.config/pixeldeck/mail-state.json so a restart doesn't re-flash old mail or
forget what you've already opened. This is local only: nothing is marked read on the server.
"""

from __future__ import annotations

import json
import os
import time

from ..config import CONFIG_DIR
from .base import Item

STATE_PATH = CONFIG_DIR / "mail-state.json"
KEEP_SECONDS = 45 * 86400
SLACK_SECONDS = 15 * 60  # mail can land a little "in the past" (clock skew, slow delivery)


def mail_key(item: Item) -> str | None:
    ref = item.ref or {}
    kind = ref.get("kind")
    if kind == "graph_mail":
        return f"graph:{ref['id']}"
    if kind == "imap" and ref.get("uid"):
        return f"imap:{ref['acct']}:{ref['uid']}"
    if kind == "apple_mail":
        return f"apple:{ref['mid']}"
    return None


class MailState:
    def __init__(self) -> None:
        self.known: dict[str, float] = {}
        self.fresh: dict[str, float] = {}
        self.ack: dict[str, float] = {}
        self.last_sync: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            return
        self.known = data.get("known", {})
        self.fresh = data.get("fresh", {})
        self.ack = data.get("ack", {})
        self.last_sync = data.get("last_sync")

    def save(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        for d in (self.known, self.fresh, self.ack):
            for k in [k for k, ts in d.items() if ts < cutoff]:
                del d[k]
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"known": self.known, "fresh": self.fresh, "ack": self.ack,
                                   "last_sync": self.last_sync}))
        os.chmod(tmp, 0o600)
        tmp.replace(STATE_PATH)

    def apply(self, items: list[Item]) -> list[Item]:
        """Mark items new / acknowledged in place. Returns the ones that just arrived."""
        now = time.time()
        first_ever = self.last_sync is None
        since = (self.last_sync or now) - SLACK_SECONDS
        arrived: list[Item] = []
        for item in items:
            key = mail_key(item)
            if not key:
                continue
            if key not in self.known:
                self.known[key] = now
                when = item.when.timestamp() if item.when else now
                # Only mail that landed after we last looked counts as new; older mail that
                # merely scrolled into view (a filter change, a deleted message) doesn't.
                if not first_ever and item.unread and when >= since:
                    self.fresh[key] = now
                    arrived.append(item)
            if key in self.ack or not item.unread:
                self.fresh.pop(key, None)
                _mute(item)
            elif key in self.fresh:
                item.flash = True
        self.last_sync = now
        self.save()
        return arrived

    def acknowledge(self, item: Item) -> bool:
        key = mail_key(item)
        if not key:
            return False
        changed = key not in self.ack or key in self.fresh
        self.ack[key] = time.time()
        self.fresh.pop(key, None)
        self.save()
        _mute(item)
        return changed


def _mute(item: Item) -> None:
    item.flash = False
    item.unread = False
    item.tone = "muted"
    item.icon = "·"

