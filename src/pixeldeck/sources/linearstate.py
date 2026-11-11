"""Which Linear tickets you've opened since they were assigned to you.

A ticket assigned to you recently carries the NEW tag until you open it (detail page or browser).
Being assigned it again later brings the tag back. Kept in ~/.config/pixeldeck/linear-state.json;
nothing changes in Linear.
"""

from __future__ import annotations

import json
import os
import time

from ..config import CONFIG_DIR

STATE_PATH = CONFIG_DIR / "linear-state.json"
KEEP_SECONDS = 90 * 86400


class LinearState:
    def __init__(self) -> None:
        self.ack: dict[str, float] = {}  # ticket id -> when you opened it
        try:
            self.ack = json.loads(STATE_PATH.read_text()).get("ack", {})
        except (OSError, ValueError):
            pass

    def acknowledge(self, ticket: str) -> None:
        self.ack[ticket] = time.time()
        cutoff = time.time() - KEEP_SECONDS
        self.ack = {k: ts for k, ts in self.ack.items() if ts >= cutoff}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ack": self.ack}))
        os.chmod(tmp, 0o600)
        tmp.replace(STATE_PATH)
