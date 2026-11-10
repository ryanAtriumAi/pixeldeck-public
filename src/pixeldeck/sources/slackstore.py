"""Local SQLite copy of Slack state, so restarts and reloads start warm.

Holds people, conversations with their unread state, and the last ~100 messages of each
conversation / thread you've opened. It's a cache: Slack stays the source of truth and
everything here gets refreshed in the background. Private to your account (mode 600).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from ..config import CONFIG_DIR

DB_PATH = CONFIG_DIR / "slack.db"
KEEP_MESSAGES = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta     (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users    (id TEXT PRIMARY KEY, name TEXT, is_bot INTEGER);
CREATE TABLE IF NOT EXISTS convos   (id TEXT PRIMARY KEY, name TEXT, kind TEXT, user TEXT, last_read TEXT,
                                     latest_ts TEXT, latest_text TEXT, latest_user TEXT, unread INTEGER,
                                     refreshed REAL);
CREATE TABLE IF NOT EXISTS messages (convo TEXT, thread TEXT, ts TEXT, data TEXT, PRIMARY KEY (convo, thread, ts));
"""


class SlackStore:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # meta -----------------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, **values) -> None:
        self.db.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            [(k, str(v)) for k, v in values.items() if v is not None])
        self.db.commit()

    # people -----------------------------------------------------------------------

    def load_users(self) -> tuple[dict[str, str], set[str]]:
        users, bots = {}, set()
        for uid, name, is_bot in self.db.execute("SELECT id, name, is_bot FROM users"):
            users[uid] = name
            if is_bot:
                bots.add(uid)
        return users, bots

    def save_users(self, users: dict[str, str], bots: set[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM users")
            self.db.executemany("INSERT INTO users (id, name, is_bot) VALUES (?, ?, ?)",
                                [(u, n, int(u in bots)) for u, n in users.items()])
        self.put(users_at=time.time())

    # conversations ----------------------------------------------------------------

    def load_convos(self) -> list[dict]:
        cols = ["id", "name", "kind", "user", "last_read", "latest_ts", "latest_text", "latest_user", "unread",
                "refreshed"]
        return [dict(zip(cols, row)) for row in self.db.execute(f"SELECT {', '.join(cols)} FROM convos")]

    def save_convos(self, convos: list, replace: bool = False) -> None:
        rows = [(c.id, c.name, c.kind, c.user, c.last_read, c.latest_ts, c.latest_text, c.latest_user, c.unread,
                 c.refreshed) for c in convos]
        with self.db:
            if replace:  # a full listing: conversations you've left disappear
                ids = [r[0] for r in rows]
                self.db.execute(f"DELETE FROM convos WHERE id NOT IN ({', '.join('?' * len(ids))})", ids)
            self.db.executemany(
                "INSERT OR REPLACE INTO convos (id, name, kind, user, last_read, latest_ts, latest_text, "
                "latest_user, unread, refreshed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    # messages ---------------------------------------------------------------------

    def load_messages(self, convo: str, thread: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT data FROM messages WHERE convo = ? AND thread = ? ORDER BY CAST(ts AS REAL)",
                               (convo, thread or ""))
        return [json.loads(r[0]) for r in rows]

    def save_messages(self, convo: str, thread: str | None, messages: list) -> None:
        keep = messages[-KEEP_MESSAGES:]
        with self.db:
            self.db.execute("DELETE FROM messages WHERE convo = ? AND thread = ?", (convo, thread or ""))
            self.db.executemany("INSERT OR REPLACE INTO messages (convo, thread, ts, data) VALUES (?, ?, ?, ?)",
                                [(convo, thread or "", m.ts, json.dumps(asdict(m))) for m in keep])

    def wipe(self) -> None:
        with self.db:
            for t in ("meta", "users", "convos", "messages"):
                self.db.execute(f"DELETE FROM {t}")
