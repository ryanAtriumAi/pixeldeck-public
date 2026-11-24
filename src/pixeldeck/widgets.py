"""Text boxes that edit like a Mac text field, whatever the terminal sends for Option / Cmd.

Terminals report the same keys differently: Option+← can arrive as alt+left (xterm style),
as Esc-b (iTerm "Natural Text Editing", Terminal.app's Option-as-Meta) which Textual reads as
ctrl+left, and Option+Delete as alt+backspace or ctrl+w. Every variant maps to the same action.
Stock Input binds alt+backspace to delete-word-RIGHT; here it deletes the word to the left.
"""

from __future__ import annotations

import re

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.widgets import Input, TextArea
from textual.widgets.input import Selection

from . import pixel as px


class TextInput(Input):
    BINDINGS = [
        Binding("alt+left,ctrl+left", "cursor_left_word", "word left", show=False),
        Binding("alt+right,ctrl+right", "cursor_right_word", "word right", show=False),
        Binding("alt+shift+left,ctrl+shift+left", "cursor_left_word(True)", show=False),
        Binding("alt+shift+right,ctrl+shift+right", "cursor_right_word(True)", show=False),
        Binding("alt+backspace,ctrl+backspace,ctrl+w", "delete_left_word", "delete word", show=False),
        Binding("alt+d,alt+delete,ctrl+delete", "delete_right_word", show=False),
        Binding("super+left,ctrl+a,home", "home", show=False),
        Binding("super+right,ctrl+e,end", "end", show=False),
        Binding("super+shift+left,shift+home", "home(True)", show=False),
        Binding("super+shift+right,shift+end", "end(True)", show=False),
        Binding("super+backspace,ctrl+u", "delete_left_all", show=False),
        Binding("super+delete,ctrl+k", "delete_right_all", show=False),
    ]

    _WORD_END = re.compile(r"\W*\w+")

    def _word_end(self) -> int:
        """Mac-style: Option+→ stops at the end of the current/next word, not the start of the one after."""
        m = self._WORD_END.match(self.value, self.cursor_position)
        return m.end() if m else len(self.value)

    def action_cursor_right_word(self, select: bool = False) -> None:
        if self.password:
            return self.action_end(select)
        target = self._word_end()
        if select:
            self.selection = Selection(self.selection.start, target)
        else:
            self.cursor_position = target

    def action_delete_right_word(self) -> None:
        if not self.selection.is_empty:
            self.delete_selection()
        elif self.password:
            self.action_delete_right_all()
        else:
            self.delete(self.cursor_position, self._word_end())


class TextBox(TextArea):
    BINDINGS = [
        Binding("alt+left,ctrl+left", "cursor_word_left", show=False),
        Binding("alt+right,ctrl+right", "cursor_word_right", show=False),
        Binding("alt+shift+left,ctrl+shift+left", "cursor_word_left(True)", show=False),
        Binding("alt+shift+right,ctrl+shift+right", "cursor_word_right(True)", show=False),
        Binding("alt+backspace,ctrl+backspace,ctrl+w", "delete_word_left", show=False),
        Binding("alt+d,alt+delete,ctrl+delete", "delete_word_right", show=False),
        Binding("super+left,ctrl+a,home", "cursor_line_start", show=False),
        Binding("super+right,ctrl+e,end", "cursor_line_end", show=False),
        Binding("super+shift+left,shift+home", "cursor_line_start(True)", show=False),
        Binding("super+shift+right,shift+end", "cursor_line_end(True)", show=False),
        Binding("super+up", "cursor_document_start", show=False),
        Binding("super+down", "cursor_document_end", show=False),
        Binding("super+backspace,ctrl+u", "delete_to_start_of_line", show=False),
    ]

    def action_cursor_document_start(self) -> None:
        self.move_cursor((0, 0))

    def action_cursor_document_end(self) -> None:
        self.move_cursor(self.document.end)


def render_item(item, width: int, phase: float | None = None, glow: float | None = None) -> Text:
    """One feed item as two lines (title, then meta), the way tiles and section pages list them.
    phase: the NEW pulse (0..1). glow: marked words breathing green ↔ pink (a deploy in progress)."""
    top = Text(no_wrap=True)
    if phase is not None:
        lit = px._lerp(px.SHADOW, px.HOTPINK, phase)
        top.append(" NEW ", Style(color=px._lerp(px.DIM, px.VOID, phase), bgcolor=lit, bold=True))
        top.append(" ")
        top.append(item.title, Style(color=px._lerp(px.INK, px.HOTPINK, phase), bold=True))
    else:
        color = px.TONE.get(item.tone, px.INK)
        top.append(f"{item.icon} ", Style(color=color, bold=True))
        start = len(top)
        top.append(item.title, Style(color=px.INK if item.tone != "muted" else px.DIM, bold=item.unread))
        for text, tone in getattr(item, "marks", None) or []:
            at = item.title.find(text) if text else -1
            if at >= 0:
                color = px._lerp(px.GREEN, px.HOTPINK, glow) if glow is not None else px.TONE.get(tone, px.INK)
                top.stylize(Style(color=color, bold=True), start + at, start + at + len(text))
    top.truncate(width, overflow="ellipsis")
    sub = Text(f"  {item.meta}", Style(color=px.DIM), no_wrap=True)
    sub.truncate(width, overflow="ellipsis")
    return Text("\n").join([top, sub])
