"""Now playing: a full-screen Spotify page.

The record slides out of its sleeve when the page opens and spins while the track plays, a little
dance floor of sprites bops on the beat, and an 8-bit equalizer runs along the bottom. Spotify's
AppleScript gives no audio data or tempo, so the equalizer is a show, not a spectrum: it moves to a
steady ~118 bpm pulse while playing and drops when you pause.
"""

from __future__ import annotations

import math
import random
import time

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from . import pixel as px
from .sources.spotify import NowPlaying, fmt_time

FPS = 12
BPM = 118
INTRO = 1.2  # seconds for the record to slide out and the bars to rise
# The cover is fetched small and blown up into big square blocks, so it reads as pixel art like the
# dashboard's 8×8 cover instead of a blurry photo: sleeve size in pixels -> art resolution.
ART_RES = {48: 16, 32: 16, 24: 12, 16: 8}

NOTE = ["....hhh.", "....h.hh", "....h...", "....h...", "..hhh...", ".hhhh...", ".hhh....", "........"]
EQ_STOPS = [px.CYAN, px.BLUE, px.VIOLET, px.HOTPINK, px.PINK]

CSS = f"""
SpotifyScreen {{ background: {px.VOID}; }}
#sp-head {{ height: auto; background: {px.DEEP}; padding: 1 2; }}
#sp-stage {{ height: 1fr; align-vertical: middle; }}
#sp-top {{ height: auto; padding: 1 2 0 2; }}
#sp-scene {{ width: auto; height: auto; }}
#sp-info {{ width: 1fr; height: auto; padding: 1 0 0 4; }}
#sp-eq {{ height: auto; padding: 0 2; }}
"""


def _mix(a: str, b: str, t: float) -> str:
    return px._lerp(a, b, min(max(t, 0.0), 1.0))


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


class SpotifyScreen(Screen):
    """Big cover art, a spinning record, the track, and an equalizer that dances while it plays."""

    CSS = CSS
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("p,space", "cmd('playpause')", "Play/Pause", key_display="p"),
        Binding("less_than_sign,comma", "cmd('previous')", "Prev", key_display="<"),
        Binding("greater_than_sign,full_stop", "cmd('next')", "Next", key_display=">"),
        Binding("left", "cmd('back10')", "-10s", key_display="←"),
        Binding("right", "cmd('fwd10')", "+10s", key_display="→"),
        Binding("left_square_bracket", "cmd('quieter')", "Vol -", key_display="["),
        Binding("right_square_bracket", "cmd('louder')", "Vol +", key_display="]"),
        Binding("s", "cmd('shuffle')", "Shuffle"),
        Binding("r", "cmd('repeat')", "Repeat"),
        Binding("o", "open_app", "Open Spotify"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.np: NowPlaying | None = None
        self.art: list[list[str | None]] | None = None
        self.art_key: tuple[str, int] | None = None
        self.size_px = 24  # sleeve size in pixels (square)
        self.started = time.monotonic()
        self.spin = 0.0  # record angle, radians
        self.frame = 0
        self.rng = random.Random(7)
        self.levels: list[float] = []
        self.peaks: list[float] = []

    def compose(self) -> ComposeResult:
        yield Static(id="sp-head")
        with Vertical(id="sp-stage"):  # art and track, centred in whatever the equalizer leaves
            with Horizontal(id="sp-top"):
                yield Static(id="sp-scene")
                yield Static(id="sp-info")
        yield Static(id="sp-eq")
        yield Footer()

    def on_mount(self) -> None:
        self._pick_size()
        try:  # start from what the dashboard last saw, so the first frame isn't empty
            self.np = self.app.screen_stack[0].query_one("Vitals").spotify
        except Exception:
            self.np = None
        self.poll()
        self.set_interval(1, self.poll)
        self.set_interval(1 / FPS, self.tick)
        self.tick()

    def on_resize(self) -> None:
        old = self.size_px
        self._pick_size()
        if self.size_px != old and self.np and self.np.art_url:
            self.run_worker(self._fetch_art(self.np.art_url), group="sp-art", exclusive=True)

    def _pick_size(self) -> None:
        w, h = self.size.width or 120, self.size.height or 40
        rows = h - 5 - self._eq_rows()  # what's left after the head, footer and equalizer
        self.size_px = (48 if rows >= 28 and w >= 170 else 32 if rows >= 18 and w >= 110
                        else 24 if rows >= 13 and w >= 80 else 16)

    def _eq_rows(self) -> int:
        return max(min((self.size.height or 40) // 3, 26), 4)

    # data ---------------------------------------------------------------------

    def poll(self) -> None:
        self.run_worker(self._poll(), group="sp-poll", exclusive=True)

    async def _poll(self) -> None:
        sp = self.app.spotify
        if not sp:
            return
        np = await sp.now()
        self.np = np
        if np.art_url and self.art_key != (np.art_url, self.size_px):
            await self._fetch_art(np.art_url)

    async def _fetch_art(self, url: str) -> None:
        size = self.size_px
        grid = await self.app.spotify.art(url, size=ART_RES[size])
        if grid:
            k = size // len(grid)
            grid = [[c for c in row for _ in range(k)] for row in grid for _ in range(k)]
        self.art, self.art_key = grid, (url, size)

    async def action_cmd(self, what: str) -> None:
        np = self.np
        if what == "playpause" and np and np.has_track:  # flip now so the record and bars react at once
            np.position, np.fetched = np.position_now(), time.monotonic()
            np.state = "paused" if np.state == "playing" else "playing"
        await self.app.action_spotify(what)
        self.set_timer(0.35, self.poll)

    async def action_open_app(self) -> None:
        if self.app.spotify:
            await self.app.spotify.open_app()

    def action_back(self) -> None:
        self.dismiss()

    # animation ----------------------------------------------------------------

    def tick(self) -> None:
        now = time.monotonic()
        t = now - self.started
        playing = bool(self.np and self.np.state == "playing")
        if playing:
            self.spin = (self.spin + 2 * math.pi * (33.3 / 60) / FPS) % (2 * math.pi)
        self.frame += 1
        beat = (t * BPM / 60) % 1.0
        self._head(playing)
        self.query_one("#sp-scene", Static).update(self._scene(t))
        self.query_one("#sp-info", Static).update(self._info(t, beat, playing))
        self.query_one("#sp-eq", Static).update(self._eq(t, beat, playing))

    def _head(self, playing: bool) -> None:
        head = Text(no_wrap=True)
        head.append(" ♪ NOW PLAYING ", Style(color=px.VOID, bgcolor=px.PINK, bold=True))
        np = self.np
        state = np.state if np else "…"
        label = {"playing": "▶ playing", "paused": "❚❚ paused", "stopped": "■ stopped", "closed": "spotify is closed",
                 "missing": "spotify not installed", "denied": "needs permission"}.get(state, state)
        head.append(f"  {label}", Style(color=px.CYAN if playing else px.YELLOW, bold=True))
        if np and np.has_track:
            head.append(f"   vol {np.volume}", Style(color=px.DIM))
            if np.shuffle:
                head.append("  ⤮ shuffle", Style(color=px.CYAN))
            if np.repeat:
                head.append("  ↻ repeat", Style(color=px.CYAN))
        self.query_one("#sp-head", Static).update(head)

    def _sleeve(self, s: int) -> list[list[str | None]]:
        if self.art and self.np and self.np.has_track and len(self.art) == s:
            return [row[:] for row in self.art]
        # no art: a gradient sleeve with a big pixel note
        grid = [[_mix(px.DEEP, px.gradient_at((x + y) / (2 * s), [px.PURPLE, px.BLUE, px.PINK]), 0.55)
                 for x in range(s)] for y in range(s)]
        k = max(s // 12, 1)
        ox = oy = (s - 8 * k) // 2
        for y, row in enumerate(NOTE):
            for x, ch in enumerate(row):
                if ch != ".":
                    for dy in range(k):
                        for dx in range(k):
                            grid[oy + y * k + dy][ox + x * k + dx] = px.INK
        return grid

    def _label_color(self, sleeve: list[list[str | None]]) -> str:
        cols = [c for row in sleeve for c in row if c]
        if not cols:
            return px.PINK
        r = sum(int(c[1:3], 16) for c in cols) // len(cols)
        g = sum(int(c[3:5], 16) for c in cols) // len(cols)
        b = sum(int(c[5:7], 16) for c in cols) // len(cols)
        return _mix(f"#{r:02x}{g:02x}{b:02x}", px.INK, 0.2)

    def _scene(self, t: float) -> Text:
        """Sleeve with the record sliding out to its right, spinning, with a turning glint."""
        s = self.size_px
        sleeve = self._sleeve(s)
        label = self._label_color(sleeve)
        width = s + s // 2 + 1
        grid: list[list[str | None]] = [[None] * width for _ in range(s)]
        r = s / 2 - 0.5
        cy = s / 2 - 0.5
        cx = s / 2 - 0.5 + (s / 2) * _ease(t / INTRO)  # slides from behind the sleeve
        for y in range(s):
            for x in range(width):
                dx, dy = x - cx, y - cy
                d = math.hypot(dx, dy)
                if d > r:
                    continue
                if d < max(r * 0.07, 0.6):
                    c = px.VOID  # spindle hole
                elif d < r * 0.34:
                    c = label
                    ang = math.atan2(dy, dx)
                    if abs(((ang - self.spin + math.pi) % (2 * math.pi)) - math.pi) < 0.5 and d > r * 0.16:
                        c = _mix(label, px.VOID, 0.45)  # a mark on the label so you can see it turn
                elif d > r - 1:
                    c = "#3b2f52"  # rim
                else:
                    c = "#2a2236" if int(d * 1.4) % 3 == 0 else "#15101d"  # grooves
                    ang = math.atan2(dy, dx)
                    off = abs(((ang - self.spin) % math.pi) - math.pi / 2)  # two opposite glints
                    if off > math.pi / 2 - 0.4:
                        c = _mix(c, "#9a86c4", (off - (math.pi / 2 - 0.4)) / 0.4 * 0.6)
                grid[y][x] = c
        for y in range(s):
            for x in range(s):
                grid[y][x] = sleeve[y][x]
            grid[y][s] = px.SHADOW if grid[y][s] is None else _mix(grid[y][s], px.VOID, 0.5)  # sleeve edge shadow
        return px.render(grid)

    def _info(self, t: float, beat: float, playing: bool) -> Text:
        np = self.np
        avail = max(self.size.width - (self.size_px + self.size_px // 2 + 1) - 10, 20)
        out = Text(no_wrap=True)
        if not np or not np.has_track:
            state = np.state if np else ""
            say = {
                "closed": "Spotify is closed. p opens it and starts playing.",
                "stopped": "Nothing playing. p starts it.",
                "missing": "The Spotify app isn't installed on this Mac.",
            }.get(state, (np.message if np else "") or "asking Spotify…")
            out.append("SPOTIFY\n", Style(color=px.HOTPINK, bold=True))
            out.append(say + "\n\n", Style(color=px.YELLOW if state in ("denied", "error") else px.DIM))
            out.append_text(self._dancers(t, beat, False))
            return out
        out.append_text(self._marquee(np.title, avail, Style(color=px.INK, bold=True)))
        out.append("\n")
        out.append(np.artist[:avail] + "\n", Style(color=px.CYAN, bold=True))
        out.append((np.album or "")[:avail] + "\n\n", Style(color=px.DIM))
        pos = np.position_now()
        clock = f"  {fmt_time(pos)} / {fmt_time(np.duration)}"
        pct = pos / np.duration * 100 if np.duration else 0
        out.append_text(px.bar(pct, max(min(avail, 72) - len(clock), 8), [px.PURPLE, px.HOTPINK, px.CYAN]))
        out.append(clock + "\n", Style(color=px.INK))
        vol = Text(no_wrap=True)
        vol.append("vol ", Style(color=px.DIM))
        vol.append_text(px.bar(np.volume, 10, [px.BLUE, px.CYAN]))
        vol.append(f" {np.volume}", Style(color=px.DIM))
        out.append_text(vol)
        out.append("\n\n")
        out.append_text(self._dancers(t, beat, playing))
        return out

    def _marquee(self, text: str, width: int, style: Style) -> Text:
        if len(text) <= width:
            return Text(text, style, no_wrap=True)
        loop = text + "   ✦   "
        off = (self.frame // 2) % len(loop)
        return Text((loop * 2)[off: off + width], style, no_wrap=True)

    def _dancers(self, t: float, beat: float, playing: bool) -> Text:
        """Invader, ghost, invader: they swap frames and hop on every beat while the music plays."""
        sprites = [px.INVADER, px.GHOST, px.INVADER]
        h = 10  # pixel rows: 8-row sprite plus 2 for the hop
        gap = 3
        width = sum(len(s[0][0]) for s in sprites) + gap * (len(sprites) - 1)
        grid: list[list[str | None]] = [[None] * width for _ in range(h)]
        beat_n = int(t * BPM / 60)
        x0 = 0
        for i, frames in enumerate(sprites):
            fr = frames[(beat_n + i) % 2] if playing else frames[0]
            hop = 0 if not playing else (2 if (beat_n + i) % 2 == 0 and beat < 0.35 else 0)
            top = h - len(fr) - hop
            for y, row in enumerate(fr):
                for x, ch in enumerate(row):
                    c = px.SPRITE_COLORS.get(ch)
                    if c:
                        grid[top + y][x0 + x] = c if playing else _mix(c, px.VOID, 0.55)
            x0 += len(fr[0]) + gap
        if self.size_px >= 48:  # big window: big dancers, so they don't look lost next to the cover
            grid = [[c for c in row for _ in range(2)] for row in grid for _ in range(2)]
        out = px.render(grid)
        if not playing and self.np and self.np.has_track:
            out = Text("  z z z\n", Style(color=px.DIM)) + out
        return out

    def _eq(self, t: float, beat: float, playing: bool) -> Text:
        cols = max(self.size.width - 4, 10)
        rows = self._eq_rows()
        n = max(cols // 3, 4)
        if len(self.levels) != n:
            self.levels, self.peaks = [0.0] * n, [0.0] * n
        intro = _ease(t / INTRO)
        kick = math.exp(-beat * 6)
        for i in range(n):
            f = i / (n - 1)
            if playing:
                wave = math.sin(t * 2.1 + i * 0.45) ** 2 * math.sin(t * 0.7 - i * 0.2) ** 2
                jitter = self.rng.random() ** 2  # mostly small, now and then a spike
                target = 0.08 + wave * 0.35 + kick * (1 - f) ** 1.5 * 0.6 + jitter * 0.45 * (0.3 + f)
                target = min(target, 1.0) * intro
            else:
                target = 0.0
            cur = self.levels[i]
            cur += (target - cur) * (0.55 if target > cur else 0.18)
            self.levels[i] = cur
            self.peaks[i] = max(cur, self.peaks[i] - 0.025)
        hpx = rows * 2
        grid: list[list[str | None]] = [[None] * (n * 3) for _ in range(hpx)]
        for i in range(n):
            lit = round(self.levels[i] * hpx)
            peak = min(round(self.peaks[i] * hpx), hpx - 1)
            for k in range(lit):
                c = px.gradient_at(k / max(hpx - 1, 1), EQ_STOPS)
                y = hpx - 1 - k
                grid[y][i * 3] = grid[y][i * 3 + 1] = c
            if peak > 0 and peak >= lit:
                y = hpx - 1 - peak
                grid[y][i * 3] = grid[y][i * 3 + 1] = px.YELLOW
        for i in range(n * 3):  # floor line so the stage is there even when it's quiet
            if grid[hpx - 1][i] is None:
                grid[hpx - 1][i] = px.SHADOW
        return px.render(grid)
