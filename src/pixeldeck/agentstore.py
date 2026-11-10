"""What the agent (Hermes, through `pixeldeck mcp`) leaves for you, and what it reads of the dashboard.

Everything lives in ~/.config/pixeldeck (private files):

- agent.db — proposals (an action the agent suggests; nothing runs until you accept it in the
  AGENT tile, and accepting opens the usual confirm screen), tasks (your list, with due times), and
  updates (briefs the agent writes).
- dashboard.json — a snapshot of every tile, written by the running app, so the agent can see what
  the dashboard shows even though it runs in another process.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .sources.base import Item, parse_time

DB_PATH = CONFIG_DIR / "agent.db"
SNAPSHOT_PATH = CONFIG_DIR / "dashboard.json"

# what a proposal can ask you to do; each one opens a screen you confirm on
KINDS = {
    "reply_mail": "draft a reply to an email",
    "slack_message": "draft a Slack message (in a conversation or a thread)",
    "create_event": "put something on your calendar",
    "add_task": "add a task to your list",
    "open": "look at something (a PR to review, a ticket, a thread)",
}

SCHEMA = """
create table if not exists proposals (
  id integer primary key, created real not null, kind text not null, title text not null,
  why text not null default '', payload text not null default '{}', item text,
  status text not null default 'open', snooze_until real, dedupe text unique, decided real
);
create table if not exists tasks (
  id integer primary key, created real not null, title text not null, notes text not null default '',
  due real, status text not null default 'open', item text, done_at real, source text not null default 'you'
);
create table if not exists updates (
  id integer primary key, created real not null, title text not null, body text not null, seen integer not null default 0
);
"""


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def item_to_json(item: Item | None) -> str | None:
    if item is None:
        return None
    d = asdict(item)
    d["when"] = item.when.isoformat() if item.when else None
    for k in ("flash", "marks", "pulse", "rule_after"):
        d.pop(k, None)
    return json.dumps(d, default=str)


def item_from_json(raw: str | None) -> Item | None:
    if not raw:
        return None
    d = json.loads(raw)
    d["when"] = parse_time(d.get("when"))
    fields = Item.__dataclass_fields__
    return Item(**{k: v for k, v in d.items() if k in fields})


def parse_when(value: str | None) -> float | None:
    """'2026-11-07', '2026-11-07T15:00', or '2026-11-07 15:00' → epoch seconds (local time)."""
    if not value:
        return None
    v = value.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        if "T" not in v:
            dt = dt.replace(hour=17)  # a date alone means end of that working day
        dt = dt.astimezone()
    return dt.timestamp()


def fmt_when(ts: float | None) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts).astimezone()
    today = datetime.now().astimezone().date()
    days = (dt.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days, f"{dt:%a %b %d}")
    return f"{day} {dt:%H:%M}" if (dt.hour, dt.minute) != (17, 0) else day


class AgentStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        if fresh:
            _private(path)

    # proposals ------------------------------------------------------------------

    def propose(self, kind: str, title: str, why: str = "", payload: dict | None = None,
                item: Item | None = None, dedupe: str | None = None) -> tuple[int, bool]:
        """Returns (id, created). An open or snoozed proposal with the same dedupe key isn't repeated,
        and one you dismissed stays dismissed."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if dedupe:
            row = self.db.execute("select id from proposals where dedupe = ?", (dedupe,)).fetchone()
            if row:
                return row["id"], False
        cur = self.db.execute(
            "insert into proposals (created, kind, title, why, payload, item, dedupe) values (?,?,?,?,?,?,?)",
            (time.time(), kind, title.strip()[:200], why.strip()[:600], json.dumps(payload or {}),
             item_to_json(item), dedupe))
        return cur.lastrowid, True

    def proposals(self, include_snoozed: bool = False) -> list[sqlite3.Row]:
        now = time.time()
        rows = self.db.execute(
            "select * from proposals where status in ('open','snoozed') order by created desc").fetchall()
        return [r for r in rows if include_snoozed or r["status"] == "open" or (r["snooze_until"] or 0) <= now]

    def decide(self, pid: int, status: str, snooze_secs: float | None = None) -> None:
        until = time.time() + snooze_secs if snooze_secs else None
        self.db.execute("update proposals set status = ?, snooze_until = ?, decided = ? where id = ?",
                        (status, until, time.time(), pid))

    def recent_decisions(self, limit: int = 15) -> list[sqlite3.Row]:
        return self.db.execute("select * from proposals where status in ('accepted','dismissed') "
                               "order by decided desc limit ?", (limit,)).fetchall()

    # tasks -----------------------------------------------------------------------

    def add_task(self, title: str, notes: str = "", due: float | None = None, item: Item | None = None,
                 source: str = "you") -> int:
        cur = self.db.execute("insert into tasks (created, title, notes, due, item, source) values (?,?,?,?,?,?)",
                              (time.time(), title.strip()[:200], notes.strip()[:2000], due, item_to_json(item), source))
        return cur.lastrowid

    def tasks(self, include_done: bool = False) -> list[sqlite3.Row]:
        q = "select * from tasks" + ("" if include_done else " where status = 'open'")
        rows = self.db.execute(q + " order by due is null, due, created").fetchall()
        return list(rows)

    def task(self, tid: int) -> sqlite3.Row | None:
        return self.db.execute("select * from tasks where id = ?", (tid,)).fetchone()

    def complete_task(self, tid: int, done: bool = True) -> bool:
        cur = self.db.execute("update tasks set status = ?, done_at = ? where id = ?",
                              ("done" if done else "open", time.time() if done else None, tid))
        return cur.rowcount > 0

    def update_task(self, tid: int, title: str | None = None, notes: str | None = None,
                    due: float | None | bool = False) -> bool:
        sets, args = [], []
        if title:
            sets.append("title = ?"), args.append(title.strip()[:200])
        if notes is not None:
            sets.append("notes = ?"), args.append(notes.strip()[:2000])
        if due is not False:
            sets.append("due = ?"), args.append(due)
        if not sets:
            return False
        cur = self.db.execute(f"update tasks set {', '.join(sets)} where id = ?", (*args, tid))
        return cur.rowcount > 0

    # updates (briefs) --------------------------------------------------------------

    def post_update(self, title: str, body: str) -> int:
        cur = self.db.execute("insert into updates (created, title, body) values (?,?,?)",
                              (time.time(), title.strip()[:200], body.strip()[:20000]))
        self.db.execute("delete from updates where id not in (select id from updates order by created desc limit 30)")
        return cur.lastrowid

    def updates(self, limit: int = 3) -> list[sqlite3.Row]:
        return self.db.execute("select * from updates order by created desc limit ?", (limit,)).fetchall()

    def mark_update_seen(self, uid: int) -> None:
        self.db.execute("update updates set seen = 1 where id = ?", (uid,))


# ── dashboard snapshot ────────────────────────────────────────────────────────


def write_snapshot(tiles: list[dict]) -> None:
    """tiles: [{key, label, note, count, items: [Item]}]. Written atomically, private."""
    data = {"written": datetime.now(timezone.utc).isoformat(), "tiles": [
        {**{k: v for k, v in t.items() if k != "items"},
         "items": [json.loads(item_to_json(i)) for i in t["items"][:25]]} for t in tiles]}
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SNAPSHOT_PATH)


def read_snapshot() -> tuple[datetime | None, list[dict]]:
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
    except (OSError, ValueError):
        return None, []
    tiles = []
    for t in data.get("tiles", []):
        t["items"] = [item_from_json(json.dumps(i)) for i in t.get("items", [])]
        tiles.append(t)
    return parse_time(data.get("written")), tiles
