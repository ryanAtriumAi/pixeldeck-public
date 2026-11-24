"""The Mail page (press the Mail tile's number twice): triage the inbox without leaving the keyboard.

Cards slice the newest emails (unread, new since you last looked, today, important, with files).
The side panel previews the highlighted email, then shows who writes to you most and how the week went.
d deletes (to Deleted Items, z undoes), u flips read / unread, space selects several for d or u,
r opens the email with the reply box ready. Changes need Microsoft 365 mail.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from datetime import datetime, timedelta

from rich.style import Style
from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.widgets import OptionList

from . import pixel as px
from .pages import Card, SectionScreen, heading, hbar, spark
from .sources.base import Feed, Item, ago
from .sources.mailstate import mail_key
from .widgets import render_item

DEPTH = 150  # how many of the newest emails the page looks at


class MailPage(SectionScreen):
    NAME = "MAIL"
    TILE = "mail"
    REFRESH = 90.0
    SIDE_TITLE = "PREVIEW"
    SIDE_FOLLOWS = True
    STATE = ("feed", "unread_beyond")  # not `marked`: a selection belongs to one visit

    BINDINGS = [
        *SectionScreen.BINDINGS,
        Binding("d,delete", "trash", "Delete"),
        Binding("u", "read", "Read / unread"),
        Binding("space", "mark", "Select"),
        Binding("r", "reply", "Reply"),
        Binding("z", "undo", "Undo delete"),
    ]

    def __init__(self, card: str | None = None) -> None:
        super().__init__(card)
        self.feed: Feed | None = None
        self.unread_beyond = 0
        self.marked: set[str] = set()  # mail keys selected with space

    def note(self) -> str:
        if self.marked:
            return f"{len(self.marked)} selected · d deletes them · u flips read · esc clears"
        return "d delete · u read / unread · space select · z undo"

    def skeleton(self) -> list[Card]:
        return [
            Card("inbox", "INBOX", tone="primary", empty="inbox zero"),
            Card("unread", "UNREAD", tone="accent", empty="nothing unread"),
            Card("new", "NEW", tone="warning", empty="nothing new since you last looked"),
            Card("today", "TODAY", tone="secondary", empty="nothing arrived today"),
            Card("important", "IMPORTANT", tone="error", empty="nothing flagged important"),
            Card("files", "WITH FILES", tone="primary", empty="no attachments"),
        ]

    async def fetch(self) -> list[Card]:
        self.feed = await self.app.mail.inbox(limit=DEPTH)
        self.app.mail_arrived(self.feed)
        # unread mail older than the emails listed here still counts on the UNREAD card
        self.unread_beyond = max((self.feed.count or 0) - sum(1 for it in self.feed.items if it.unread), 0)
        keys = {mail_key(it) for it in self.feed.items}
        self.marked &= keys
        return self._cards()

    # cards ------------------------------------------------------------------------

    def _cards(self) -> list[Card]:
        cards = self.skeleton()
        if self.feed is None:
            return cards
        items = self.feed.items
        today = datetime.now().astimezone().date()
        flag = lambda it, name: bool((it.ref or {}).get(name))
        sets = {
            "inbox": items,
            "unread": [it for it in items if it.unread],
            "new": [it for it in items if it.flash],
            "today": [it for it in items if it.when and it.when.astimezone().date() == today],
            "important": [it for it in items if flag(it, "important")],
            "files": [it for it in items if flag(it, "files")],
        }
        unread_total = len(sets["unread"]) + self.unread_beyond
        hints = {
            "inbox": f"newest {len(items)}",
            "unread": "in your inbox" if unread_total > len(sets["unread"]) else "to read",
            "new": "since you looked",
            "today": f"{sum(1 for it in sets['today'] if it.unread)} unread",
            "important": f"{sum(1 for it in sets['important'] if it.unread)} unread",
            "files": "attachments",
        }
        for c in cards:
            c.rows = list(sets[c.key])
            c.value = str(unread_total if c.key == "unread" else len(c.rows))
            c.hint = hints[c.key]
        return cards

    def row_text(self, item: Item, width: int) -> Text:
        text = render_item(item, width - 2, 1.0 if item.flash else None)
        on = mail_key(item) in self.marked
        out = Text(no_wrap=True)
        for i, line in enumerate(text.split("\n")):
            if i:
                out.append("\n")
            out.append(("◉ " if on else "  ") if i == 0 else "  ", Style(color=px.YELLOW, bold=True))
            out.append_text(line)
        return out

    # side panel -------------------------------------------------------------------

    def side(self, width: int) -> Text | None:
        if self.feed is None:
            return None
        out = Text()
        item = self.current()
        if item is not None:
            ref = item.ref or {}
            who, _, _ = item.meta.partition(" · ")
            out.append(f"{who}\n", Style(color=px.HOTPINK, bold=True))
            if item.sender and item.sender.lower() not in who.lower():
                out.append(f"{item.sender}\n", Style(color=px.DIM))
            when = f"{item.when.astimezone():%a %d %b %H:%M} · {ago(item.when)}" if item.when else ""
            out.append(f"{when}\n", Style(color=px.DIM))
            tags = [t for t, yes in (("UNREAD", item.unread), ("IMPORTANT", ref.get("important")),
                                     ("HAS FILES", ref.get("files"))) if yes]
            for t in tags:
                out.append(f" {t} ", Style(color=px.VOID, bgcolor=px.YELLOW if t == "UNREAD" else px.CYAN, bold=True))
                out.append(" ")
            out.append("\n" if tags else "")
            out.append("\n")
            for line in textwrap.wrap(item.title, width)[:3]:
                out.append(f"{line}\n", Style(color=px.INK, bold=True))
            preview = ref.get("preview") or ""
            if preview:
                out.append("\n")
                for line in textwrap.wrap(preview, width)[:8]:
                    out.append(f"{line}\n", Style(color=px.INK))
                out.append("⏎ opens the whole email\n", Style(color=px.DIM))
            if not self.app.mail.can_change(item):
                out.append("\nread-only here: d and u need Microsoft 365 mail\n", Style(color=px.DIM))
            out.append("\n")

        items = self.feed.items
        senders = Counter(it.meta.partition(" · ")[0] for it in items).most_common(6)
        if senders:
            out.append_text(heading(f"WHO WRITES MOST · newest {len(items)}"))
            for name, count in senders:
                out.append_text(hbar(name, count, senders[0][1], width, px.VIOLET, label_w=min(18, width // 2)))
            out.append("\n")

        today = datetime.now().astimezone().date()
        days = [today - timedelta(days=d) for d in range(6, -1, -1)]
        got = Counter(it.when.astimezone().date() for it in items if it.when)
        oldest = min((it.when for it in items if it.when), default=None)
        if oldest and oldest.astimezone().date() <= days[0]:  # only when the list reaches back a full week
            out.append_text(heading("LAST 7 DAYS"))
            out.append_text(spark("received", [got.get(d, 0) for d in days], px.CYAN))
            out.append(" " * 9 + "".join(f"{d:%a}"[0] for d in days) + "\n", Style(color=px.DIM))
        return out

    # actions ----------------------------------------------------------------------

    def after_view(self, item: Item | None) -> None:
        if self.feed is not None:  # read or deleted while it was open
            dead = {id(it) for it, _ in self.app._mail_trashed}
            self.feed.items[:] = [it for it in self.feed.items if id(it) not in dead]
            self.set_cards(self._cards())

    def _all_rows(self) -> list[Item]:
        return self.feed.items if self.feed else []

    def _targets(self) -> list[Item]:
        """What d / u act on: the selected emails, else the highlighted one."""
        if self.marked:
            return [it for it in self._all_rows() if mail_key(it) in self.marked]
        return [it] if (it := self.current()) else []

    def action_mark(self) -> None:
        if (item := self.current()) is None:
            return
        key = mail_key(item)
        self.marked.symmetric_difference_update({key})
        self.redraw_row(item)
        self._draw_head()
        lst = self.query_one("#pg-list", OptionList)
        lst.action_cursor_down()

    @work(group="mail-change")
    async def action_trash(self) -> None:
        targets = self._targets()
        if not targets:
            return
        gone = await self.app.mail_trash(targets)
        if not gone or self.gone():
            return
        dead = {id(it) for it in gone}
        keep = self.current()
        if keep is not None and id(keep) in dead:  # land on the row that slides into its place
            i = self.current_index() or 0
            rest = [it for it in self.shown[i:] if id(it) not in dead] or \
                   [it for it in reversed(self.shown[:i]) if id(it) not in dead]
            keep = rest[0] if rest else None
        self.feed.items[:] = [it for it in self.feed.items if id(it) not in dead]
        self.marked -= {mail_key(it) for it in gone}
        self.cards = self._cards()
        self._paint_cards()
        self._fill_list(self.item_key(keep) if keep else "")
        self.draw_side()
        self._draw_head()

    @work(group="mail-change")
    async def action_read(self) -> None:
        targets = self._targets()
        if not targets:
            return
        make_read = any(it.unread for it in targets)  # anything unread: read them all; else unread them all
        changed = 0
        for it in targets:
            if it.unread != make_read:
                continue
            if not await self.app.mail_set_read(it, make_read):
                break
            changed += 1
        if changed and not self.gone():
            self.marked.clear()
            self.set_cards(self._cards())
            self._draw_head()

    @work(group="mail-change")
    async def action_undo(self) -> None:
        if await self.app.mail_undo() is not None and not self.gone():
            self.reload()

    def action_reply(self) -> None:
        if (i := self.current_index()) is not None:
            self.open_row(i, reply=True)

    def action_back(self) -> None:
        if self.marked and not self._typing():
            self.marked.clear()
            self._fill_list()
            self._draw_head()
            return
        super().action_back()
