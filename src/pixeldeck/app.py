"""PIXELDECK: an 8-bit productivity console."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections import deque
from datetime import datetime
from typing import Awaitable, Callable

from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Middle, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from . import pixel as px
from .config import get_secret, load_config
from .sources.base import Feed, NotConfigured
from .sources.chat import Linear
from .sources.slackclient import SlackClient
from .sources.github import GitHub
from .sources.mail import Mail, Outlook
from .detail import DetailScreen
from .setup import SERVICES, FormScreen, OutlookScreen, SetupMenu
from .sources.releases import Releases
from .sources.system import System, docker_summary, repo_status
from .updater import Updater, restart

THEME = Theme(
    name="pixeldeck",
    primary=px.PURPLE,
    secondary=px.CYAN,
    accent=px.HOTPINK,
    warning=px.YELLOW,
    error=px.RED,
    success=px.CYAN,
    foreground=px.INK,
    background=px.VOID,
    surface=px.DEEP,
    panel=px.PANEL,
    dark=True,
    variables={  # Markdown on detail pages (Linear tickets, PRs, issues)
        "markdown-h1-color": px.HOTPINK, "markdown-h1-text-style": "bold", "markdown-h1-background": "transparent",
        "markdown-h2-color": px.CYAN, "markdown-h2-text-style": "bold", "markdown-h2-background": "transparent",
        "markdown-h3-color": px.VIOLET, "markdown-h3-text-style": "bold",
        "markdown-h4-color": px.INK, "markdown-h4-text-style": "bold underline",
        "markdown-h5-color": px.INK, "markdown-h6-color": px.DIM,
        "link-color": px.CYAN, "link-style": "underline", "link-color-hover": px.HOTPINK,
        "link-background-hover": px.SHADOW,
    },
)

CSS = f"""
Screen {{ background: {px.VOID}; }}

#header {{ height: 4; background: {px.DEEP}; padding: 0 1; }}
#sprite {{ width: 13; padding: 0 1; }}
#logo {{ width: auto; padding: 0 2 0 1; }}
#hud {{ width: 1fr; content-align: center middle; }}
#clock {{ width: auto; padding: 0 1; }}
#divider {{ height: 1; }}

#main {{ height: 1fr; padding: 0 1; }}
.col {{ width: 3fr; height: 1fr; }}
#col-plan {{ width: 2fr; }}
.col.-quiet {{ width: 1fr; }}
#col-comms, #col-plan {{ margin-left: 1; }}

/* fit: short action lists hug their content; fill: streams take what's left */
.tile.fit {{ height: auto; max-height: 45%; }}
#mine.fit {{ max-height: 35%; }}
#ci.fit {{ max-height: 40%; }}
/* GitHub inbox is a short scroller; RELEASES below it takes the rest of the column */
#notifs.fill {{ height: 10; min-height: 6; }}
#releases.fit {{ height: 1fr; min-height: 8; max-height: 100%; }}
OptionList.tile#releases > .option-list--separator {{ color: {px.PURPLE} 70%; }}
/* Slack gets the bigger share of the comms column */
#mail.fill {{ height: 2fr; }}
#slack.fill {{ height: 3fr; }}
.tile.fill {{ height: 1fr; min-height: 6; }}
.tile.-slim {{ height: auto; min-height: 0; max-height: 6; }}
/* a fit tile grows when every stream tile in its column has collapsed */
#reviews.-grow, #mine.-grow, #agenda.-grow, #ci.-grow, #releases.-grow {{ height: 1fr; max-height: 100%; }}

#system {{ height: 6; margin: 0 1; }}

.-narrow #main {{ layout: vertical; overflow-y: auto; }}
.-narrow .col {{ width: 1fr; height: auto; margin-left: 0; }}
.-narrow .tile.fill {{ height: 14; }}
.-narrow #slack.fill {{ height: 22; }}
.-narrow #system {{ height: 8; }}

.tile {{
    background: {px.DEEP};
    border: panel {px.PURPLE};
    border-title-color: {px.VOID};
    border-title-style: bold;
    border-subtitle-color: {px.DIM};
    padding: 0 0;
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-color: {px.PURPLE};
    scrollbar-color-hover: {px.HOTPINK};
    scrollbar-background: {px.DEEP};
}}
.tile.blue {{ border: panel {px.BLUE}; }}
.tile.pink {{ border: panel {px.PINK}; }}
.tile.cyan {{ border: panel {px.CYAN}; }}
.tile:focus {{ border: panel {px.HOTPINK}; background: {px.PANEL}; }}
.tile.error {{ border-subtitle-color: {px.RED}; }}

OptionList.tile > .option-list--option {{ padding: 0 1; }}
OptionList.tile > .option-list--option-highlighted {{ background: {px.SHADOW}; }}
OptionList.tile:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
OptionList.tile > .option-list--option-hover {{ background: {px.PANEL}; }}
OptionList.tile > .option-list--option-disabled {{ color: {px.DIM}; }}
OptionList.tile > .option-list--separator {{ color: {px.SHADOW}; }}

Footer {{ background: {px.DEEP}; }}
Footer > .footer--key {{ background: {px.PINK}; color: {px.VOID}; }}
Footer FooterKey .footer-key--key {{ background: {px.PINK}; color: {px.VOID}; text-style: bold; }}
Footer FooterKey .footer-key--description {{ color: {px.INK}; }}

Toast {{ background: {px.PANEL}; border-left: outer {px.HOTPINK}; }}

SplashScreen {{ background: {px.VOID}; align: center middle; }}
#splash {{ width: auto; height: auto; text-align: center; }}
"""


# ── Header widgets ───────────────────────────────────────────────────────────


class Sprite(Static):
    def __init__(self, frames: list[list[str]], fps: float = 2, **kw) -> None:
        super().__init__(**kw)
        self.frames = [px.render(px.sprite_grid(f)) for f in frames]
        self.i = 0
        self.fps = fps

    def on_mount(self) -> None:
        self.update(self.frames[0])
        self.set_interval(1 / self.fps, self._tick)

    def _tick(self) -> None:
        self.i = (self.i + 1) % len(self.frames)
        self.update(self.frames[self.i])


class Logo(Static):
    def on_mount(self) -> None:
        self.shift = 0.0
        self.player = "…"
        self.version = ""
        self._draw()
        self.set_interval(0.12, self._tick)

    def _tick(self) -> None:
        self.shift = (self.shift + 0.015) % 1.0
        self._draw()

    def _draw(self) -> None:
        logo = px.render(px.text_grid("PIXELDECK", shift=self.shift))
        logo.append("\n")
        logo.append(" ▸ P1 ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        logo.append(f" {self.player} ", Style(color=px.INK, bold=True))
        logo.append("PRODUCTIVITY CONSOLE", Style(color=px.DIM))
        if self.version:
            logo.append(f"  v{self.version}", Style(color=px.DIM))
        self.update(logo)


class Clock(Static):
    def on_mount(self) -> None:
        self._draw()
        self.set_interval(1, self._draw)

    def _draw(self) -> None:
        now = datetime.now()
        colon = ":" if now.second % 2 == 0 else " "
        big = px.render(px.text_grid(f"{now:%H}{colon}{now:%M}", shadow=True, stops=[px.CYAN, px.PURPLE, px.HOTPINK]))
        big.append("\n")
        big.append(f"{now:%a %d %b}".upper(), Style(color=px.CYAN, bold=True))
        big.append(f"  :{now:%S}", Style(color=px.HOTPINK))
        self.update(big)


class Hud(Static):
    """Headline counters as 8-bit chips plus sync state."""

    CHIPS = [
        ("reviews", "◆", "REVIEWS", px.PINK),
        ("mail", "✉", "UNREAD", px.BLUE),
        ("slack", "✦", "PINGS", px.PURPLE),
        ("linear", "▶", "ACTIVE", px.CYAN),
        ("releases", "▲", "TO SHIP", px.HOTPINK),
        ("ci", "✖", "CI FAIL", px.RED),
        ("update", "⬆", "UPDATE · U", px.YELLOW),
    ]

    def on_mount(self) -> None:
        self.counts: dict[str, int | None] = {}
        self.synced: float | None = None
        self.busy = 0
        self.set_interval(1, self.refresh_hud)
        self.refresh_hud()

    def set_count(self, key: str, value: int | None) -> None:
        self.counts[key] = value
        self.synced = time.monotonic()
        self.refresh_hud()

    def refresh_hud(self) -> None:
        line = Text(no_wrap=True, justify="center")
        for key, icon, label, color in self.CHIPS:
            val = self.counts.get(key)
            if val is None:
                continue
            if key in ("ci", "update") and not val:
                continue
            hot = val > 0
            line.append(f" {icon} {val} ", Style(color=px.VOID, bgcolor=color if hot else px.SHADOW, bold=True))
            line.append(f" {label}  ", Style(color=color if hot else px.DIM, bold=hot))
        if not line.plain:
            line.append("LOADING", Style(color=px.DIM))
        status = Text(justify="center", no_wrap=True)
        if self.busy:
            frame = "▖▘▝▗"[int(time.monotonic() * 4) % 4]
            status.append(f"{frame} SYNCING", Style(color=px.YELLOW))
        elif self.synced:
            age = int(time.monotonic() - self.synced)
            status.append(f"● SYNCED {age}s AGO", Style(color=px.CYAN))
        status.append("   ⏎ VIEW · 66 PAGE · O BROWSER · R SYNC · S SETTINGS", Style(color=px.DIM))
        self.update(Text("\n").join([Text(""), line, status]))


class Divider(Widget):
    def render(self) -> Text:
        w = self.size.width
        out = Text(no_wrap=True)
        for x in range(w):
            ch = "▀" if x % 2 == 0 else "▄"
            out.append(ch, Style(color=px.gradient_at(x / max(w - 1, 1)), bgcolor=px.VOID))
        return out


# ── Tiles ────────────────────────────────────────────────────────────────────


class Tile(OptionList):
    def __init__(self, key: str, num: int, title: str, color: str = "", size: str = "fill", **kw) -> None:
        super().__init__(id=key, classes=f"tile {color} {size}", **kw)
        self.key, self.num, self.label = key, num, title
        self.urls: dict[str, str] = {}
        self.updated: float | None = None
        self.feed: Feed | None = None
        self._width = 0
        self.flashing: dict[str, object] = {}
        self.glowing: dict = {}  # deploys in progress
        self._pulse_timer = None
        self._set_title(None)

    def on_mount(self) -> None:
        self.add_option(Option(Text("  ▒▒▒ LOADING ▒▒▒", style=px.DIM), disabled=True))
        self.set_interval(5, self._age)

    def _set_title(self, count: int | None) -> None:
        tail = f" ×{count}" if count is not None else ""
        self.border_title = f" {self.num} ▸ {self.label}{tail} "

    def _age(self) -> None:
        if self.flashing:
            return  # the NEW counter owns the subtitle while something is pulsing
        if self.updated and "error" not in self.classes:
            secs = int(time.monotonic() - self.updated)
            self.border_subtitle = f"{secs}s" if secs < 60 else f"{secs // 60}m"

    def on_resize(self) -> None:
        if self.feed and self._line_width() != self._width:
            self._build(self.feed)

    def on_focus(self) -> None:
        if self.highlighted is None:
            for i, opt in enumerate(self.options):
                if not opt.disabled:
                    self.highlighted = i
                    break

    def _line_width(self) -> int:
        # border (2) + option padding (2) + scrollbar (1), so a scrollbar
        # appearing later never pushes a word onto a second line.
        return max(self.size.width - 6, 10)

    def set_feed(self, feed: Feed) -> None:
        self.feed = feed
        self._build(feed)
        self.updated = time.monotonic()
        self._age()

    def _build(self, feed: Feed) -> None:
        width = self._width = self._line_width()
        self.remove_class("error")
        self._set_title(feed.count)
        self.urls.clear()
        highlighted = self.highlighted
        options: list[Option | None] = []
        if feed.note:
            options.append(Option(Text(f"⚙ {feed.note}", style=Style(color=px.YELLOW)), disabled=True))
        self.flashing.clear()
        self.glowing.clear()
        for i, item in enumerate(feed.items):
            oid = f"{self.key}-{i}"
            if item.url:
                self.urls[oid] = item.url
            if item.flash:
                self.flashing[oid] = item
            elif getattr(item, "pulse", False):
                self.glowing[oid] = item
            options.append(Option(self._render_item(item, width, self._phase() if item.flash else None,
                                                    self._phase() if oid in self.glowing else None), id=oid))
            if getattr(item, "rule_after", False) and i < len(feed.items) - 1:
                options.append(None)  # thin separator line
        if not feed.items and not feed.note:
            options.append(Option(Text("  ★ ALL CLEAR ★", style=Style(color=px.CYAN, bold=True)), disabled=True))
        self.set_options(options)
        if highlighted is not None and highlighted < self.option_count:
            self.highlighted = highlighted
        self._slim(not feed.items)
        new = len(self.flashing)
        if new:
            self.border_subtitle = f"✦ {new} NEW"
        elif str(self.border_subtitle or "").startswith("✦"):
            self.border_subtitle = ""
        if (self.flashing or self.glowing) and self._pulse_timer is None:
            self._pulse_timer = self.set_interval(1 / 12, self._pulse)
        elif not (self.flashing or self.glowing) and self._pulse_timer is not None:
            self._pulse_timer.stop()
            self._pulse_timer = None

    # new-item pulse ----------------------------------------------------------

    @staticmethod
    def _phase() -> float:
        """0 → 1 → 0 every 1.6s: a slow fade in, fade out."""
        import math

        return (math.sin(time.monotonic() * 2 * math.pi / 1.6 - math.pi / 2) + 1) / 2

    def _render_item(self, item, width: int, phase: float | None = None, glow: float | None = None) -> Text:
        """phase: the NEW pulse. glow: marked words breathing green ↔ pink (a deploy in progress)."""
        top = Text(no_wrap=True)
        if phase is not None:
            glow = px._lerp(px.SHADOW, px.HOTPINK, phase)
            top.append(" NEW ", Style(color=px._lerp(px.DIM, px.VOID, phase), bgcolor=glow, bold=True))
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

    def _pulse(self) -> None:
        phase = self._phase()
        for oid, item in list(self.flashing.items()):
            try:
                self.replace_option_prompt(oid, self._render_item(item, self._width, phase))
            except Exception:
                self.flashing.pop(oid, None)
        for oid, item in list(self.glowing.items()):
            try:
                self.replace_option_prompt(oid, self._render_item(item, self._width, glow=phase))
            except Exception:
                self.glowing.pop(oid, None)

    def acknowledge(self, item) -> None:
        """Re-render one item after it was viewed (it's muted now)."""
        for oid, it in list(self.flashing.items()):
            if it is item:
                self.flashing.pop(oid)
        if self.feed and item in self.feed.items:
            oid = f"{self.key}-{self.feed.items.index(item)}"
            try:
                self.replace_option_prompt(oid, self._render_item(item, self._width))
            except Exception:
                pass
        new = len(self.flashing)
        self.border_subtitle = f"✦ {new} NEW" if new else ""

    def set_problem(self, message: str, setup: bool) -> None:
        self.feed = None
        self._set_title(None)
        self.urls.clear()
        icon, color = ("⚙ SETUP", px.YELLOW) if setup else ("⚠ ERROR", px.RED)
        msg = Text()
        msg.append(f" {icon} ", Style(color=px.VOID, bgcolor=color, bold=True))
        msg.append(f" {message}", Style(color=px.INK if setup else px.RED))
        if setup:
            msg.append("  ⏎ connect", Style(color=px.HOTPINK, bold=True))
        self.set_options([Option(msg, id=f"{self.key}-setup" if setup else None, disabled=not setup)])
        if not setup:
            self.add_class("error")
            self.border_subtitle = "⚠ retrying"
        self._slim(True)

    def _slim(self, slim: bool) -> None:
        """Collapse to a strip when there's nothing to list, and let the column re-balance."""
        if slim != self.has_class("-slim"):
            self.set_class(slim, "-slim")
            relayout = getattr(self.app, "relayout", None)
            if relayout:
                self.app.call_later(relayout)

    def current_url(self) -> str | None:
        opt = self.highlighted_option
        return self.urls.get(opt.id) if opt and opt.id else None


class Vitals(Widget):
    """System meters with home-made gradient sparklines."""

    DEFAULT_CLASSES = "tile cyan"

    def __init__(self, num: int, **kw) -> None:
        super().__init__(id="system", **kw)
        self.num = num
        self.border_title = " ▸ SYSTEM "
        self.cpu: deque[float] = deque(maxlen=200)
        self.net: deque[float] = deque(maxlen=200)
        self.v = None
        self.docker: tuple[int, int, list[str]] | None = None
        self.repos: Feed | None = None
        self.claude_usage = None
        self.meters: list[str] = ["cpu", "mem", "net", "bat"]
        self.warn_pct = 80
        self.claude_limits = None
        self.claude_wrapped = False
        self.llm: dict | None = None  # LM Studio status for the reply-assist model
        self.llm_enabled = False
        self.llm_ms: float | None = None
        self.llm_loading: tuple[str, float] | None = None
        self.spotify = None  # sources.spotify.NowPlaying; None hides the block
        self.spotify_art: list[list[str | None]] | None = None

    def push(self, vitals) -> None:
        self.v = vitals
        self.cpu.append(vitals.cpu)
        self.net.append(vitals.net_down + vitals.net_up)
        self.refresh()

    @staticmethod
    def _spark(values: deque[float], width: int, ceiling: float | None = None) -> Text:
        blocks = " ▁▂▃▄▅▆▇█"
        data = list(values)[-width:]
        top = ceiling or max(data or [1]) or 1
        out = Text(no_wrap=True)
        out.append(" " * (width - len(data)))
        for v in data:
            level = min(v / top, 1.0)
            out.append(blocks[max(round(level * 8), 1 if v > 0 else 0)], Style(color=px.gradient_at(level, [px.PURPLE, px.CYAN, px.HOTPINK])))
        return out

    @staticmethod
    def _rate(b: float) -> str:
        for unit in ("B", "K", "M", "G"):
            if b < 1024:
                return f"{b:5.1f}{unit}"
            b /= 1024
        return f"{b:.1f}T"

    @staticmethod
    def _spark2(values: deque[float], width: int, ceiling: float | None = None) -> tuple[Text, Text]:
        """Two-row sparkline: 16 levels of height."""
        blocks = " ▁▂▃▄▅▆▇█"
        data = list(values)[-width:]
        top_v = ceiling or max(data or [1]) or 1
        upper, lower = Text(no_wrap=True), Text(no_wrap=True)
        pad = " " * (width - len(data))
        upper.append(pad)
        lower.append(pad)
        for v in data:
            level = min(v / top_v, 1.0)
            steps = max(round(level * 16), 1 if v > 0 else 0)
            style = Style(color=px.gradient_at(level, [px.PURPLE, px.CYAN, px.HOTPINK]))
            lower.append(blocks[min(steps, 8)], style)
            upper.append(blocks[max(steps - 8, 0)], style)
        return upper, lower

    def render(self) -> Text:
        w = max(self.size.width - 1, 20)  # size is already inside the border
        rows = max(self.size.height, 1)
        if not self.v:
            return Text("  ▒▒▒ LOADING ▒▒▒", style=px.DIM)
        v = self.v
        label = Style(color=px.CYAN, bold=True)

        # left: meters
        left_w = 36
        meter_w = 14

        def meter(name: str, pct: float, extra: str) -> Text:
            t = Text(no_wrap=True)
            t.append(f" {name:<4}", label)
            t.append_text(px.bar(pct, meter_w))
            t.append(f" {pct:3.0f}%", Style(color=px.INK, bold=True))
            t.append(f" {extra}", Style(color=px.DIM))
            return t

        show = set(self.meters)
        left = []
        if "cpu" in show:
            left.append(meter("CPU", v.cpu, f"ld {v.load:.1f}"))
        if "mem" in show:
            left.append(meter("MEM", v.mem, f"{v.mem_used_gb:.0f}/{v.mem_total_gb:.0f}G"))
        if "net" in show:
            net = Text(no_wrap=True)
            net.append(" NET ", label)
            net.append(f"↓{self._rate(v.net_down)}/s ↑{self._rate(v.net_up)}/s", Style(color=px.INK))
            left.append(net)
        if "bat" in show and v.battery is not None:
            left.append(meter("BAT", v.battery, "⚡ ac" if v.charging else "bat"))

        # right: docker + repos, one line each
        right_w = min(64, max(w // 3, 30))
        right: list[Text] = []
        if self.docker:
            up, bad, names = self.docker
            t = Text(no_wrap=True)
            t.append("DOCK ", label)
            t.append(f"{up} up", Style(color=px.CYAN))
            if bad:
                t.append(f" · {bad} unhealthy: {', '.join(names)}", Style(color=px.RED, bold=True))
            right.append(t)
        if self.repos:
            for item in self.repos.items:
                t = Text(no_wrap=True)
                t.append(f"{item.icon} ", Style(color=px.TONE.get(item.tone, px.INK)))
                t.append(item.title, Style(color=px.INK, bold=True))
                t.append(f"  {item.meta}", Style(color=px.DIM))
                right.append(t)
            if self.repos.note:
                right.append(Text(f"⚙ {self.repos.note}", Style(color=px.YELLOW)))

        # claude: plan limits (from the status line) + token usage (from session logs)
        claude_w = 46 if w >= 120 else 0
        claude: list[Text] = []
        if claude_w:
            from .sources.claude import fmt_tokens, until

            lab = Style(color=px.HOTPINK, bold=True)
            lim = self.claude_limits
            if lim and lim.windows:
                for i, win in enumerate(lim.windows[:2]):
                    t = Text(no_wrap=True)
                    t.append("CLAUDE " if i == 0 else "       ", lab)
                    t.append(f"{win.name:<3} ", Style(color=px.INK, bold=True))
                    hot = win.pct >= self.warn_pct
                    t.append_text(px.bar(win.pct, 12, [px.YELLOW, px.RED] if hot else px.GRADIENT))
                    t.append(f" {win.pct:3.0f}%", Style(color=px.RED if hot else px.INK, bold=True))
                    if win.resets:
                        t.append(f" ↻{until(win.resets)}", Style(color=px.DIM))
                    claude.append(t)
                if lim.age > 3 * 3600:
                    claude[-1].append(f" ·{int(lim.age // 3600)}h old", Style(color=px.YELLOW))
            else:
                t = Text(no_wrap=True)
                t.append("CLAUDE ", lab)
                t.append("limits off · s → CLAUDE" if not self.claude_wrapped else "limits: waiting for a session",
                         Style(color=px.YELLOW))
                claude += [t, Text()]
            u = self.claude_usage
            if u:
                t = Text(no_wrap=True)
                t.append("  TOK  ", Style(color=px.CYAN, bold=True))
                t.append(f"5h {fmt_tokens(u.five_hour)}", Style(color=px.INK, bold=True))
                t.append(f" · {u.msgs_5h} msgs", Style(color=px.DIM))
                claude.append(t)
                t = Text(no_wrap=True)
                t.append(f"       today {fmt_tokens(u.today)} · 7d {fmt_tokens(u.week)}", Style(color=px.DIM))
                t.append(f" · {fmt_tokens(u.output_week)} out", Style(color=px.DIM))
                claude.append(t)

        # llm: the local model behind reply suggestions
        llm_w = 32 if w >= 150 else 0
        llm: list[Text] = []
        if llm_w:
            lab = Style(color=px.HOTPINK, bold=True)
            st = self.llm or {}
            entry = st.get("entry") or {}
            name = (entry.get("name") or st.get("model") or "auto")
            name = name.replace(" Instruct", "").replace(" 2512", "")
            head = Text(no_wrap=True)
            head.append("LLM ", lab)
            if self.llm_loading:
                what, since = self.llm_loading
                head.append(f"◷ {what[:20]}", Style(color=px.YELLOW, bold=True))
                state = Text(f"    loading… {time.monotonic() - since:.0f}s", Style(color=px.YELLOW))
            elif not self.llm:
                head.append("…", Style(color=px.DIM))
                state = Text("")
            elif not st.get("up"):
                head.append("✖ LM Studio off", Style(color=px.RED, bold=True))
                state = Text("    start its server", Style(color=px.DIM))
            elif st.get("current_loaded"):
                head.append(f"● {name[:24]}", Style(color=px.CYAN, bold=True))
                bits = [entry.get("params") or "", str(entry.get("quant") or ""), f"{entry.get('size_gb', 0):.1f}G"]
                state = Text("    " + " · ".join(b for b in bits if b), Style(color=px.DIM))
            else:
                head.append(f"○ {name[:24]}", Style(color=px.YELLOW, bold=True))
                state = Text("    not loaded · l to load", Style(color=px.YELLOW))
            llm += [head, state]
            info = Text(no_wrap=True)
            info.append("    replies ", Style(color=px.DIM))
            info.append("on" if self.llm_enabled else "off", Style(color=px.CYAN if self.llm_enabled else px.DIM, bold=True))
            if self.llm_ms:
                info.append(f" · last {self.llm_ms / 1000:.1f}s", Style(color=px.DIM))
            others = [m for m in st.get("loaded") or [] if m.get("key") != st.get("model")]
            if others:
                info.append(f" · +{len(others)} loaded", Style(color=px.YELLOW))
            llm.append(info)
            llm.append(Text("    l pick / load model", Style(color=px.DIM)))

        # spotify: what's playing. It outranks the LLM panel and the sparklines when space is short.
        spot_w = 46 if self.spotify is not None else 0
        spare = w - left_w - claude_w - llm_w - right_w - 12
        if spot_w and spare < spot_w + 3 and llm_w:
            llm_w = 0
            spare = w - left_w - claude_w - right_w - 12
        if spot_w and spare < spot_w + 3:
            spot_w = 0
        spot = self._spotify_block(spot_w, rows) if spot_w else []

        # middle: CPU and NET sparklines, two rows each
        mid_w = max(spare - (spot_w + 3 if spot_w else 0), 0)
        mid: list[Text] = []
        if mid_w >= 10:
            for name, data, ceil in (("cpu", self.cpu, 100), ("net", self.net, None)):
                up_row, low_row = self._spark2(data, mid_w - 4, ceil)
                a = Text(f"{name} ", Style(color=px.DIM)); a.append_text(up_row)
                b = Text("    "); b.append_text(low_row)
                mid += [a, b]

        sep = Text(" │ ", Style(color=px.SHADOW))
        out_lines = []
        for r in range(rows):
            line = Text(no_wrap=True)
            for block, width in ((left, left_w), (claude, claude_w), (llm, llm_w), (spot, spot_w), (mid, mid_w),
                                 (right, right_w)):
                if width <= 0:
                    continue
                cell = block[r].copy() if r < len(block) else Text()
                cell.truncate(width, overflow="ellipsis", pad=True)
                if line.plain:
                    line.append_text(sep)
                line.append_text(cell)
            line.truncate(w, overflow="ellipsis")
            out_lines.append(line)
        return Text("\n", no_wrap=True).join(out_lines)

    NOTE = ["....hhh.", "....h.hh", "....h...", "....h...", "..hhh...", ".hhhh...", ".hhh....", "........"]

    def _spotify_block(self, width: int, rows: int) -> list[Text]:
        """8×8 pixel cover art (4 rows of half blocks) beside track, artist, progress and controls."""
        from .sources.spotify import fmt_time

        np = self.spotify
        art_grid = self.spotify_art if np.has_track and self.spotify_art else px.sprite_grid(self.NOTE)
        if not np.has_track:
            art_grid = [[px.SHADOW if c else None for c in row] for row in art_grid]
        art = px.render(art_grid).split("\n")
        info_w = width - 10
        lab = Style(color=px.HOTPINK, bold=True)
        info: list[Text] = []
        if np.has_track:
            playing = np.state == "playing"
            t = Text(no_wrap=True)
            t.append("▶ " if playing else "❚❚ ", Style(color=px.CYAN if playing else px.YELLOW, bold=True))
            t.append(np.title, Style(color=px.INK, bold=True))
            info.append(t)
            info.append(Text(" · ".join(x for x in (np.artist, np.album) if x), Style(color=px.DIM), no_wrap=True))
            pos = np.position_now()
            clock = f" {fmt_time(pos)}/{fmt_time(np.duration)}"
            t = Text(no_wrap=True)
            pct = pos / np.duration * 100 if np.duration else 0
            t.append_text(px.bar(pct, max(info_w - len(clock), 4), [px.PURPLE, px.HOTPINK, px.CYAN]))
            t.append(clock, Style(color=px.INK))
            info.append(t)
            t = Text(no_wrap=True)
            t.append(f"vol {np.volume}", Style(color=px.DIM))
            if np.shuffle:
                t.append(" ⤮", Style(color=px.CYAN))
            if np.repeat:
                t.append(" ↻", Style(color=px.CYAN))
            t.append("  p ▶❚❚ · < > · [ ] vol", Style(color=px.DIM))
            info.append(t)
        else:
            say = {
                "closed": "closed · p opens it and plays",
                "stopped": "nothing playing · p plays",
            }.get(np.state, np.message or np.state)
            t = Text(no_wrap=True)
            t.append("SPOTIFY", lab)
            info += [t, Text(say, Style(color=px.YELLOW if np.state in ("denied", "error") else px.DIM))]
        out = []
        for r in range(rows):
            line = art[r].copy() if r < len(art) else Text(" " * 8)
            line.append("  ")
            cell = info[r].copy() if r < len(info) else Text()
            cell.truncate(info_w, overflow="ellipsis")
            line.append_text(cell)
            out.append(line)
        return out


# ── Splash ───────────────────────────────────────────────────────────────────


class SplashScreen(Screen):
    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                yield Static(id="splash")

    def on_mount(self) -> None:
        self.tick = 0
        self._draw()
        self.set_interval(0.25, self._draw)
        self.set_timer(2.4, self._auto_close)

    def _auto_close(self) -> None:
        # Timers await what their callback returns, and a screen may not await its own dismiss().
        if self.is_current:
            self.dismiss()

    def _draw(self) -> None:
        self.tick += 1
        big = px.text_grid("PIXELDECK", shift=self.tick * 0.03)
        big = [[c for c in row for _ in (0, 1)] for row in big for _ in (0, 1)]  # 2x scale
        art = px.render(big)
        invaders = [px.sprite_grid(px.INVADER[self.tick % 2]), px.sprite_grid(px.GHOST[self.tick % 2])]
        row: px.Grid = [[] for _ in range(8)]
        for n in range(5):
            g = invaders[n % 2]
            for y in range(8):
                src = g[y] if y < len(g) else [None] * len(g[0])
                row[y] += src + [None] * 4
        out = Text(justify="center")
        out.append_text(px.render(row))
        out.append("\n\n")
        out.append_text(art)
        out.append("\n\n")
        if self.tick % 2:
            out.append("▶  PRESS START  ◀", Style(color=px.HOTPINK, bold=True))
        else:
            out.append(" ")
        out.append("\n\n")
        out.append("© 2026 PLAYER ONE · INSERT COIN", Style(color=px.DIM))
        self.query_one("#splash", Static).update(out)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.dismiss()

    def on_click(self) -> None:
        self.dismiss()


# ── App ──────────────────────────────────────────────────────────────────────


class Dashboard(Screen):
    """The board itself. Its shortcuts live on this screen, not the app, so a page pushed on top
    (agent, mail, agenda, settings…) shows only its own keys in the footer, and esc there goes back."""

    BINDINGS = [
        Binding("q", "app.quit_twice", "Quit (qq)"),
        Binding("r", "app.refresh", "Refresh"),
        Binding("o", "app.open", "Open"),
        Binding("c", "app.copy", "Copy URL"),
        Binding("s", "app.setup", "Settings"),
        Binding("u", "app.update", "Update"),
        Binding("m", "app.slack_console", "Slack"),
        Binding("l", "app.llm_models", "LLM"),
        Binding("f", "app.linear_filter", "Filter Linear"),
        Binding("slash", "app.linear_search", "Search Linear"),
        Binding("question_mark", "app.ask", "Ask"),
        Binding("a", "app.agenda", "Agenda"),
        Binding("plus", "app.add_task", "Task"),
        Binding("p", "app.spotify('playpause')", "Play"),
        Binding("n", "app.spotify_page", "Now playing"),
        *[Binding(str(n), f"app.jump({n})", show=False) for n in range(1, 10)],
        Binding("0", "app.jump(0)", show=False),
    ]


class PixelDeck(App):
    TITLE = "PIXELDECK"
    CSS = CSS
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-medium"), (160, "-wide")]
    # Only the Spotify keys are app-wide (and hidden): they work on any page that doesn't use the key
    # itself. Everything else is a Dashboard binding.
    BINDINGS = [
        Binding("p", "spotify('playpause')", show=False),
        Binding("greater_than_sign", "spotify('next')", show=False),
        Binding("less_than_sign", "spotify('previous')", show=False),
        Binding("right_square_bracket", "spotify('louder')", show=False),
        Binding("left_square_bracket", "spotify('quieter')", show=False),
    ]

    def get_default_screen(self) -> Screen:
        return Dashboard(id="_default")

    # (column, [(key, title, colour, size)]). "fit" tiles hug short action lists,
    # "fill" tiles split whatever height is left in their column.
    COLUMNS = [
        ("col-code", [
            ("agent", "AGENT", "pink", "fit"),  # tile 0: what your agent (Hermes) left you
            ("reviews", "REVIEW QUEUE", "pink", "fit"),
            ("mine", "MY PULL REQUESTS", "blue", "fit"),
            ("notifs", "GITHUB INBOX", "", "fill"),
            ("releases", "RELEASES", "cyan", "fit"),
        ]),
        ("col-comms", [
            ("mail", "MAIL", "blue", "fill"),
            ("slack", "SLACK", "", "fill"),
        ]),
        ("col-plan", [
            ("agenda", "AGENDA", "cyan", "fit"),
            ("linear", "LINEAR QUESTS", "pink", "fill"),
            ("ci", "CI PIPELINE", "", "fit"),
        ]),
    ]

    def __init__(self, splash: bool = True, setup: bool = False) -> None:
        super().__init__()
        self.splash = splash
        self.open_setup = setup
        self.system = System()
        self.updater = Updater()
        self.restart_note: str | None = None
        self.llm_loading: tuple[str, float] | None = None
        self.jobs: dict[str, Callable[[], Awaitable[None]]] = {}
        self._seen_reviews: set[str] | None = None
        from .agentstore import AgentStore

        self.agent_store = AgentStore()
        self._agent_known: set[str] | None = None  # every key the tile has shown (None until the first build)
        self._agent_opened: set[str] = set()  # keys you've opened: they stop pulsing
        self._snapshot_dirty = False
        self._build_sources()

    def _build_sources(self) -> None:
        self.cfg = load_config()
        self.gh = GitHub(self.cfg["github"])
        self.outlook = Outlook(self.cfg["outlook"]) if self.cfg["outlook"].get("enabled") else None
        self.mail = Mail(self.cfg, self.outlook)
        old_slack = getattr(self, "slack", None)
        self.slack = SlackClient(self.cfg["slack"])  # starts warm from ~/.config/pixeldeck/slack.db
        if old_slack is not None:  # keep conversation state across config reloads
            self.slack.convos, self.slack.users = old_slack.convos, old_slack.users
            self.slack.me, self.slack.team = old_slack.me, old_slack.team
            self.slack.bots = old_slack.bots
            self.slack._prev_latest, self.slack.fresh = old_slack._prev_latest, old_slack.fresh
            if old_slack.hide_apps != self.slack.hide_apps:
                for c in self.slack.convos.values():
                    c.refreshed = 0.0  # setting changed: unread state must be recomputed
        self.linear = Linear(self.cfg["linear"])
        from .sources.spotify import Spotify, installed

        self.spotify = Spotify(self.cfg["spotify"]) if self.cfg["spotify"].get("enabled") and installed() else None
        from .assist import Assist

        self.assist = Assist(self.cfg.get("assist", {}))
        if self.outlook:  # your company domain, so external emails stay professional
            try:
                accts = self.outlook._msal().get_accounts()
                if accts:
                    user = accts[0].get("username", "")
                    self.assist.identity["domain"] = user.rpartition("@")[2]
                    self.assist.identity["name"] = user.split("@")[0].split(".")[0].capitalize()
            except Exception:
                pass
        old = getattr(self, "releases", None)
        self.releases = Releases(self.cfg["releases"])
        if old:
            self.releases.health = old.health  # keep the uptime history across reloads

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Sprite(px.INVADER, id="sprite")
            yield Logo(id="logo")
            yield Hud(id="hud")
            yield Clock(id="clock")
        yield Divider(id="divider")
        n = 0
        with Horizontal(id="main"):
            for col_id, tiles in self.COLUMNS:
                with Vertical(id=col_id, classes="col"):
                    for key, title, color, size in tiles:
                        if key == "agent":
                            yield Tile(key, 0, title, color, size)
                            continue
                        n += 1
                        yield Tile(key, n, title, color, size)
        yield Vitals(n + 1)
        yield Footer()

    def relayout(self) -> None:
        """Narrow a column whose tiles all collapsed, so busy columns get the width."""
        for col in self.query(".col"):
            tiles = list(col.query(Tile))
            col.set_class(all(t.has_class("-slim") for t in tiles), "-quiet")
            fills_idle = all(t.has_class("-slim") for t in tiles if t.has_class("fill"))
            for t in tiles:
                t.set_class(fills_idle and t.has_class("fit") and not t.has_class("-slim"), "-grow")

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "pixeldeck"
        if self.splash:
            self.push_screen(SplashScreen())
        refresh = self.cfg["refresh"]
        self._schedule("github", refresh["github"], self._github)
        self._schedule("notifs", refresh["github"], self._notifs)
        self._schedule("mail", refresh["mail"], self._mail)
        self._schedule("agenda", refresh["calendar"], self._agenda)
        self._schedule("slack", min(refresh["slack"], 30), self._slack)
        self._schedule("linear", refresh["linear"], self._linear)
        self._schedule("repos", refresh["repos"], self._repos)
        self._schedule("releases", refresh["releases"], self._releases)
        self._schedule("deploys", 5, self._deploy_watch)
        self._schedule("claude", 15, self._claude)
        self._schedule("llm", 10, self._llm)
        self._schedule("agent", 10, self._agent)
        self._schedule("spotify", 2, self._spotify)
        self.set_interval(20, self._write_snapshot)
        self.set_interval(1, self._llm_tick)
        self.set_interval(refresh["system"], self._vitals)
        self._vitals()
        self.query_one("#reviews", Tile).focus()
        self.query_one(Logo).version = self.updater.version
        # self-update: watch our own source every 3s, the git remote every N minutes
        self.set_interval(3, self._watch_local)
        self.set_interval(max(self.cfg["update"]["check_minutes"], 1) * 60, self._check_remote)
        self.set_timer(20, self._check_remote)
        if note := os.environ.pop("PIXELDECK_UPDATED", None):
            self.notify(note, title="★ UPDATED", timeout=8)
        if self.open_setup:
            self.call_after_refresh(self.action_setup)
        if self.assist.enabled:
            self.run_worker(self.assist.warm(), group="assist-warm")

    # scheduling ---------------------------------------------------------------

    def _schedule(self, name: str, every: float, fn: Callable[[], Awaitable[None]]) -> None:
        self.jobs[name] = fn
        self.set_interval(every, lambda: self._run(name))
        self._run(name)

    def _run(self, name: str) -> None:
        self.run_worker(self._wrapped(name), group=name, exclusive=True, exit_on_error=False)

    async def _wrapped(self, name: str) -> None:
        hud = self.query_one(Hud)
        hud.busy += 1
        try:
            await self.jobs[name]()
        finally:
            hud.busy -= 1

    async def _feed(self, key: str, coro: Awaitable[Feed], hud_key: str | None = None) -> Feed | None:
        tile = self.query_one(f"#{key}", Tile)
        try:
            feed = await coro
        except NotConfigured as exc:
            tile.set_problem(str(exc), setup=True)
            return None
        except Exception as exc:  # keep the dashboard alive whatever a source does
            tile.set_problem(f"{type(exc).__name__}: {exc}"[:200], setup=False)
            return None
        tile.set_feed(feed)
        self._snapshot_dirty = True
        if hud_key:
            self.query_one(Hud).set_count(hud_key, feed.count)
        return feed

    # jobs ---------------------------------------------------------------------

    async def _github(self) -> None:
        try:
            queue, mine = await self.gh.pull_requests()
        except Exception as exc:
            for key in ("reviews", "mine"):
                self.query_one(f"#{key}", Tile).set_problem(str(exc)[:200], setup=isinstance(exc, NotConfigured))
        else:
            self._announce(queue)
            self.query_one("#reviews", Tile).set_feed(queue)
            self.query_one("#mine", Tile).set_feed(mine)
            self.query_one(Hud).set_count("reviews", queue.count)
            if self.gh.login:
                logo = self.query_one(Logo)
                logo.player = self.gh.login
        await self._feed("ci", self.gh.ci_runs(), "ci")

    def _announce(self, queue: Feed) -> None:
        urls = {i.url for i in queue.items if i.url}
        if self._seen_reviews is not None:
            for item in queue.items:
                if item.url and item.url not in self._seen_reviews:
                    self.notify(item.title, title="★ NEW REVIEW REQUEST", timeout=8)
                    self.bell()
        self._seen_reviews = urls

    async def _notifs(self) -> None:
        await self._feed("notifs", self.gh.notifications())

    async def _mail(self) -> None:
        feed = await self._feed("mail", self.mail.inbox(), "mail")
        if feed and feed.arrived:
            first = feed.arrived[0]
            who = first.meta.split(" · ")[0]
            more = f"  (+{len(feed.arrived) - 1} more)" if len(feed.arrived) > 1 else ""
            self.notify(f"{who}: {first.title}{more}", title="✉ NEW MAIL", timeout=8)
            self.bell()

    async def _agenda(self) -> None:
        async def agenda() -> Feed:
            if not self.outlook:
                raise NotConfigured("sign in to Microsoft 365 for your calendar")
            return await self.outlook.agenda()

        await self._feed("agenda", agenda())

    async def _slack(self) -> None:
        feed = await self._feed("slack", self.slack.feed(budget=12), "slack")
        if feed and self.slack.arrived:
            c = self.slack.arrived[0]
            more = f"  (+{len(self.slack.arrived) - 1} more)" if len(self.slack.arrived) > 1 else ""
            text = self.slack.render_text(c.latest_text)[:120]
            self.notify(f"{self.slack.name(c.latest_user)} in {c.name}: {text}{more}", title="✦ SLACK", timeout=8)
            self.bell()

    def open_slack(self, convo: str | None = None, ts: str | None = None, thread_ts: str | None = None,
                   draft: str | None = None) -> None:
        from .slackui import SlackScreen

        def back(_=None) -> None:
            self._run("slack")
            self._focus_tile("slack")

        self.push_screen(SlackScreen(self.slack, convo, ts, thread_ts, draft), back)

    async def _linear(self) -> None:
        feed = await self._feed("linear", self.linear.issues())  # title: everything that matches
        if feed is not None:
            self.query_one(Hud).set_count("linear", feed.active)  # ▶ ACTIVE chip: in progress only

    async def _llm(self) -> None:
        vitals = self.query_one(Vitals)
        vitals.llm = await self.assist.status()
        vitals.llm_enabled = self.assist.enabled
        vitals.llm_ms = self.assist.last_ms
        vitals.refresh()

    def _llm_tick(self) -> None:
        vitals = self.query_one(Vitals)
        vitals.llm_loading = self.llm_loading
        if self.llm_loading:
            vitals.refresh()  # keep the load timer moving

    def refresh_llm(self) -> None:
        self._run("llm")

    def action_ask(self, mode: str | None = None) -> None:
        """? : chat with the local model (searches every connected source) or your agent (Hermes).
        Opens in the mode you used last ([ask] default to start); ctrl+t switches."""
        from .chatui import AskScreen

        mode = mode or getattr(self, "ask_mode", None) or self.cfg["ask"].get("default", "model")
        self.push_screen(AskScreen(mode))

    def ask_session_for(self, mode: str):
        sessions = self.__dict__.setdefault("ask_sessions", {})
        if mode not in sessions:
            self.new_ask_session(mode)
        return sessions[mode]

    def new_ask_session(self, mode: str):
        from .agentask import AgentSession
        from .ask import AskSession

        sessions = self.__dict__.setdefault("ask_sessions", {})
        sessions[mode] = AgentSession(self) if mode == "agent" else AskSession(self)
        return sessions[mode]

    def action_linear_search(self) -> None:
        """/ : search every Linear ticket; reopens with the last search."""
        from .linearui import LinearSearch

        if not get_secret("linear", env="LINEAR_API_KEY"):
            self._setup_for("linear")
            return
        q, open_only, mine = getattr(self, "linear_search_state", ("", False, False))
        self.push_screen(LinearSearch(q, open_only, mine))

    def action_agenda(self, focus_id: str | None = None, prefill: dict | None = None) -> None:
        if not self.outlook:
            self._setup_for("agenda")
            return
        from .calendarui import AgendaScreen

        def back(_=None) -> None:
            self._run("agenda")  # pick up anything you created
            self._focus_tile("agenda")

        self.push_screen(AgendaScreen(focus_id, prefill), back)

    # agent ----------------------------------------------------------------------

    async def _agent(self) -> None:
        from .agentui import build_feed

        tile = self.query_one("#agent", Tile)
        first = self._agent_known is None
        feed = await asyncio.to_thread(build_feed, self.agent_store, None)
        keys = {it.ref["key"] for it in feed.items}
        if first:
            self._agent_opened |= keys
        else:
            new = [it for it in feed.items if it.ref["key"] not in self._agent_known and it.ref["kind"] != "agent_task"]
            if new:
                more = f"  (+{len(new) - 1} more)" if len(new) > 1 else ""
                self.notify(f"{new[0].title}{more}", title="✦ AGENT", timeout=8)
                self.bell()
            for it in feed.items:  # pulse until opened
                it.flash = it.ref["key"] not in self._agent_opened and it.ref["kind"] != "agent_task"
        self._agent_known = keys | (self._agent_known or set())
        tile.set_feed(feed)

    def _write_snapshot(self) -> None:
        """What every tile shows, for the agent (it runs in another process)."""
        if not self._snapshot_dirty:
            return
        self._snapshot_dirty = False
        from .agentstore import write_snapshot

        tiles = [{"key": t.key, "label": t.label, "note": t.feed.note, "count": t.feed.count, "items": t.feed.items}
                 for t in self.query(Tile) if t.key != "agent" and t.feed is not None]
        try:
            write_snapshot(tiles)
        except Exception:
            pass

    def open_item(self, item, source: str = "AGENT", draft: str | None = None) -> None:
        """Open any item where it belongs: Slack in the console, events in the agenda, the rest as a page."""
        ref = item.ref or {}
        kind = ref.get("kind")
        if kind == "graph_event":
            self.action_agenda(ref.get("id"))
        elif kind == "slack_convo":
            self.open_slack(ref.get("id"), ref.get("ts"), ref.get("thread_ts"), draft)
        elif kind == "slack" and ref.get("channel"):
            self.open_slack(ref["channel"], ref.get("ts"), ref.get("thread_ts"), draft)
        else:
            self.push_screen(DetailScreen([item], 0, source, draft))

    def agent_open(self, item) -> None:
        from .agentui import SNOOZE_SECS, AgentCard

        ref = item.ref or {}
        store = self.agent_store
        self._agent_opened.add(ref.get("key", ""))
        kind = ref.get("kind")
        if kind == "agent_update":
            store.mark_update_seen(ref["id"])
            self.push_screen(DetailScreen([item], 0, "AGENT"), lambda _=None: self._run("agent"))
            return
        is_task = kind == "agent_task"
        row = store.task(ref["id"]) if is_task else store.db.execute(
            "select * from proposals where id = ?", (ref["id"],)).fetchone()
        if row is None:
            self._run("agent")
            return

        def chose(what: str | None) -> None:
            if what:
                self.agent_act(row, is_task, what)

        self.push_screen(AgentCard(row, is_task), chose)

    def agent_act(self, row, is_task: bool, what: str) -> None:
        """What you chose on a card or the AGENT page: accept/snooze/dismiss a suggestion, done/tomorrow a task."""
        from . import agentstore as st
        from .agentui import SNOOZE_SECS

        store = self.agent_store
        about = st.item_from_json(row["item"])
        if what == "source":
            if about:
                self.open_item(about)
        elif is_task and what == "done":
            store.complete_task(row["id"])
            self.notify(row["title"], title="☑ DONE", timeout=3)
        elif is_task and what == "tomorrow":
            from datetime import datetime, timedelta

            base = datetime.fromtimestamp(row["due"]) if row["due"] else datetime.now().replace(hour=17, minute=0)
            nxt = max(base, datetime.now().replace(hour=base.hour, minute=base.minute)) + timedelta(days=1)
            store.update_task(row["id"], due=nxt.timestamp())
            self.notify(f"due {st.fmt_when(nxt.timestamp())}", title="☐ MOVED", timeout=3)
        elif not is_task and what == "accept":
            self._agent_accept(row, about)
        elif not is_task and what == "snooze":
            store.decide(row["id"], "snoozed", SNOOZE_SECS)
            self.notify("back in 2 hours", title="◷ SNOOZED", timeout=3)
        elif not is_task and what == "dismiss":
            store.decide(row["id"], "dismissed")
            self.notify(row["title"], title="✖ DISMISSED", timeout=3)
        self._run("agent")

    def _focus_tile(self, key: str) -> None:
        """Back on the dashboard, focus a tile (a no-op when another page, like AGENT, is underneath)."""
        try:
            self.query_one(f"#{key}", Tile).focus()
        except Exception:
            pass

    def _agent_accept(self, row, about) -> None:
        import json

        p = json.loads(row["payload"] or "{}")
        kind = row["kind"]
        if kind == "reply_mail" and about:
            self.push_screen(DetailScreen([about], 0, "AGENT", p.get("draft") or None))
        elif kind == "slack_message" and p.get("channel"):
            self.open_slack(p["channel"], None, p.get("thread_ts"), p.get("draft") or None)
        elif kind == "create_event":
            self.action_agenda(None, p.get("event") or {})
        elif kind == "add_task":
            self.agent_store.add_task(row["title"], p.get("notes") or "", p.get("due"), about, source="agent")
            self.notify(row["title"], title="☐ TASK ADDED", timeout=3)
        elif kind == "open" and about:
            self.open_item(about)
        else:
            self.notify("this suggestion is missing what it needs to open", title="✖ AGENT", severity="error")
            return
        self.agent_store.decide(row["id"], "accepted")

    def action_agent_page(self) -> None:
        from .agentui import AgentScreen

        self.push_screen(AgentScreen(), lambda _=None: (self._run("agent"), self._focus_tile("agent")))

    def action_add_task(self, after: Callable[[], None] | None = None) -> None:
        from .slackui import AskScreen

        def done(text: str | None) -> None:
            if not text:
                return
            due = None
            title = text
            if " @ " in text:
                from datetime import datetime, time as dtime

                from .calendarui import parse_clock, parse_day

                title, _, when = text.rpartition(" @ ")
                parts = when.split()
                day = parse_day(parts[0]) if parts else None
                clock = parse_clock(" ".join(parts[1:])) if len(parts) > 1 else None
                if day is None:
                    self.notify(f"can't read “{when}” as a day; try fri, tomorrow, 11/14", title="☐ TASK", severity="error")
                    return
                due = datetime.combine(day, dtime(*(clock or (17, 0)))).timestamp()
            self.agent_store.add_task(title.strip(), due=due)
            self.notify(title.strip(), title="☐ TASK ADDED", timeout=3)
            self._run("agent")
            if after:
                after()

        self.push_screen(AskScreen("☐ NEW TASK", "what · optionally “ @ fri 2pm”"), done)

    def action_linear_filter(self) -> None:
        from .linearui import LinearFilter

        tile = self.query_one("#linear", Tile)
        facets = (tile.feed.extra or {}).get("facets") if tile.feed else None

        def done(applied: bool | None) -> None:
            if applied:
                self.linear = Linear(load_config()["linear"])
                self._run("linear")
                tile.focus()

        self.push_screen(LinearFilter(facets), done)

    def action_llm_models(self) -> None:
        from .llmui import ModelScreen

        self.push_screen(ModelScreen(), lambda _=None: self.refresh_llm())

    async def _claude(self) -> None:
        from .sources import claude as c

        if not hasattr(self, "_claude_scanner"):
            self._claude_scanner = c.UsageScanner()
        vitals = self.query_one(Vitals)
        try:
            vitals.claude_usage = await asyncio.to_thread(self._claude_scanner.scan)
        except Exception:
            vitals.claude_usage = None
        vitals.claude_wrapped = c.wrapper_installed()
        vitals.meters = self.cfg["system"].get("meters", vitals.meters)
        vitals.warn_pct = int(self.cfg.get("claude", {}).get("warn_pct", 80))
        vitals.claude_limits = c.read_limits() if vitals.claude_wrapped else None
        vitals.refresh()

    async def _releases(self) -> None:
        self._releases_at = time.monotonic()
        await self._feed("releases", self.releases.pipeline(), "releases")

    async def _deploy_watch(self) -> None:
        """Every 5s: a ship script starting or finishing refreshes RELEASES now; while anything is
        deploying it refreshes every 30s (instead of every minute or so) to catch the new version."""
        local = await self.releases.local_deploys()
        key = tuple(sorted((k, v["ref"]) for k, v in local.items()))
        changed = key != getattr(self, "_deploy_key", key)
        self._deploy_key = key
        busy = bool(local or self.releases.deploying)
        if changed or (busy and time.monotonic() - getattr(self, "_releases_at", 0) > 30):
            self._run("releases")

    async def _repos(self) -> None:
        vitals = self.query_one(Vitals)
        if self.cfg["system"].get("docker", True):
            vitals.docker = await docker_summary()
        vitals.repos = await repo_status(self.cfg["system"].get("repos", []))
        vitals.refresh()

    async def _spotify(self) -> None:
        vitals = self.query_one(Vitals)
        if not self.spotify:
            if vitals.spotify is not None:
                vitals.spotify, vitals.spotify_art = None, None
                vitals.refresh()
            return
        np = await self.spotify.now()
        old = vitals.spotify
        vitals.spotify = np
        if np.art_url and (old is None or old.art_url != np.art_url or vitals.spotify_art is None):
            vitals.spotify_art = await self.spotify.art(np.art_url)
        vitals.refresh()

    def action_spotify_page(self) -> None:
        if not self.spotify:
            self.notify("Spotify is off or not installed ([spotify] enabled in config.toml)", timeout=4)
            return
        from .spotifyui import SpotifyScreen

        self.push_screen(SpotifyScreen(), lambda _=None: self._run("spotify"))

    async def action_spotify(self, what: str) -> None:
        if not self.spotify:
            self.notify("Spotify is off or not installed ([spotify] enabled in config.toml)", timeout=4)
            return
        try:
            await self.spotify.command(what)
        except Exception as exc:
            self.notify(str(exc)[:160], title="SPOTIFY", severity="warning", timeout=6)
            return
        self.set_timer(0.4, lambda: self._run("spotify"))

    def _vitals(self) -> None:
        self.query_one(Vitals).push(self.system.vitals(self.cfg["refresh"]["system"]))

    # actions ------------------------------------------------------------------

    def _focused_url(self) -> str | None:
        w = self.focused
        return w.current_url() if isinstance(w, Tile) else None

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        tile = event.option_list
        if not isinstance(tile, Tile):
            return
        oid = event.option.id or ""
        if oid.endswith("-setup"):
            self._setup_for(tile.key)
            return
        index = int(oid.rpartition("-")[2]) if oid.rpartition("-")[2].isdigit() else None
        picked = tile.feed.items[index] if tile.feed and index is not None and index < len(tile.feed.items) else None
        if tile.key == "agent":
            if picked:
                self.agent_open(picked)
            return
        if picked and (picked.ref or {}).get("kind") == "graph_event":
            self.action_agenda(picked.ref.get("id"))
            return
        if picked and (picked.ref or {}).get("kind") == "slack_convo":
            ref = picked.ref
            self.slack.fresh.discard(ref.get("id"))
            self.open_slack(ref.get("id"), ref.get("ts"), ref.get("thread_ts"))
            return
        if tile.feed and index is not None and index < len(tile.feed.items):
            self._open_detail(tile, index)
        elif oid in tile.urls:
            subprocess.Popen(["open", tile.urls[oid]])
            if tile.feed and index is not None and index < len(tile.feed.items):
                self.item_viewed(tile.feed.items[index])

    def item_viewed(self, item) -> None:
        """Called when an item is opened (detail page or browser): mail gets acknowledged."""
        from .sources.mailstate import mail_key

        if not mail_key(item) or not self.cfg["mail"].get("ack_on_open", True):
            return
        was_unread = item.unread
        self.mail.state.acknowledge(item)
        tile = self.query_one("#mail", Tile)
        tile.acknowledge(item)
        if was_unread and tile.feed and tile.feed.count:
            tile.feed.count -= 1
            tile._set_title(tile.feed.count)
            self.query_one(Hud).set_count("mail", tile.feed.count)

    def _setup_for(self, key: str) -> None:
        if key == "agenda":
            self.push_screen(OutlookScreen(), self._setup_done)
        elif key in SERVICES:
            self.push_screen(FormScreen(SERVICES[key]), self._setup_done)
        else:
            self.action_setup()

    def action_slack_console(self) -> None:
        try:
            self.slack.token()
        except NotConfigured:
            self._setup_for("slack")
            return
        self.open_slack()

    def action_setup(self) -> None:
        from .settings import SettingsScreen

        self.push_screen(SettingsScreen(), self._setup_done)

    def _setup_done(self, changed: bool | None) -> None:
        if changed:
            self._build_sources()
            for name in self.jobs:
                self._run(name)
            self.notify("new keys loaded, re-syncing", title="⚙ SETUP", timeout=3)

    # self-update ----------------------------------------------------------------

    def _config_mtime(self) -> float:
        from .config import CONFIG_PATH

        return CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0

    def _watch_local(self) -> None:
        mtime = self._config_mtime()
        if mtime != getattr(self, "_cfg_mtime", mtime):
            try:
                self._build_sources()
            except Exception as exc:  # a half-edited config keeps the old one running
                self.notify(str(exc)[:200], title="⚠ CONFIG DIDN'T PARSE", severity="error", timeout=8)
            else:
                for name in self.jobs:
                    self._run(name)
                self.notify("config.toml changed, re-syncing", title="⚙ CONFIG", timeout=3)
        self._cfg_mtime = mtime
        if not self.updater.local_changed() or getattr(self, "_reloading", False):
            return
        if err := self.updater.compiles():
            self.updater.stamp = self.updater._fingerprint()  # wait for the next save
            self.notify(err, title="⚠ NEW BUILD DOESN'T COMPILE · STAYING PUT", severity="error", timeout=8)
            return
        self._reloading = True
        self.run_worker(self._safe_reload(), group="reload", exclusive=True)

    async def _safe_reload(self) -> None:
        """Reinstall if dependencies changed, prove the new code imports, then restart into it."""
        try:
            await asyncio.sleep(1.5)  # let a burst of saves settle
            if self.updater.deps_changed():
                self.notify("dependencies changed, installing…", title="⬆ RELOAD", timeout=4)
                await self.updater.reinstall()
                self.updater.deps_stamp = self.updater._deps_fingerprint()
            if err := await self.updater.imports():
                self.updater.stamp = self.updater._fingerprint()
                self.notify(err[:300], title="⚠ NEW BUILD DOESN'T LOAD · STAYING PUT", severity="error", timeout=10)
                return
            self._restart("reloaded local changes")
        except Exception as exc:
            self.updater.stamp = self.updater._fingerprint()
            self.notify(str(exc)[:200], title="⚠ RELOAD FAILED · STAYING PUT", severity="error", timeout=10)
        finally:
            self._reloading = False

    def _check_remote(self) -> None:
        self.run_worker(self._remote(apply=self.cfg["update"].get("auto", False)), group="update", exclusive=True)

    async def _remote(self, apply: bool, loud: bool = False) -> None:
        try:
            pending = await self.updater.pending()
        except Exception as exc:
            if loud:
                self.notify(str(exc), title="⚠ UPDATE CHECK FAILED", severity="error")
            return
        self.query_one(Hud).set_count("update", len(pending))
        if not pending:
            if loud:
                where = "remote" if await self.updater.has_upstream() else "remote (none set: local edits still auto-reload)"
                self.notify(f"already on the latest from the {where}", title="✔ UP TO DATE", timeout=4)
            return
        if not apply:
            return
        self.notify(pending[0], title=f"⬆ INSTALLING {len(pending)} UPDATE(S)", timeout=4)
        try:
            subjects = await self.updater.pull()
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ UPDATE FAILED", severity="error", timeout=10)
            return
        self._restart(" · ".join(subjects[:3]) or "pulled latest")

    def action_update(self) -> None:
        self.run_worker(self._remote(apply=True, loud=True), group="update", exclusive=True)

    def _restart(self, note: str) -> None:
        self.restart_note = note
        self.exit()

    def action_open(self) -> None:
        if url := self._focused_url():
            subprocess.Popen(["open", url])
            tile = self.focused
            opt = tile.highlighted_option if isinstance(tile, Tile) else None
            idx = opt.id.rpartition("-")[2] if opt and opt.id else ""
            if tile.feed and idx.isdigit() and int(idx) < len(tile.feed.items):
                self.item_viewed(tile.feed.items[int(idx)])

    def action_copy(self) -> None:
        if url := self._focused_url():
            subprocess.run(["pbcopy"], input=url.encode())
            self.notify(url, title="COPIED", timeout=3)

    def action_refresh(self) -> None:
        for name in self.jobs:
            self._run(name)
        self.notify("re-syncing every source", title="▶ REFRESH", timeout=2)

    def _open_detail(self, tile: Tile, index: int) -> None:
        items = tile.feed.items

        def back(i: int | None) -> None:
            # land on whatever item the page was showing when you left
            if i is not None and tile.feed and tile.feed.items is items:
                try:
                    tile.highlighted = tile.get_option_index(f"{tile.key}-{i}")
                except Exception:
                    pass
            tile.focus()

        self.push_screen(DetailScreen(items, index, tile.label), back)

    DOUBLE_PRESS = 0.5  # seconds between two presses of the same number

    QUIT_WINDOW = 1.5  # seconds for the second q

    def open_url(self, url: str, *, new_tab: bool = True) -> None:
        """Email attachment links (pixeldeck-att:) open just that file; everything else goes to the browser."""
        from . import attachments

        if url.startswith(attachments.SCHEME):
            self.open_attachment(url)
        else:
            super().open_url(url, new_tab=new_tab)

    def open_attachment(self, url: str, reveal: bool = False, in_app: bool = False) -> None:
        self.run_worker(self._open_attachment(url, reveal, in_app), group="attachment")

    async def _open_attachment(self, url: str, reveal: bool, in_app: bool = False) -> None:
        from . import attachments

        self.notify("fetching attachment…", title="📎", timeout=2)
        try:
            how, path = await attachments.open_link(self, url, reveal=reveal, in_app=in_app)
        except Exception as exc:
            self.notify(str(exc)[:200], title="✖ COULDN'T OPEN ATTACHMENT", severity="error", timeout=6)
            return
        if how == "finder" and not reveal:
            self.notify(f"{path.name} could run code, so it's shown in Finder instead of opened",
                        title="📦 SHOWN IN FINDER", timeout=6)

    def action_quit_twice(self) -> None:
        """q quits only when pressed twice; one stray q just asks."""
        now = time.monotonic()
        if now - getattr(self, "_quit_armed", 0.0) <= self.QUIT_WINDOW:
            self.exit()
            return
        self._quit_armed = now
        self.notify("press q again to quit", title="QUIT?", timeout=self.QUIT_WINDOW)

    def action_jump(self, n: int) -> None:
        """One press focuses tile n; the same number twice quickly opens its page (66 = Slack)."""
        tile = next((t for t in self.query(Tile) if t.num == n), None)
        if tile is None:
            return
        last_n, last_t = getattr(self, "_last_jump", (None, 0.0))
        now = time.monotonic()
        self._last_jump = (n, now)
        if last_n == n and now - last_t <= self.DOUBLE_PRESS:
            self._last_jump = (None, 0.0)
            self.open_tile_page(tile)
        else:
            tile.focus()

    def open_tile_page(self, tile: Tile) -> None:
        """The full page for a tile: Slack's console, setup if it isn't connected, else the item pages."""
        if tile.key == "slack":
            self.action_slack_console()
            return
        if tile.key == "agenda" and self.outlook:
            self.action_agenda()
            return
        if tile.key == "agent":
            self.action_agent_page()
            return
        if not tile.feed:
            opt = tile.get_option_at_index(0) if tile.option_count else None
            if opt is not None and (opt.id or "").endswith("-setup"):
                self._setup_for(tile.key)
            return
        if not tile.feed.items:
            self.notify("nothing here right now", title=tile.label, timeout=2)
            return
        opt = tile.highlighted_option
        idx = opt.id.rpartition("-")[2] if opt and opt.id else ""
        index = int(idx) if idx.isdigit() and int(idx) < len(tile.feed.items) else 0
        self._open_detail(tile, index)
