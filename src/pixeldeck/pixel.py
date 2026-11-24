"""Tiny pixel-art engine: bitmap font, sprites and half-block rendering.

Two vertical pixels share one terminal cell via ▀ / ▄ with fg/bg colours, so a
5-row glyph takes 3 terminal rows.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

# ── Palette ──────────────────────────────────────────────────────────────────
# The names say what a colour was in the first theme (ARCADE); every theme fills the same slots, so
# PURPLE is "the main frame colour", HOTPINK "the accent", CYAN "the second colour" and so on.

THEMES: dict[str, dict] = {
    "arcade": {
        "label": "ARCADE", "about": "the original", "dark": True,
        "VOID": "#0b0420", "DEEP": "#140834", "PANEL": "#1b0c45", "SHADOW": "#2a1260",
        "SELECT": "#3a1670", "FOCUS": "#4a1a7a", "FENCE": "#120628", "INLINE": "#cbb8ff", "INLINE_BG": "#221142",
        "PURPLE": "#8b5cf6", "VIOLET": "#b26bff", "BLUE": "#3a86ff", "CYAN": "#4cc9f0",
        "PINK": "#f72585", "HOTPINK": "#ff5fd2", "YELLOW": "#ffd23f", "RED": "#ff3864", "GREEN": "#3dff8f",
        "INK": "#e9ddff", "DIM": "#7d6aa8",
    },
    "phosphor": {
        "label": "PHOSPHOR", "about": "green CRT", "dark": True,
        "VOID": "#020a04", "DEEP": "#06140a", "PANEL": "#0a1f10", "SHADOW": "#123a1c",
        "SELECT": "#175226", "FOCUS": "#1f6b31", "FENCE": "#03100a", "INLINE": "#b8ffcc", "INLINE_BG": "#0d2a15",
        "PURPLE": "#2fbf5b", "VIOLET": "#5ee08a", "BLUE": "#1f9d6b", "CYAN": "#7dffb0",
        "PINK": "#b4f000", "HOTPINK": "#d6ff5c", "YELLOW": "#ffe066", "RED": "#ff5c5c", "GREEN": "#39ff14",
        "INK": "#d5ffe0", "DIM": "#4f8f62",
    },
    "amber": {
        "label": "AMBER", "about": "amber terminal", "dark": True,
        "VOID": "#0d0700", "DEEP": "#1a0f02", "PANEL": "#261705", "SHADOW": "#40280a",
        "SELECT": "#5a380d", "FOCUS": "#734810", "FENCE": "#120a01", "INLINE": "#ffd9a0", "INLINE_BG": "#2e1c06",
        "PURPLE": "#d98a1f", "VIOLET": "#ffb347", "BLUE": "#b3661a", "CYAN": "#ffd27a",
        "PINK": "#ff7a1a", "HOTPINK": "#ff9d3c", "YELLOW": "#fff07a", "RED": "#ff4d3d", "GREEN": "#c9e265",
        "INK": "#ffe8c2", "DIM": "#9a7648",
    },
    "ocean": {
        "label": "OCEAN", "about": "navy, teal and coral", "dark": True,
        "VOID": "#04101c", "DEEP": "#071a2c", "PANEL": "#0b2540", "SHADOW": "#123a5e",
        "SELECT": "#17507f", "FOCUS": "#1d659e", "FENCE": "#050f1a", "INLINE": "#b9e1ff", "INLINE_BG": "#0f2f4f",
        "PURPLE": "#2f9bd6", "VIOLET": "#6cc4f5", "BLUE": "#2f6fd6", "CYAN": "#4fe3d0",
        "PINK": "#ff6b6b", "HOTPINK": "#ff8e72", "YELLOW": "#ffd166", "RED": "#ff4d6d", "GREEN": "#5ef2a0",
        "INK": "#dcefff", "DIM": "#6589a8",
    },
    "frost": {
        "label": "FROST", "about": "soft blue-grey", "dark": True,
        "VOID": "#1b202b", "DEEP": "#232a38", "PANEL": "#2b3445", "SHADOW": "#3b4559",
        "SELECT": "#46526b", "FOCUS": "#52617f", "FENCE": "#181d27", "INLINE": "#d8dee9", "INLINE_BG": "#303a4d",
        "PURPLE": "#81a1c1", "VIOLET": "#a3bfe0", "BLUE": "#5e81ac", "CYAN": "#88c0d0",
        "PINK": "#b48ead", "HOTPINK": "#d8a0c8", "YELLOW": "#ebcb8b", "RED": "#bf616a", "GREEN": "#a3be8c",
        "INK": "#e5e9f0", "DIM": "#7b88a1",
    },
    "mono": {
        "label": "MONO", "about": "greyscale", "dark": True,
        "VOID": "#0a0a0a", "DEEP": "#141414", "PANEL": "#1e1e1e", "SHADOW": "#333333",
        "SELECT": "#444444", "FOCUS": "#585858", "FENCE": "#101010", "INLINE": "#e0e0e0", "INLINE_BG": "#2a2a2a",
        "PURPLE": "#a0a0a0", "VIOLET": "#c8c8c8", "BLUE": "#808080", "CYAN": "#e0e0e0",
        "PINK": "#f5f5f5", "HOTPINK": "#ffffff", "YELLOW": "#ffd866", "RED": "#ff6666", "GREEN": "#9aff9a",
        "INK": "#f2f2f2", "DIM": "#808080",
    },
    "daylight": {
        "label": "DAYLIGHT", "about": "light, for bright rooms", "dark": False,
        "VOID": "#f6f1e7", "DEEP": "#ece5d6", "PANEL": "#e0d7c4", "SHADOW": "#cfc4ac",
        "SELECT": "#d9cdf2", "FOCUS": "#c6b3ee", "FENCE": "#e6dfcf", "INLINE": "#4a2fa0", "INLINE_BG": "#ddd3ee",
        "PURPLE": "#6d3fd1", "VIOLET": "#5b2fc0", "BLUE": "#1f5fd1", "CYAN": "#007a8a",
        "PINK": "#d6156b", "HOTPINK": "#c2188f", "YELLOW": "#a36a00", "RED": "#c8102e", "GREEN": "#0d8a3f",
        "INK": "#2a2140", "DIM": "#7a6f8a",
    },
}
DEFAULT_THEME = "arcade"
SLOTS = [k for k in THEMES[DEFAULT_THEME] if k.isupper()]

ACTIVE = DEFAULT_THEME
DARK = True
VOID = DEEP = PANEL = SHADOW = SELECT = FOCUS = FENCE = INLINE = INLINE_BG = ""
PURPLE = VIOLET = BLUE = CYAN = PINK = HOTPINK = YELLOW = RED = GREEN = INK = DIM = ""
TONE: dict[str, str] = {}
GRADIENT: list[str] = []
SPRITE_COLORS: dict[str, str] = {}  # letters in the sprites below map to palette entries; "." is transparent


def css_vars(name: str) -> dict[str, str]:
    """A theme as stylesheet variables: $pd-void, $pd-hotpink, $pd-inline-bg… (every screen's CSS uses these,
    so switching the Textual theme restyles everything at once)."""
    theme = THEMES[name]
    return {f"pd-{slot.lower().replace('_', '-')}": theme[slot] for slot in SLOTS}


def apply_theme(name: str | None) -> str:
    """Switch the palette that code draws with (px.VOID, px.TONE, the logo gradient…). Stylesheets follow
    the Textual theme instead (see css_vars and PixelDeck.set_look). Returns the theme in use."""
    global ACTIVE, DARK
    ACTIVE = name if name in THEMES else DEFAULT_THEME
    theme = THEMES[ACTIVE]
    DARK = theme["dark"]
    g = globals()
    for slot in SLOTS:
        g[slot] = theme[slot]
    TONE.clear()
    TONE.update({"primary": VIOLET, "secondary": CYAN, "accent": HOTPINK, "success": CYAN, "warning": YELLOW,
                 "error": RED, "muted": DIM, "green": GREEN})
    GRADIENT[:] = [PURPLE, BLUE, CYAN, HOTPINK, PINK]
    SPRITE_COLORS.clear()
    SPRITE_COLORS.update({"p": PURPLE, "b": BLUE, "c": CYAN, "k": PINK, "h": HOTPINK, "w": INK, "s": SHADOW})
    return ACTIVE


apply_theme(DEFAULT_THEME)

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
    "+": ["...", ".#.", "###", ".#.", "..."],
    "-": ["...", "...", "###", "...", "..."],
    ".": [".", ".", ".", ".", "#"],
    "%": ["#.#", "..#", ".#.", "#..", "#.#"],
    "/": ["..#", "..#", ".#.", "#..", "#.."],
    "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "M": ["#.#", "###", "###", "#.#", "#.#"],
    "W": ["#.#", "#.#", "###", "###", "#.#"],
}

# ── Sprites ──────────────────────────────────────────────────────────────────

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
