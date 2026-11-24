"""Section pages: the full page behind a tile (press the tile's number twice, e.g. 88 for Linear).

A page is a strip of metric cards, the list for the card you picked, and a side panel of charts.
Picking a card (←/→, its number, or a click) lists what that number counts; Enter on a row opens it
in a popup. A subclass says what the cards are (`skeleton`, `fetch`) and what the side panel shows.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from . import pixel as px
from .sources.base import Item, NotConfigured, is_network_error
from .widgets import TextInput, render_item


@dataclass
class Group:
    """A heading inside a list, e.g. URGENT · 3."""

    title: str
    tone: str = "muted"


@dataclass
class Card:
    """One number on the landing strip. Selecting it lists `rows`."""

    key: str
    label: str
    value: str = ""  # "" while loading
    hint: str = ""
    tone: str = "primary"
    rows: list[Item | Group] | None = field(default_factory=list)  # None: a number only, nothing to list
    empty: str = "nothing here"

    @property
    def items(self) -> list[Item]:
        return [r for r in self.rows or [] if isinstance(r, Item)]


# ── drawing helpers for side panels ─────────────────────────────────────────────

SPARKS = "▁▂▃▄▅▆▇█"


def heading(text: str, color: str = px.CYAN) -> Text:
    return Text(f"{text}\n", Style(color=color, bold=True))


def hbar(label: str, value: int, peak: int, width: int, color: str, label_w: int = 11) -> Text:
    """`LABEL      ▰▰▰▰▱▱▱▱  12`: one row of a bar chart."""
    num = f"{value:>4}"
    cells = max(width - label_w - len(num) - 2, 4)
    filled = round(cells * value / peak) if peak else 0
    if value and not filled:
        filled = 1
    out = Text(no_wrap=True)
    out.append(f"{label[:label_w]:<{label_w}} ", Style(color=px.INK if value else px.DIM))
    out.append("▰" * filled, Style(color=color))
    out.append("▱" * (cells - filled), Style(color=px.SHADOW))
    out.append(f" {num}\n", Style(color=color if value else px.DIM, bold=bool(value)))
    return out


def spark(label: str, values: list[int], color: str, peak: int | None = None, label_w: int = 8) -> Text:
    """`created ▂▅▇▃▁▂▄█  23`: one series as a row of blocks, oldest first, with its total."""
    top = max(peak if peak is not None else max(values, default=0), 1)
    out = Text(no_wrap=True)
    out.append(f"{label[:label_w]:<{label_w}} ", Style(color=px.INK))
    for v in values:
        out.append(SPARKS[min(round(v / top * 7), 7)] if v else "·", Style(color=color if v else px.SHADOW))
    out.append(f"  {sum(values)}\n", Style(color=color, bold=True))
    return out


def kv(label: str, value: str, color: str = px.INK, label_w: int = 14) -> Text:
    out = Text(no_wrap=True)
    out.append(f"{label[:label_w]:<{label_w}} ", Style(color=px.DIM))
    out.append(f"{value}\n", Style(color=color, bold=True))
    return out


def big_number(value: str, tone: str) -> Text:
    """The card's headline in the pixel font (3 rows); plain text when a glyph is missing."""
    color = px.TONE.get(tone, px.INK)
    if not value:
        return Text("\n· · ·\n", Style(color=px.DIM), justify="center")
    if len(value) <= 6 and all(ch.upper() in px.FONT for ch in value):
        art = px.render(px.text_grid(value, shadow=False, stops=[color, px._lerp(color, px.INK, 0.45)]))
        art.justify = "center"
        return art
    return Text(f"\n{value}\n", Style(color=color, bold=True), justify="center")


class CardBox(Static):
    """One metric card. Clicking it picks it."""

    class Picked(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(self, card: Card, num: int) -> None:
        super().__init__(classes="pg-card")
        self.card, self.num = card, num

    def show(self, card: Card, selected: bool) -> None:
        self.card = card
        self.set_class(selected, "-on")
        self.set_class(card.rows is None, "-flat")
        self.border_title = f" {self.num} {card.label} " if card.rows is not None else f" {card.label} "
        self.border_subtitle = f" {card.hint} " if card.hint else ""
        self.update(big_number(card.value, card.tone if card.value not in ("0", "") else "muted"))

    def on_click(self) -> None:
        if self.card.rows is not None:
            self.post_message(self.Picked(self.card.key))


PAGE_CSS = f"""
SectionScreen {{ background: {px.VOID}; }}
#pg-head {{ height: auto; background: {px.DEEP}; padding: 1 2 0 2; }}
#pg-rule {{ height: 1; }}
#pg-cards {{ height: 5; padding: 0 1; background: {px.VOID}; }}
.pg-card {{ width: 1fr; min-width: 12; height: 5; padding: 0 1; background: {px.DEEP}; border: round {px.PURPLE};
            border-title-color: {px.INK}; border-subtitle-color: {px.DIM}; text-align: center; }}
.pg-card:hover {{ background: {px.PANEL}; }}
.pg-card.-flat {{ border: round {px.SHADOW}; border-title-color: {px.DIM}; }}
.pg-card.-flat:hover {{ background: {px.DEEP}; }}
.pg-card.-on {{ border: heavy {px.HOTPINK}; background: {px.PANEL}; border-title-color: {px.HOTPINK};
                border-title-style: bold; border-subtitle-color: {px.INK}; }}
#pg-body {{ height: 1fr; padding: 0 1; }}
#pg-left {{ width: 1fr; height: 1fr; }}
#pg-filter {{ display: none; height: 3; border: tall {px.HOTPINK}; background: {px.DEEP}; }}
#pg-filter.-on {{ display: block; }}
#pg-list {{ height: 1fr; background: {px.DEEP}; border: panel {px.PURPLE}; border-title-color: {px.VOID};
            border-title-style: bold; border-subtitle-color: {px.DIM}; padding: 0; scrollbar-size-vertical: 1;
            scrollbar-color: {px.PURPLE}; scrollbar-color-hover: {px.HOTPINK}; scrollbar-background: {px.DEEP}; }}
#pg-list:focus {{ border: panel {px.HOTPINK}; }}
#pg-list > .option-list--option {{ padding: 0 1; }}
#pg-list > .option-list--option-highlighted {{ background: {px.SHADOW}; }}
#pg-list:focus > .option-list--option-highlighted {{ background: {px.FOCUS}; }}
#pg-list > .option-list--option-hover {{ background: {px.PANEL}; }}
#pg-list > .option-list--separator {{ color: {px.SHADOW}; }}
#pg-side {{ width: 48; height: 1fr; margin-left: 1; background: {px.DEEP}; border: panel {px.CYAN};
            border-title-color: {px.VOID}; border-title-style: bold; padding: 0 1; scrollbar-size-vertical: 1;
            scrollbar-color: {px.PURPLE}; }}
"""


class SectionScreen(Screen):
    """Base for a tile's page. Subclasses set NAME / TILE and implement `skeleton` and `fetch`."""

    CSS = PAGE_CSS
    NAME = "PAGE"  # shown in the header
    TILE = ""  # the dashboard tile this page belongs to (its number is shown, its job re-runs on leaving)
    REFRESH = 180.0  # seconds between background reloads; 0 turns them off
    SIDE_TITLE = "AT A GLANCE"
    SIDE_FOLLOWS = False  # redraw the side panel when the highlighted row changes (a preview)
    NARROW = 112  # below this width the side panel hides

    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("left", "card(-1)", "◀ Card"),
        Binding("right", "card(1)", "Card ▶"),
        Binding("slash", "filter", "Filter"),
        Binding("o", "open", "Open in browser"),
        Binding("c", "copy", "Copy link"),
        Binding("ctrl+r", "reload", "Reload"),
        *[Binding(str(n), f"pick({n})", show=False) for n in range(1, 10)],
    ]

    def __init__(self, card: str | None = None) -> None:
        super().__init__()
        self.cards: list[Card] = []
        self.card_key = card  # the selected card
        self.shown: list[Item] = []  # the items listed right now, in order
        self.needle = ""
        self.busy = False
        self.loaded_at: float | None = None
        self.error: str | None = None
        self._boxes: list[CardBox] = []

    # what a subclass provides -----------------------------------------------------

    def skeleton(self) -> list[Card]:
        """The cards before any data arrives (labels only), so the page has its shape at once."""
        return []

    async def fetch(self) -> list[Card]:
        """Load everything and return the cards. Raise to show the error in the header."""
        raise NotImplementedError

    def side(self, width: int) -> Text | None:
        """The side panel, `width` columns wide. None hides nothing but leaves it blank."""
        return None

    def note(self) -> str:
        """Extra words for the header (e.g. the period)."""
        return ""

    def row_text(self, item: Item, width: int) -> Text:
        return render_item(item, width, 1.0 if item.flash else None)

    def after_view(self, item: Item | None) -> None:
        """Called after a popup closes (the item may have changed: read, acknowledged)."""

    # layout ---------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        from .app import Divider

        yield Static(id="pg-head")
        yield Divider(id="pg-rule")
        yield Horizontal(id="pg-cards")
        with Horizontal(id="pg-body"):
            with Vertical(id="pg-left"):
                yield TextInput(placeholder="filter this list…  (esc clears)", id="pg-filter")
                yield OptionList(id="pg-list")
            with VerticalScroll(id="pg-side"):
                yield Static(id="pg-side-text")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pg-side", VerticalScroll).border_title = f" {self.SIDE_TITLE} "
        self.query_one("#pg-side", VerticalScroll).can_focus = False
        self._fit()
        self.set_cards(self.skeleton())
        self.query_one("#pg-list", OptionList).focus()
        self.reload()
        if self.REFRESH:
            self.set_interval(self.REFRESH, self.reload)
        self.set_interval(5, self._draw_head)

    def _fit(self) -> None:
        """The side panel only shows when there's room for it beside the list."""
        if self.gone():
            return
        self.query_one("#pg-side", VerticalScroll).display = self.app.size.width >= self.NARROW

    def on_resize(self) -> None:
        self._fit()
        self.call_after_refresh(self._fill_list)
        self.call_after_refresh(self.draw_side)

    # header ---------------------------------------------------------------------

    def _tile_num(self) -> str:
        from .app import Tile

        for t in self.app.query(Tile):
            if t.key == self.TILE:
                return f"{t.num} ▸ "
        return ""

    def _draw_head(self) -> None:
        if not self.query("#pg-head"):
            return  # the page was closed mid-load: its worker is winding down
        out = Text(no_wrap=True)
        out.append(f" {self._tile_num()}{self.NAME} ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        if note := self.note():
            out.append(f"  {note}", Style(color=px.INK))
        if self.error:
            out.append(f"  ⚠ {self.error}", Style(color=px.RED, bold=True))
        elif self.busy and self.loaded_at is None:
            out.append("  loading…", Style(color=px.DIM))
        elif self.loaded_at is not None:
            secs = int(time.monotonic() - self.loaded_at)
            age = f"{secs}s" if secs < 60 else f"{secs // 60}m"
            out.append(f"  {'syncing…' if self.busy else 'synced ' + age + ' ago'}", Style(color=px.DIM))
        self.query_one("#pg-head", Static).update(out)

    # data -----------------------------------------------------------------------

    @work(exclusive=True, group="pg-load")
    async def reload(self) -> None:
        self.busy = True
        self._draw_head()
        try:
            cards = await self.fetch()
        except NotConfigured as exc:
            self.error = str(exc)
        except Exception as exc:  # a page shows what went wrong and keeps what it had
            self.error = "offline · will retry" if is_network_error(exc) else f"{type(exc).__name__}: {exc}"[:160]
        else:
            if self.gone():
                return
            self.error = None
            self.loaded_at = time.monotonic()
            self.set_cards(cards)
        finally:
            self.busy = False
            self._draw_head()

    def gone(self) -> bool:
        """This page has been left: a job that was still running must not touch its widgets."""
        return self not in self.app.screen_stack

    def action_reload(self) -> None:
        self.reload()

    def set_cards(self, cards: list[Card]) -> None:
        """Show these cards (same keys: updated in place; different keys: the strip is rebuilt)."""
        old = [c.key for c in self.cards]
        self.cards = cards
        listable = [c.key for c in cards if c.rows is not None]
        if self.card_key not in listable:
            self.card_key = listable[0] if listable else None
        if old == [c.key for c in cards] and len(self._boxes) == len(cards):
            self._paint_cards()
            self._fill_list()
            self.draw_side()
        else:
            self.call_later(self._remount_cards)

    async def _remount_cards(self) -> None:
        if self.gone():
            return
        strip = self.query_one("#pg-cards", Horizontal)
        await strip.remove_children()
        self._boxes = [CardBox(c, n) for n, c in enumerate(self.cards, 1)]
        await strip.mount_all(self._boxes)
        self._paint_cards()
        self._fill_list()
        self.draw_side()
        self._draw_head()

    def _paint_cards(self) -> None:
        for box, card in zip(self._boxes, self.cards):
            box.show(card, card.key == self.card_key)

    @property
    def card(self) -> Card | None:
        return next((c for c in self.cards if c.key == self.card_key), None)

    # the list ---------------------------------------------------------------------

    @staticmethod
    def item_key(item: Item) -> str:
        ref = item.ref or {}
        return str(ref.get("id") or ref.get("url") or item.url or item.title)

    def _list_width(self) -> int:
        return max(self.query_one("#pg-list", OptionList).size.width - 6, 20)

    def _fill_list(self, keep: str | None = None) -> None:
        if self.gone():
            return
        lst = self.query_one("#pg-list", OptionList)
        card = self.card
        if keep is None and (cur := self.current()) is not None:
            keep = self.item_key(cur)
        width = self._list_width()
        words = self.needle.lower().split()
        options: list[Option | None] = []
        self.shown = []
        pending: Group | None = None
        for row in (card.rows or []) if card else []:
            if isinstance(row, Group):
                pending = row
                continue
            if words and not all(w in f"{row.title} {row.meta}".lower() for w in words):
                continue
            if pending is not None:  # a heading only shows when something under it does
                if options:
                    options.append(None)
                head = Text(no_wrap=True)
                head.append(f" {pending.title} ", Style(color=px.VOID, bgcolor=px.TONE.get(pending.tone, px.DIM), bold=True))
                options.append(Option(head, disabled=True))
                pending = None
            options.append(Option(self.row_text(row, width), id=f"r{len(self.shown)}"))
            self.shown.append(row)
            if row.rule_after:
                options.append(None)
        if not self.shown:
            if card is None or (not card.value and self.loaded_at is None):
                msg, color = "  ▒▒▒ LOADING ▒▒▒", px.DIM
            elif words:
                msg, color = f"  nothing matches “{self.needle}”", px.DIM
            else:
                msg, color = f"  ★ {card.empty} ★", px.CYAN
            options = [Option(Text(msg, Style(color=color, bold=color != px.DIM)), disabled=True)]
        lst.set_options(options)
        lst.border_title = f" {card.label} ×{len(self.shown)} " if card else " … "
        lst.border_subtitle = f" / {self.needle} " if words else ""
        index = next((i for i, it in enumerate(self.shown) if keep and self.item_key(it) == keep), 0)
        if self.shown:
            lst.highlighted = lst.get_option_index(f"r{index}")

    def redraw_row(self, item: Item) -> None:
        """Re-render one listed row in place (after its state changed)."""
        for i, it in enumerate(self.shown):
            if it is item:
                try:
                    self.query_one("#pg-list", OptionList).replace_option_prompt(
                        f"r{i}", self.row_text(item, self._list_width()))
                except Exception:
                    pass

    def current_index(self) -> int | None:
        opt = self.query_one("#pg-list", OptionList).highlighted_option
        oid = (opt.id or "") if opt else ""
        return int(oid[1:]) if oid[1:].isdigit() and int(oid[1:]) < len(self.shown) else None

    def current(self) -> Item | None:
        i = self.current_index()
        return self.shown[i] if i is not None else None

    @on(OptionList.OptionHighlighted, "#pg-list")
    def _highlighted(self) -> None:
        if self.SIDE_FOLLOWS:
            self.draw_side()

    @on(OptionList.OptionSelected, "#pg-list")
    def _selected(self) -> None:
        if (i := self.current_index()) is not None:
            self.open_row(i)

    def open_row(self, index: int, reply: bool = False) -> None:
        """Enter on a row: its popup. Next / Prev in the popup walk this list."""
        items = self.shown

        def closed(i: int | None) -> None:
            it = items[i] if i is not None and i < len(items) else None
            self._paint_cards()
            self._fill_list(self.item_key(it) if it else None)
            self.query_one("#pg-list", OptionList).focus()
            self.after_view(it)

        self.app.open_items(items, index, self.NAME, closed, reply=reply)

    # the side panel ---------------------------------------------------------------

    def draw_side(self) -> None:
        if self.gone():
            return
        box = self.query_one("#pg-side", VerticalScroll)
        text = self.side(max(box.size.width - 4, 20)) if box.size.width else None
        self.query_one("#pg-side-text", Static).update(text or Text(""))

    # actions ----------------------------------------------------------------------

    def pick_card(self, key: str) -> None:
        if key != self.card_key and any(c.key == key and c.rows is not None for c in self.cards):
            self.card_key = key
            self._paint_cards()
            self._fill_list(keep="")
            self.draw_side()

    @on(CardBox.Picked)
    def _card_clicked(self, e: CardBox.Picked) -> None:
        self.pick_card(e.key)
        self.query_one("#pg-list", OptionList).focus()

    def action_card(self, delta: int) -> None:
        keys = [c.key for c in self.cards if c.rows is not None]
        if self.card_key in keys:
            self.pick_card(keys[(keys.index(self.card_key) + delta) % len(keys)])

    def action_pick(self, n: int) -> None:
        if 0 < n <= len(self.cards):
            self.pick_card(self.cards[n - 1].key)

    def _filter_box(self) -> TextInput:
        return self.query_one("#pg-filter", TextInput)

    def action_filter(self) -> None:
        box = self._filter_box()
        box.add_class("-on")
        box.focus()

    @on(TextInput.Changed, "#pg-filter")
    def _filter_changed(self, e: TextInput.Changed) -> None:
        self.needle = e.value.strip()
        self._fill_list()

    @on(TextInput.Submitted, "#pg-filter")
    def _filter_done(self) -> None:
        self.query_one("#pg-list", OptionList).focus()

    def _typing(self) -> bool:
        return self.focused is self._filter_box()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self._typing() and action not in ("back", "reload", "filter"):
            return False  # letters and arrows belong to the filter box while you type in it
        return True

    def action_back(self) -> None:
        box = self._filter_box()
        if box.has_class("-on") and (self._typing() or self.needle):
            box.value = ""
            box.remove_class("-on")
            self.query_one("#pg-list", OptionList).focus()
            return
        self.dismiss(None)

    def action_open(self) -> None:
        if (item := self.current()) and item.url:
            subprocess.Popen(["open", item.url])
            self.app.item_viewed(item)
            self.redraw_row(item)
            self.after_view(item)

    def action_copy(self) -> None:
        if (item := self.current()) and item.url:
            subprocess.run(["pbcopy"], input=item.url.encode())
            self.notify(item.url, title="COPIED", timeout=3)
