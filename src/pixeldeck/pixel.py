"""Tiny pixel-art engine: bitmap font, sprites and half-block rendering.

Two vertical pixels share one terminal cell via ▀ / ▄ with fg/bg colours, so a
5-row glyph takes 3 terminal rows.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

# ── Palette ──────────────────────────────────────────────────────────────────

VOID = "#0b0420"
DEEP = "#140834"
PANEL = "#1b0c45"
PURPLE = "#8b5cf6"
VIOLET = "#b26bff"
BLUE = "#3a86ff"
CYAN = "#4cc9f0"
PINK = "#f72585"
HOTPINK = "#ff5fd2"
YELLOW = "#ffd23f"
RED = "#ff3864"
GREEN = "#3dff8f"  # "it's live": deployed versions
INK = "#e9ddff"
DIM = "#7d6aa8"
SHADOW = "#2a1260"

TONE = {
    "primary": VIOLET,
    "secondary": CYAN,
    "accent": HOTPINK,
    "success": CYAN,
    "warning": YELLOW,
    "error": RED,
    "muted": DIM,
    "green": GREEN,
}

GRADIENT = [PURPLE, BLUE, CYAN, HOTPINK, PINK]

# ── Font (3x5) ───────────────────────────────────────────────────────────────

FONT = {
    "A": ["###", "#.#", "###", "#.#", "#.#"],
    "C": ["###", "#..", "#..", "#..", "###"],
    "D": ["##.", "#.#", "#.#", "#.#", "##."],
    "E": ["###", "#..", "##.", "#..", "###"],
    "I": ["###", ".#.", ".#.", ".#.", "###"],
    "K": ["#.#", "#.#", "##.", "#.#", "#.#"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "P": ["###", "#.#", "###", "#..", "#..", ],
    "R": ["##.", "#.#", "##.", "#.#", "#.#"],
    "S": ["###", "#..", "###", "..#", "###"],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "X": ["#.#", "#.#", ".#.", "#.#", "#.#"],
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    ":": [".", "#", ".", "#", "."],
    " ": [".", ".", ".", ".", "."],
    "!": ["#", "#", "#", ".", "#"],
}

# ── Sprites ──────────────────────────────────────────────────────────────────
# Letters map to palette entries; "." is transparent.

SPRITE_COLORS = {"p": PURPLE, "b": BLUE, "c": CYAN, "k": PINK, "h": HOTPINK, "w": INK, "s": SHADOW}

INVADER = [
    [
        "..k.....k..",
        "...k...k...",
        "..hhhhhhh..",
        ".hhwhhhwhh.",
        "hhhhhhhhhhh",
        "h.hhhhhhh.h",
        "h.h.....h.h",
        "...hh.hh...",
    ],
    [
        "..k.....k..",
        "h..k...k..h",
        "h.hhhhhhh.h",
        "hhhwhhhwhhh",
        "hhhhhhhhhhh",
        ".hhhhhhhhh.",
        "..h.....h..",
        ".h.......h.",
    ],
]

GHOST = [
    ["..ccc..", ".ccccc.", "cwbcwbc", "ccccccc", "ccccccc", "c.c.c.c"],
    ["..ccc..", ".ccccc.", "cbwcbwc", "ccccccc", "ccccccc", ".c.c.c."],
]


def _lerp(a: str, b: str, t: float) -> str:
    a_rgb = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    b_rgb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a_rgb, b_rgb))


def gradient_at(t: float, stops: list[str] = GRADIENT) -> str:
    t = min(max(t, 0.0), 1.0)
    seg = t * (len(stops) - 1)
    i = min(int(seg), len(stops) - 2)
    return _lerp(stops[i], stops[i + 1], seg - i)


Grid = list[list[str | None]]


def render(grid: Grid, bg: str | None = None) -> Text:
    """Render a colour grid to half-block Text (rows paired top/bottom)."""
    if len(grid) % 2:
        grid = grid + [[None] * len(grid[0])]
    out = Text(no_wrap=True)
    for r in range(0, len(grid), 2):
        if r:
            out.append("\n")
        for top, bot in zip(grid[r], grid[r + 1]):
            if top is None and bot is None:
                out.append(" ", Style(bgcolor=bg) if bg else None)
            elif top == bot:
                out.append("█", Style(color=top))
            elif bot is None:
                out.append("▀", Style(color=top, bgcolor=bg))
            elif top is None:
                out.append("▄", Style(color=bot, bgcolor=bg))
            else:
                out.append("▀", Style(color=top, bgcolor=bot))
    return out


def text_grid(text: str, shift: float = 0.0, shadow: bool = True, stops: list[str] = GRADIENT) -> Grid:
    """Bitmap for `text` in the 3x5 font with a column gradient and drop shadow."""
    cols: list[list[bool]] = []
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[" "])
        for x in range(len(glyph[0])):
            cols.append([glyph[y][x] == "#" for y in range(5)])
        cols.append([False] * 5)
    cols = cols[:-1] or [[False] * 5]
    width, height = len(cols) + 1, 6
    grid: Grid = [[None] * width for _ in range(height)]
    for x, col in enumerate(cols):
        for y, on in enumerate(col):
            if on and shadow:
                grid[y + 1][x + 1] = SHADOW
    for x, col in enumerate(cols):
        color = gradient_at(((x / max(len(cols) - 1, 1)) + shift) % 1.0, stops)
        for y, on in enumerate(col):
            if on:
                grid[y][x] = color
    return grid


def sprite_grid(rows: list[str]) -> Grid:
    return [[SPRITE_COLORS.get(ch) for ch in row] for row in rows]


def bar(value: float, width: int = 16, stops: list[str] = GRADIENT) -> Text:
    """Segmented 8-bit meter: ■■■■□□□ with a gradient fill."""
    filled = round(min(max(value, 0), 100) / 100 * width)
    out = Text(no_wrap=True)
    for i in range(width):
        if i < filled:
            out.append("▰", Style(color=gradient_at(i / max(width - 1, 1), stops)))
        else:
            out.append("▱", Style(color=SHADOW))
    return out
