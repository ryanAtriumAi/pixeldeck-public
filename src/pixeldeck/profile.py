"""Your profile: who the dashboard is for, and how it looks.

PIXELDECK asks once, the first time it starts without a [profile] in config.toml. Saving writes
[profile] (and [ui] theme if you picked another one) and nothing else: sources, filters and every
other setting stay as they are. Change it later in Settings → Profile.
"""

from __future__ import annotations

import getpass
import subprocess
import tomllib

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from . import pixel as px
from .config import CONFIG_PATH, get_value, set_value
from .setup import SETUP_CSS, _bad
from .widgets import TextInput


def has_profile() -> bool:
    """True when config.toml itself names you (built-in defaults don't count)."""
    if not CONFIG_PATH.exists():
        return False
    try:
        with CONFIG_PATH.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return True  # a config that doesn't parse has its own warning; don't stack a prompt on it
    profile = data.get("profile")
    return isinstance(profile, dict) and bool(str(profile.get("name") or "").strip())


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def guess(outlook=None) -> dict[str, str]:
    """A starting point for the form, from what this Mac and your accounts already say."""
    name = str(get_value("profile.name") or get_value("assist.name") or "").strip()
    if not name:
        name = _run("id", "-F") or getpass.getuser()
    email = str(get_value("profile.email") or "").strip()
    if not email and outlook is not None:
        try:
            email = next(iter(a.get("username", "") for a in outlook._msal().get_accounts()), "")
        except Exception:
            email = ""
    if not email:
        email = _run("git", "config", "--global", "user.email")
    return {"name": name, "email": email}


def theme_row(key: str, current: bool, width: int = 68) -> Text:
    """One theme as a list row, drawn in its own colours so the row is the preview."""
    t = px.THEMES[key]
    bg = t["VOID"]
    out = Text(no_wrap=True)
    out.append(" ✔ " if current else "   ", Style(color=t["GREEN"], bgcolor=bg, bold=True))
    out.append(f"{t['label']:<9}", Style(color=t["INK"], bgcolor=bg, bold=True))
    for slot in ("PURPLE", "BLUE", "CYAN", "HOTPINK", "PINK", "YELLOW", "RED", "GREEN"):
        out.append("██", Style(color=t[slot], bgcolor=bg))
    out.append(" ", Style(bgcolor=bg))
    out.append(" 8 ▸ TILE ", Style(color=t["VOID"], bgcolor=t["PURPLE"], bold=True))
    out.append(" NEW ", Style(color=t["VOID"], bgcolor=t["HOTPINK"], bold=True))
    out.append(f" {t['about']}", Style(color=t["DIM"], bgcolor=bg))
    pad = width - out.cell_len
    if pad > 0:
        out.append(" " * pad, Style(bgcolor=bg))
    out.truncate(width)
    return out


class ThemeList(OptionList):
    """Pick a theme. `chosen` is what's highlighted; Enter posts OptionSelected as usual."""

    DEFAULT_CSS = """
    ThemeList { height: auto; max-height: 9; border: none; padding: 0; }
    ThemeList > .option-list--option { padding: 0 2; }  /* the highlight shows in the margins beside a row */
    """

    def __init__(self, current: str, **kw) -> None:
        self.current = current if current in px.THEMES else px.DEFAULT_THEME
        super().__init__(*[Option(theme_row(k, k == self.current), id=k) for k in px.THEMES], **kw)

    def on_mount(self) -> None:
        self.highlighted = self.get_option_index(self.current)

    def on_resize(self) -> None:
        self._redraw()  # rows are cut to the list's width, so none wraps onto a second line

    def _redraw(self) -> None:
        width = max(self.size.width - 4, 30)  # the option padding either side
        for k in px.THEMES:
            self.replace_option_prompt(k, theme_row(k, k == self.current, width))

    @property
    def chosen(self) -> str:
        opt = self.highlighted_option
        return opt.id if opt and opt.id else self.current

    def mark(self, key: str) -> None:
        """Move the ✔ to `key` (after it was saved)."""
        self.current = key
        self._redraw()


class ProfileScreen(ModalScreen[dict | None]):
    """Name, email and theme. Dismisses with {"theme": changed?} when saved, None when put off."""

    CSS = SETUP_CSS + f"""
    ProfileScreen {{ align: center middle; background: {px.VOID} 70%; }}
    ProfileScreen .box {{ width: 84; }}
    ProfileScreen .hint {{ color: {px.DIM}; }}
    ProfileScreen ThemeList {{ background: {px.DEEP}; margin-top: 0; }}
    ProfileScreen ThemeList > .option-list--option-highlighted {{ background: {px.HOTPINK}; }}
    """
    BINDINGS = [Binding("escape", "later", "Later"), Binding("ctrl+s", "save", "Save")]

    def __init__(self, first_run: bool, start: dict[str, str]) -> None:
        super().__init__()
        self.first_run = first_run  # no config.toml yet: this is a brand-new install
        self.start = start

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="box") as box:
            box.border_title = " ★ PLAYER ONE · YOUR PROFILE "
            blurb = Text()
            if self.first_run:
                blurb.append("Welcome to PIXELDECK. ", Style(color=px.HOTPINK, bold=True))
                blurb.append("Say who you are and pick a look; connecting your accounts comes next.",
                             Style(color=px.INK))
            else:
                blurb.append("One thing is new: a profile. ", Style(color=px.HOTPINK, bold=True))
                blurb.append("Your sources, filters and settings stay exactly as they are; saving adds your "
                             "profile to config.toml and touches nothing else.", Style(color=px.INK))
            yield Static(blurb, classes="steps")
            yield Label("NAME", classes="field-label")
            yield TextInput(value=self.start.get("name", ""), placeholder="what PIXELDECK calls you", id="pf-name")
            yield Label("EMAIL  (optional)", classes="field-label")
            yield TextInput(value=self.start.get("email", ""), placeholder="you@company.com", id="pf-email")
            yield Static("Its domain tells reply assist who is a colleague and who is outside your company. "
                         "It stays on this Mac.", classes="hint")
            yield Label("THEME", classes="field-label")
            yield ThemeList(str(get_value("ui.theme") or px.DEFAULT_THEME), id="pf-theme")
            yield Static("↑ ↓ to look through them. A different theme restarts PIXELDECK (about a second).",
                         classes="hint")
            yield Static("", id="pf-status", classes="status")
            with Horizontal(classes="buttons"):
                yield Button("LATER", id="later")
                yield Button("SAVE  ctrl+s", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#pf-name", TextInput).focus()

    @on(TextInput.Submitted)
    def _next_field(self, e: TextInput.Submitted) -> None:
        if e.input.id == "pf-name":
            self.query_one("#pf-email", TextInput).focus()
        else:
            self.query_one("#pf-theme", ThemeList).focus()

    @on(OptionList.OptionSelected, "#pf-theme")
    def _theme_enter(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#later")
    def action_later(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        name = self.query_one("#pf-name", TextInput).value.strip()
        email = self.query_one("#pf-email", TextInput).value.strip()
        theme = self.query_one("#pf-theme", ThemeList).chosen
        status = self.query_one("#pf-status", Static)
        if not name:
            status.update(_bad("a name is all it needs"))
            self.query_one("#pf-name", TextInput).focus()
            return
        if email and ("@" not in email or " " in email):
            status.update(_bad("that doesn't look like an email address (or leave it empty)"))
            self.query_one("#pf-email", TextInput).focus()
            return
        was = str(get_value("ui.theme") or px.DEFAULT_THEME)
        try:
            set_value("profile.name", name)
            set_value("profile.email", email)
            if theme != was:
                set_value("ui.theme", theme)
        except Exception as exc:  # e.g. a config.toml that no longer parses
            status.update(_bad(f"couldn't save: {exc}"[:200]))
            return
        self.dismiss({"theme": theme != px.ACTIVE})
