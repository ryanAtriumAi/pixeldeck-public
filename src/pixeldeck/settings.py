"""Settings: every connector in one place. Changes save to config.toml as you make them
(comments kept), the dashboard reloads within a few seconds, ctrl+z undoes."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalGroup, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Footer, Input, Label, OptionList, Static, Switch

from .widgets import TextInput
from textual.widgets.option_list import Option

from . import pixel as px
from .config import (
    CONFIG_PATH,
    DEFAULTS,
    _MISSING,
    delete_secret,
    delete_value,
    file_value,
    get_secret,
    get_value,
    mask,
    set_value,
)
from .sources.chat import SCOPES as LINEAR_SCOPES, SORTS as LINEAR_SORTS, STATES as LINEAR_STATES
from .setup import SERVICES, FormScreen, GitHubScreen, OutlookScreen, _bad, _busy, _ok, status

SETTINGS_CSS = f"""
SettingsScreen {{ background: {px.VOID}; }}
#st-head {{ height: 2; background: {px.DEEP}; padding: 0 1; }}
#st-body {{ height: 1fr; }}
#st-nav {{ width: 34; background: {px.DEEP}; border-right: tall {px.PURPLE}; }}
#st-nav OptionList {{ height: 1fr; background: {px.DEEP}; border: none; }}
#st-nav OptionList > .option-list--option-highlighted {{ background: #3a1670; }}
#st-nav OptionList:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
#st-form {{ width: 1fr; padding: 1 2; background: {px.VOID}; scrollbar-size-vertical: 1; scrollbar-color: {px.PURPLE}; }}
#st-side {{ width: 40; padding: 1 2; background: {px.DEEP}; border-left: tall {px.SHADOW}; }}
.st-title {{ color: {px.HOTPINK}; text-style: bold; margin-bottom: 1; }}
.st-sub {{ color: {px.VOID}; background: {px.PURPLE}; text-style: bold; width: auto; padding: 0 1; margin-top: 1; }}
.st-status {{ height: auto; margin-bottom: 1; }}
.field {{ height: auto; margin-top: 1; }}
.field > .lbl {{ width: 24; color: {px.CYAN}; text-style: bold; padding-top: 1; }}
.field > .ctl {{ width: 1fr; height: auto; }}
.field .help {{ color: {px.DIM}; height: auto; }}
.field Input {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
.field Input:focus {{ border: tall {px.HOTPINK}; }}
.field Input.-bad {{ border: tall {px.RED}; }}
.field Switch {{ background: {px.DEEP}; border: tall {px.PURPLE}; }}
.field Switch:focus {{ border: tall {px.HOTPINK}; }}
.field Switch > .switch--slider {{ color: {px.SHADOW}; background: {px.PANEL}; }}
.field Switch.-on > .switch--slider {{ color: {px.HOTPINK}; }}
.chips {{ height: auto; }}
.chip {{ min-width: 4; height: 1; border: none; background: {px.SHADOW}; color: {px.INK}; margin: 0 1 0 0; }}
.chip:hover, .chip:focus {{ background: {px.RED}; color: {px.VOID}; }}
.checks {{ height: auto; }}
.checks Checkbox {{ background: {px.DEEP}; border: none; margin-right: 1; }}
.actions {{ height: auto; margin-top: 1; }}
#st-form Button {{ margin-right: 1; min-width: 12; background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
#st-form Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
#st-form Button.-error {{ background: {px.DEEP}; color: {px.RED}; border: tall {px.RED}; }}
#st-form Button.chip {{ min-width: 4; height: 1; border: none; background: {px.SHADOW}; color: {px.INK}; margin: 0 1 0 0; }}
#st-form Button.chip:hover, #st-form Button.chip:focus {{ background: {px.RED}; color: {px.VOID}; }}
.envrow {{ height: auto; margin-top: 1; padding: 0 1; background: {px.DEEP}; }}
.envrow > Static {{ width: 1fr; padding-top: 1; }}
EnvScreen {{ align: center middle; background: {px.VOID} 70%; }}
EnvScreen > VerticalScroll {{ width: 96; height: auto; max-height: 90%; background: {px.DEEP}; border: panel {px.HOTPINK};
                              border-title-color: {px.VOID}; border-title-style: bold; padding: 1 2; }}
"""


def _txt(s: str, color: str = px.INK, bold: bool = False) -> Text:
    return Text(s, Style(color=color, bold=bold))


# ── fields ───────────────────────────────────────────────────────────────────


class Field(Horizontal):
    """Label on the left, a control and help text on the right, bound to one config key."""

    DEFAULT_CLASSES = "field"

    def __init__(self, path: str, label: str, help: str = "") -> None:
        super().__init__()
        self.path, self.label_text, self.help_text = path, label, help

    def compose(self) -> ComposeResult:
        yield Label(self.label_text, classes="lbl")
        with Vertical(classes="ctl"):
            yield from self.control()
            if self.help_text:
                yield Static(self.help_text, classes="help")

    def control(self) -> ComposeResult:
        yield from ()

    @property
    def value(self) -> Any:
        return get_value(self.path)

    def save(self, value: Any) -> None:
        self.screen.save(self.path, value)


class ToggleField(Field):
    def __init__(self, path: str, label: str, help: str = "", on_change: Callable[[bool], None] | None = None,
                 value: bool | None = None) -> None:
        super().__init__(path, label, help)
        self.on_change, self.initial = on_change, value

    def control(self) -> ComposeResult:
        yield Switch(value=bool(self.value if self.initial is None else self.initial), animate=False)

    @on(Switch.Changed)
    def _changed(self, e: Switch.Changed) -> None:
        e.stop()
        if self.on_change:
            self.on_change(e.value)
        else:
            self.save(e.value)


class TextField(Field):
    def __init__(self, path: str, label: str, help: str = "", placeholder: str = "",
                 number: tuple[int, int] | None = None, check: Callable[[str], str | None] | None = None) -> None:
        super().__init__(path, label, help)
        self.placeholder, self.number, self.check = placeholder, number, check

    def control(self) -> ComposeResult:
        v = self.value
        yield TextInput(value="" if v is None else str(v), placeholder=self.placeholder,
                    type="integer" if self.number else "text")

    def _commit(self, raw: str) -> None:
        inp = self.query_one(Input)
        raw = raw.strip()
        if self.number:
            lo, hi = self.number
            if not raw.isdigit() or not lo <= int(raw) <= hi:
                inp.add_class("-bad")
                self.app.notify(f"{self.label_text}: {lo}–{hi}", title="⚠ OUT OF RANGE", severity="warning", timeout=4)
                return
            value: Any = int(raw)
        else:
            value = raw
            if self.check and (err := self.check(raw)):
                inp.add_class("-bad")
                self.app.notify(err, title=f"⚠ {self.label_text}", severity="warning", timeout=5)
                return
        inp.remove_class("-bad")
        if value != self.value:
            self.save(value)

    @on(Input.Submitted)
    def _submitted(self, e: Input.Submitted) -> None:
        e.stop()
        self._commit(e.value)

    @on(Input.Blurred)
    def _blurred(self, e: Input.Blurred) -> None:
        self._commit(e.value)


class TagField(Field):
    """A list of strings: removable chips plus an input to add more."""

    def __init__(self, path: str, label: str, help: str = "", placeholder: str = "add…",
                 normalize: Callable[[str], str] = str.strip, check: Callable[[str], str | None] | None = None) -> None:
        super().__init__(path, label, help)
        self.placeholder, self.normalize, self.check = placeholder, normalize, check

    def control(self) -> ComposeResult:
        yield HorizontalGroup(classes="chips")
        yield TextInput(placeholder=self.placeholder)

    def on_mount(self) -> None:
        self._chips()

    def _chips(self) -> None:
        box = self.query_one(".chips", HorizontalGroup)
        box.remove_children()
        values = list(self.value or [])
        if not values:
            box.mount(Static(_txt("(none)", px.DIM)))
        for i, v in enumerate(values):
            box.mount(Button(f"{v} ×", classes="chip", name=str(i)))

    @on(Button.Pressed, ".chip")
    def _remove(self, e: Button.Pressed) -> None:
        e.stop()
        values = list(self.value or [])
        del values[int(e.button.name)]
        self.save(values)
        self._chips()

    @on(Input.Submitted)
    def _add(self, e: Input.Submitted) -> None:
        e.stop()
        raw = self.normalize(e.value)
        if not raw:
            return
        if self.check and (err := self.check(raw)):
            self.app.notify(err, title=f"⚠ {self.label_text}", severity="warning", timeout=5)
            return
        values = list(self.value or [])
        if raw not in values:
            self.save(values + [raw])
        e.input.value = ""
        self._chips()


class ChoiceField(Field):
    """Exactly one of a few options (saved as its key)."""

    def __init__(self, path: str, label: str, options: list[tuple[str, str]], help: str = "") -> None:
        super().__init__(path, label, help)
        self.options = options

    def control(self) -> ComposeResult:
        from textual.widgets import RadioButton, RadioSet

        current = self.value
        with RadioSet():
            for key, text in self.options:
                yield RadioButton(text, value=key == current, name=key)

    def on_radio_set_changed(self, e) -> None:
        e.stop()
        if e.pressed.name and e.pressed.name != self.value:
            self.save(e.pressed.name)


class ChecksField(Field):
    """Pick from a fixed set; all ticked saves [] (= everything)."""

    def __init__(self, path: str, label: str, options: list[tuple[str, str]], help: str = "",
                 empty_means_all: bool = True) -> None:
        super().__init__(path, label, help)
        self.options, self.empty_means_all = options, empty_means_all

    def control(self) -> ComposeResult:
        chosen = set(self.value or [])
        with HorizontalGroup(classes="checks"):
            for key, text in self.options:
                yield Checkbox(text, value=(not chosen and self.empty_means_all) or key in chosen, name=key)

    @on(Checkbox.Changed)
    def _changed(self, e: Checkbox.Changed) -> None:
        e.stop()
        picked = [cb.name for cb in self.query(Checkbox) if cb.value]
        if self.empty_means_all and len(picked) == len(self.options):
            picked = []
        self.save(picked)


def Sub(title: str) -> Static:
    return Static(f" {title} ", classes="st-sub")


def Actions(*buttons: Button) -> Horizontal:
    return Horizontal(*buttons, classes="actions")


# ── validators ───────────────────────────────────────────────────────────────


def _repo_name(v: str) -> str | None:
    return None if re.fullmatch(r"[\w.-]+/[\w.-]+", v) else "use owner/repo"


def _git_path(v: str) -> str | None:
    p = Path(v).expanduser()
    if not p.is_dir():
        return f"{v} isn't a folder"
    return None if (p / ".git").exists() else f"{v} isn't a git checkout"


def _url(v: str) -> str | None:
    return None if not v or re.match(r"https?://", v) else "start with http:// or https://"


def _sender(v: str) -> str | None:
    return None if re.fullmatch(r"[\w.+-]+(@[\w.-]+)?|@?[\w-]+(\.[\w-]+)+", v) else "a domain (github.com) or an address"


REASONS = [
    ("review_requested", "review"), ("mention", "@you"), ("team_mention", "@team"), ("author", "author"),
    ("comment", "comment"), ("assign", "assign"), ("ci_activity", "CI"), ("state_change", "state"),
    ("subscribed", "watching"), ("security_alert", "security"),
]


# ── env editor ───────────────────────────────────────────────────────────────


class EnvScreen(ModalScreen[dict | None]):
    from .setup import SETUP_CSS as CSS  # buttons / inputs look like the other setup forms
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, env: dict | None) -> None:
        super().__init__()
        self.env = dict(env or {})

    def compose(self) -> ComposeResult:
        e = self.env
        with VerticalScroll() as box:
            box.border_title = " ⚙ RELEASE ENVIRONMENT "
            yield Static(_txt("Version comes from ONE of: a deploy workflow's newest run, or the newest commit "
                              "that pins `app_image -> <tag>` in a file.", px.DIM))
            for key, label, ph in (
                ("name", "NAME", "PROD"),
                ("workflow", "DEPLOY WORKFLOW", "deploy-test.yml   (leave empty to use a pin file)"),
                ("pin_file", "PIN FILE", "deploy/production.tfvars"),
                ("health", "HEALTH URL", "https://app.example.com/api/health"),
                ("app_url", "APP URL", "https://app.example.com"),
            ):
                yield Label(label, classes="field-label")
                yield TextInput(value=str(e.get(key, "")), placeholder=ph, id=f"env-{key}")
            yield Checkbox("show drift / cadence / uptime stats for this env", value=bool(e.get("stats")), id="env-stats")
            yield Static("", id="env-status")
            with Horizontal(classes="buttons"):
                yield Button("CANCEL", id="cancel")
                yield Button("SAVE", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#env-name", Input).focus()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        v = {k: self.query_one(f"#env-{k}", Input).value.strip()
             for k in ("name", "workflow", "pin_file", "health", "app_url")}
        st = self.query_one("#env-status", Static)
        if not v["name"]:
            st.update(_bad("give it a name"))
            return
        if bool(v["workflow"]) == bool(v["pin_file"]):
            st.update(_bad("set exactly one of DEPLOY WORKFLOW or PIN FILE"))
            return
        for k in ("health", "app_url"):
            if err := _url(v[k]):
                st.update(_bad(f"{k}: {err}"))
                return
        env = {k: val for k, val in v.items() if val}
        if self.query_one("#env-stats", Checkbox).value:
            env["stats"] = True
        self.dismiss(env)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ── the screen ───────────────────────────────────────────────────────────────

SECTIONS = [
    ("github", "GitHub"), ("outlook", "Microsoft 365"), ("mail", "Mail filters"), ("imap", "IMAP inboxes"),
    ("apple_mail", "Apple Mail"), ("slack", "Slack"), ("linear", "Linear"), ("claude", "Claude"),
    ("releases", "Releases"), ("system", "System"), ("assist", "Reply assist"),
]

# what "reset section" clears, so built-in defaults apply again
RESET = {
    "github": ["github.repos", "github.inbox_repos", "github.ci_repos", "github.ci_branch", "github.inbox_reasons",
               "refresh.github"],
    "outlook": ["outlook.client_id", "outlook.authority", "refresh.mail", "refresh.calendar"],
    "mail": ["mail.hide_from", "mail.hide_subjects", "mail.ack_on_open"],
    "slack": ["slack.hide_apps", "slack.starred", "slack.dm_days", "refresh.slack"],
    "linear": ["linear.scope", "linear.states", "linear.sort", "linear.limit", "linear.teams",
               "linear.hide_states", "linear.priorities", "linear.new_days",
               "linear.include_backlog", "linear.include_triage", "refresh.linear"],
    "claude": ["claude.warn_pct"],
    "releases": ["refresh.releases"],
    "system": ["system.docker", "system.meters", "refresh.repos"],
    "assist": ["assist.url", "assist.model", "assist.tone", "assist.name", "assist.domain"],
}


class SettingsScreen(Screen):
    CSS = SETTINGS_CSS + """
    .field-label { color: #4cc9f0; text-style: bold; margin-top: 1; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+z", "undo", "Undo"),
        Binding("ctrl+o", "open_file", "Open config.toml"),
    ]

    def __init__(self, section: str | None = None) -> None:
        super().__init__()
        self.section = section or "github"
        self.undo: list[list[tuple[str, Any]]] = []  # each entry: one user action, possibly several keys
        self.changed = False

    def compose(self) -> ComposeResult:
        yield Static(id="st-head")
        with Horizontal(id="st-body"):
            with Vertical(id="st-nav"):
                yield OptionList(id="st-list")
            yield VerticalScroll(id="st-form")
            yield Static(id="st-side")
        yield Footer()

    def on_mount(self) -> None:
        head = Text()
        head.append(" ⚙ SETTINGS ", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
        head.append(f"  {CONFIG_PATH}".replace(str(Path.home()), "~"), Style(color=px.DIM))
        head.append("\nchanges save as you make them · ctrl+z undo · ctrl+o open the file · esc back",
                    Style(color=px.DIM))
        self.query_one("#st-head", Static).update(head)
        self._nav()
        lst = self.query_one("#st-list", OptionList)
        lst.highlighted = lst.get_option_index(self.section)
        lst.focus()
        self._show(self.section)

    # navigation ---------------------------------------------------------------

    def _nav(self) -> None:
        lst = self.query_one("#st-list", OptionList)
        keep = lst.highlighted
        opts: list[Option] = [Option(_txt("CONNECTIONS", px.CYAN, True), disabled=True)]
        for key, name in SECTIONS:
            if key == "mail":
                on_, note = True, f"{len(get_value('mail.hide_from') or [])} hidden senders"
            elif key == "system":
                on_, note = True, f"{len(get_value('system.repos') or [])} repos"
            elif key == "assist":
                on_ = bool(get_value("assist.enabled"))
                note = (get_value("assist.model") or "auto model") if on_ else "off"
            else:
                on_, note = status(key)
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(" ✔ " if on_ else " ✖ ", Style(color=px.VOID, bgcolor=px.CYAN if on_ else px.PINK, bold=True))
            t.append(f" {name}", Style(color=px.INK, bold=True))
            t.append(f"\n    {note[:26]}", Style(color=px.DIM))
            opts.append(Option(t, id=key))
        lst.set_options(opts)
        if keep is not None:
            lst.highlighted = keep

    @on(OptionList.OptionHighlighted, "#st-list")
    def _picked(self, e: OptionList.OptionHighlighted) -> None:
        if e.option.id and e.option.id != self.section:
            self._show(e.option.id)

    @on(OptionList.OptionSelected, "#st-list")
    def _enter(self, e: OptionList.OptionSelected) -> None:
        self._show(e.option.id)
        first = next(iter(self.query_one("#st-form").query("Input, Switch, Button, Checkbox")), None)
        if first:
            first.focus()

    def _show(self, key: str) -> None:
        self.section = key
        self.run_worker(self._build(key), exclusive=True, group="form")

    async def _build(self, key: str) -> None:
        form = self.query_one("#st-form", VerticalScroll)
        await form.remove_children()
        name = dict(SECTIONS)[key]
        widgets: list[Widget] = [Static(_txt(name.upper(), px.HOTPINK, True), classes="st-title"),
                                 Static("", id="st-status", classes="st-status")]
        widgets += getattr(self, f"_form_{key}")()
        if key in RESET:
            widgets.append(Actions(Button("RESET SECTION", id="reset", variant="error")))
        await form.mount_all(widgets)
        form.scroll_home(animate=False)
        self.query_one("#st-side", Static).update("")
        self.run_worker(self._side(key), exclusive=True, group="side")

    def _status(self, text: Text) -> None:
        try:
            self.query_one("#st-status", Static).update(text)
        except Exception:
            pass

    def _side_text(self, text: Text | str) -> None:
        self.query_one("#st-side", Static).update(text)

    # saving -------------------------------------------------------------------

    def save(self, path: str, value: Any) -> None:
        old = file_value(path)
        try:
            set_value(path, value)
        except Exception as exc:
            self.notify(str(exc)[:200], title="⚠ NOT SAVED", severity="error")
            return
        self.undo.append([(path, old)])
        self.changed = True
        self._nav()
        self.notify(f"{path}", title="✔ SAVED", timeout=1.5)
        self.run_worker(self._side(self.section), exclusive=True, group="side")

    def action_undo(self) -> None:
        if not self.undo:
            self.notify("nothing to undo", timeout=2)
            return
        group = self.undo.pop()
        for path, old in reversed(group):
            if old is _MISSING:
                delete_value(path)
            else:
                set_value(path, old)
        self.notify(", ".join(p for p, _ in group), title="↶ UNDONE", timeout=2)
        self._nav()
        self._show(self.section)

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        group = []
        for path in RESET.get(self.section, []):
            old = file_value(path)
            if old is not _MISSING:
                delete_value(path)
                group.append((path, old))
        if group:
            self.undo.append(group)
        self.changed = True
        self.notify("defaults restored · ctrl+z to undo", title=dict(SECTIONS)[self.section], timeout=3)
        self._nav()
        self._show(self.section)

    def secrets_changed(self) -> None:
        """Keychain changes don't touch config.toml, so tell the dashboard directly."""
        self.changed = True
        done = getattr(self.app, "_setup_done", None)
        if done:
            done(True)
        self._nav()
        self._show(self.section)

    def action_open_file(self) -> None:
        subprocess.Popen(["open", "-t", str(CONFIG_PATH)])

    def action_back(self) -> None:
        if self.focused and self.focused.id != "st-list":
            self.query_one("#st-list", OptionList).focus()
        else:
            self.dismiss(self.changed)

    def _after_setup(self, saved: bool | None) -> None:
        if saved:
            self.secrets_changed()

    # ── sections ─────────────────────────────────────────────────────────────

    def _form_github(self) -> list[Widget]:
        mon, inbox = len(get_value("github.repos") or []), len(get_value("github.inbox_repos") or [])
        return [
            Sub("REPOS"),
            Static(_txt(f"MONITOR {mon or 'all'} repos (review queue, my PRs, CI) · INBOX {inbox or 'all'} repos")),
            Actions(Button("PICK REPOS…", id="gh-pick", variant="primary")),
            Sub("CI PIPELINE"),
            TagField("github.ci_repos", "CI REPOS", "empty = the first 8 monitored repos",
                     "owner/repo", check=_repo_name),
            TextField("github.ci_branch", "CI BRANCH", "", "main"),
            Sub("INBOX"),
            ChecksField("github.inbox_reasons", "SHOW", REASONS, "which notification types reach GITHUB INBOX"),
            TextField("refresh.github", "REFRESH (s)", "30–3600", number=(30, 3600)),
        ]

    @on(Button.Pressed, "#gh-pick")
    def _gh_pick(self) -> None:
        self.app.push_screen(GitHubScreen(), lambda ok: (self._nav(), self._show("github")) if ok else None)

    def _form_outlook(self) -> list[Widget]:
        return [
            Sub("ACCOUNT"),
            Actions(Button("SIGN IN", id="ol-in", variant="primary"), Button("ALLOW SENDING", id="ol-send"),
                    Button("SIGN OUT", id="ol-out", variant="error")),
            ToggleField("outlook.enabled", "ENABLED", "mail + calendar tiles use Microsoft 365"),
            TextField("refresh.mail", "MAIL REFRESH (s)", "30–3600", number=(30, 3600)),
            TextField("refresh.calendar", "CALENDAR REFRESH (s)", "60–3600", number=(60, 3600)),
            Sub("ADVANCED"),
            TextField("outlook.client_id", "APP (CLIENT) ID", "Microsoft's public Graph CLI app by default"),
            TextField("outlook.authority", "AUTHORITY", "…/organizations, or …/<tenant-id>", check=_url),
        ]

    @on(Button.Pressed, "#ol-in")
    def _ol_in(self) -> None:
        self.app.push_screen(OutlookScreen(), self._after_setup)

    @on(Button.Pressed, "#ol-send")
    def _ol_send(self) -> None:
        self.app.push_screen(OutlookScreen(extra_scopes=["Mail.Send"]), self._after_setup)

    @on(Button.Pressed, "#ol-out")
    def _ol_out(self) -> None:
        from .sources.mail import CACHE_PATH

        CACHE_PATH.unlink(missing_ok=True)
        self.save("outlook.enabled", False)
        self.secrets_changed()
        self.notify("signed out of Microsoft 365", timeout=3)

    def _form_mail(self) -> list[Widget]:
        return [
            Sub("HIDE"),
            TagField("mail.hide_from", "SENDERS", "github.com hides the domain and its subdomains; "
                     "an address hides one sender", "domain or address", normalize=lambda s: s.strip().lower(),
                     check=_sender),
            TagField("mail.hide_subjects", "SUBJECTS", "case-insensitive text, e.g. [JIRA]", "text in subject"),
            Sub("READING"),
            ToggleField("mail.ack_on_open", "OPEN = ACKNOWLEDGED", "opening an email mutes it and drops it from unread"),
        ]

    def _form_imap(self) -> list[Widget]:
        out: list[Widget] = [Sub("ACCOUNTS")]
        accts = get_value("imap") or []
        if not accts:
            out.append(Static(_txt("no IMAP inboxes", px.DIM)))
        for i, a in enumerate(accts):
            secret = a.get("secret", f"imap-{a.get('name', a['user'])}")
            out.append(Horizontal(
                Static(Text.assemble((a.get("name", "?"), Style(color=px.INK, bold=True)),
                                     (f"  {a['user']} @ {a['host']} · password {mask(get_secret(secret))}",
                                      Style(color=px.DIM)))),
                Button("TEST", name=str(i), classes="imap-test"),
                Button("REMOVE", name=str(i), classes="imap-del", variant="error"),
                classes="envrow"))
        out.append(Actions(Button("ADD INBOX", id="imap-add", variant="primary")))
        return out

    @on(Button.Pressed, "#imap-add")
    def _imap_add(self) -> None:
        self.app.push_screen(FormScreen(SERVICES["imap"]), self._after_setup)

    @on(Button.Pressed, ".imap-del")
    def _imap_del(self, e: Button.Pressed) -> None:
        accts = list(file_value("imap") or [])
        a = accts.pop(int(e.button.name))
        delete_secret(a.get("secret", f"imap-{a.get('name', a['user'])}"))
        if accts:
            self.save("imap", accts)
        else:
            delete_value("imap")
            self.undo.append([("imap", [a])])
        self.secrets_changed()

    @on(Button.Pressed, ".imap-test")
    def _imap_test(self, e: Button.Pressed) -> None:
        a = (get_value("imap") or [])[int(e.button.name)]
        self.run_worker(self._imap_check(a), group="test")

    async def _imap_check(self, a: dict) -> None:
        from .setup import check_imap

        self._status(_busy(f"logging in to {a['host']}…"))
        pw = get_secret(a.get("secret", f"imap-{a.get('name', a['user'])}")) or ""
        try:
            self._status(_ok(await check_imap({**a, "password": pw})))
        except Exception as exc:
            self._status(_bad(str(exc)[:200]))

    def _form_apple_mail(self) -> list[Widget]:
        return [
            ToggleField("apple_mail.enabled", "ENABLED", "reads Mail.app's unified inbox via AppleScript"),
            Static(_txt("Only turn this on for accounts Microsoft 365 / IMAP don't already cover: a mirrored "
                        "inbox adds 20–40s per refresh, shows mail twice and lets hidden senders through "
                        "(Mail.app often omits Exchange addresses).", px.YELLOW)),
            Actions(Button("TEST", id="am-test")),
        ]

    @on(Button.Pressed, "#am-test")
    def _am_test(self) -> None:
        async def go() -> None:
            from .setup import check_apple_mail

            self._status(_busy("asking Mail.app…"))
            try:
                self._status(_ok(await check_apple_mail({})))
            except Exception as exc:
                self._status(_bad(str(exc)[:200]))

        self.run_worker(go(), group="test")

    def _form_slack(self) -> list[Widget]:
        tok = get_secret("slack", env="SLACK_TOKEN")
        return [
            Sub("TOKEN"),
            Static(Text.assemble(("user token ", Style(color=px.DIM)), (mask(tok), Style(color=px.INK, bold=True)))),
            Actions(Button("REPLACE TOKEN", id="sl-replace", variant="primary"), Button("COPY MANIFEST", id="sl-manifest"),
                    Button("REMOVE TOKEN", id="sl-remove", variant="error")),
            Sub("WHAT SHOWS"),
            ToggleField("slack.hide_apps", "HIDE APPS", "app DMs, app posts and app mentions stay off the dashboard"),
            TagField("slack.starred", "STARRED", "come right after DMs · also toggled with * in the Slack console",
                     "#channel", normalize=lambda s: ("#" + s.strip().lstrip("#")).lower() if s.strip() else ""),
            TextField("slack.dm_days", "HIDE DMs QUIET FOR (days)", "older DMs drop off the lists but stay "
                      "findable by name in the console · unread always shows · 0 = never hide", number=(0, 365)),
            TextField("refresh.slack", "REFRESH (s)", "20–600 (the tile caps at 30)", number=(20, 600)),
        ]

    @on(Button.Pressed, "#sl-replace")
    def _sl_replace(self) -> None:
        self.app.push_screen(FormScreen(SERVICES["slack"]), self._after_setup)

    @on(Button.Pressed, "#sl-manifest")
    def _sl_manifest(self) -> None:
        from importlib import resources

        subprocess.run(["pbcopy"], input=resources.files("pixeldeck").joinpath("slack-manifest.yml").read_bytes())
        self._status(_ok("manifest copied: paste it under App Manifest (YAML), then reinstall"))

    @on(Button.Pressed, "#sl-remove")
    def _sl_remove(self) -> None:
        delete_secret("slack")
        self.secrets_changed()
        self.notify("Slack token removed from the keychain", timeout=3)

    def _form_linear(self) -> list[Widget]:
        return [
            Sub("KEY"),
            Static(Text.assemble(("personal API key ", Style(color=px.DIM)),
                                 (mask(get_secret("linear", env="LINEAR_API_KEY")), Style(color=px.INK, bold=True)))),
            Actions(Button("REPLACE KEY", id="ln-replace", variant="primary"),
                    Button("REMOVE KEY", id="ln-remove", variant="error")),
            Sub("WHAT SHOWS"),
            ChoiceField("linear.scope", "WHICH ISSUES", [(k, v[0]) for k, v in LINEAR_SCOPES.items()]),
            ChecksField("linear.states", "STATES", LINEAR_STATES, "untick backlog to keep the list short",
                        empty_means_all=False),
            ChoiceField("linear.sort", "SORT", list(LINEAR_SORTS.items())),
            Actions(Button("FILTER BY STATUS / PRIORITY…", id="ln-filter")),
            TextField("linear.limit", "SHOW UP TO", "rows in the tile (the title counts all of them)", number=(10, 1000)),
            TextField("linear.new_days", "NEW FOR (days)", "tickets assigned to you this recently get a NEW tag "
                      "and show even if the filter hides them, until you open one; 0 = off", number=(0, 60)),
            TagField("linear.teams", "TEAMS", "team keys like ENG; empty = every team (for 'whole team': the teams you're on)",
                     "ENG", normalize=lambda s: s.strip().upper()),
            TextField("refresh.linear", "REFRESH (s)", "60–3600", number=(60, 3600)),
        ]

    @on(Button.Pressed, "#ln-replace")
    def _ln_replace(self) -> None:
        self.app.push_screen(FormScreen(SERVICES["linear"]), self._after_setup)

    @on(Button.Pressed, "#ln-filter")
    def _ln_filter(self) -> None:
        from .linearui import LinearFilter

        tile = self.app.query_one("#linear")
        facets = (tile.feed.extra or {}).get("facets") if getattr(tile, "feed", None) else None
        self.app.push_screen(LinearFilter(facets), lambda ok: ok and (self._nav(), self._show("linear")))

    @on(Button.Pressed, "#ln-remove")
    def _ln_remove(self) -> None:
        delete_secret("linear")
        self.secrets_changed()

    def _form_claude(self) -> list[Widget]:
        from .sources import claude as c

        def flip(on_: bool) -> None:
            try:
                msg = c.install_wrapper() if on_ else c.uninstall_wrapper()
                self._status(_ok(msg))
            except Exception as exc:
                self._status(_bad(str(exc)[:200]))
            self._nav()

        return [
            Sub("PLAN LIMITS"),
            ToggleField("claude._limits", "LIMITS FROM STATUS LINE", "wraps your Claude Code status-line command "
                        "(Orca's keeps working); settings.json.pixeldeck-bak is the backup",
                        on_change=flip, value=c.wrapper_installed()),
            TextField("claude.warn_pct", "WARN AT (%)", "bars turn red from here", number=(50, 100)),
            Sub("USAGE"),
            Static(_txt("Token totals come from ~/.claude/projects and need no setup.", px.DIM)),
        ]

    def _form_releases(self) -> list[Widget]:
        out: list[Widget] = [
            Sub("SOURCE"),
            TextField("releases.repo", "REPO", "versions are read from this repo's runs and commits", "owner/repo",
                      check=lambda v: _repo_name(v) if v else None),
            TextField("releases.branch", "BRANCH", "", "main"),
            Sub("ENVIRONMENTS"),
        ]
        for i, e in enumerate(get_value("releases.env") or []):
            src = f"workflow {e['workflow']}" if e.get("workflow") else f"pins {e.get('pin_file', '?')}"
            out.append(Horizontal(
                Static(Text.assemble((f"{e.get('name', '?'):<9}", Style(color=px.INK, bold=True)),
                                     (f"{src} · {e.get('health') or 'no health check'}", Style(color=px.DIM)),
                                     ("  ★ stats" if e.get("stats") else "", Style(color=px.HOTPINK)))),
                Button("EDIT", name=str(i), classes="env-edit"),
                Button("TEST", name=str(i), classes="env-test"),
                Button("REMOVE", name=str(i), classes="env-del", variant="error"),
                classes="envrow"))
        out += [Actions(Button("ADD ENVIRONMENT", id="env-add", variant="primary")),
                TextField("refresh.releases", "REFRESH (s)", "30–3600", number=(30, 3600))]
        return out

    def _envs(self) -> list[dict]:
        v = file_value("releases.env")
        return [dict(e) for e in v] if isinstance(v, list) else []

    def _save_envs(self, envs: list[dict]) -> None:
        if envs:
            self.save("releases.env", envs)
        else:
            old = file_value("releases.env")
            delete_value("releases.env")
            self.undo.append([("releases.env", old)])
            self.changed = True
        self._show("releases")

    @on(Button.Pressed, "#env-add")
    def _env_add(self) -> None:
        self.app.push_screen(EnvScreen(None), lambda e: e and self._save_envs(self._envs() + [e]))

    @on(Button.Pressed, ".env-edit")
    def _env_edit(self, ev: Button.Pressed) -> None:
        i = int(ev.button.name)
        envs = self._envs()

        def done(e: dict | None) -> None:
            if e:
                envs[i] = e
                self._save_envs(envs)

        self.app.push_screen(EnvScreen(envs[i]), done)

    @on(Button.Pressed, ".env-del")
    def _env_del(self, ev: Button.Pressed) -> None:
        envs = self._envs()
        envs.pop(int(ev.button.name))
        self._save_envs(envs)

    @on(Button.Pressed, ".env-test")
    def _env_test(self, ev: Button.Pressed) -> None:
        env = (get_value("releases.env") or [])[int(ev.button.name)]

        async def go() -> None:
            from .sources.releases import Releases

            self._status(_busy(f"checking {env.get('name')}…"))
            try:
                feed = await Releases({**get_value("releases"), "env": [{**env, "stats": False}]}).pipeline()
                row = feed.items[0]
                (self._status(_bad(f"{row.title.strip()} · {row.meta}")) if row.tone == "error"
                 else self._status(_ok(f"{row.title.strip()} · {row.meta}")))
            except Exception as exc:
                self._status(_bad(str(exc)[:200]))

        self.run_worker(go(), group="test")

    def _form_system(self) -> list[Widget]:
        return [
            Sub("STRIP"),
            ChecksField("system.meters", "METERS", [("cpu", "CPU"), ("mem", "memory"), ("net", "network"),
                                                     ("bat", "battery")], empty_means_all=False),
            ToggleField("system.docker", "DOCKER", "running / unhealthy container counts"),
            Sub("LOCAL REPOS"),
            TagField("system.repos", "REPOS", "branch, changed files, ahead/behind and worktrees",
                     "~/path/to/repo", check=_git_path),
            TextField("refresh.repos", "REFRESH (s)", "10–600", number=(10, 600)),
        ]

    def _form_assist(self) -> list[Widget]:
        from .assist import STYLE_PATH, Style as Tone

        st = Tone.load()
        tone = Text()
        if st.built_at:
            tone.append(f"learned from {len(st.slack)} Slack messages and {len(st.email)} sent emails\n\n",
                        Style(color=px.DIM))
            tone.append(st.notes, Style(color=px.INK))
        else:
            tone.append("not learned yet: suggestions use a generic casual tone", Style(color=px.YELLOW))
        return [
            Sub("SUGGESTIONS"),
            ToggleField("assist.enabled", "ENABLED", "pink suggestions in the Slack box and email replies; → takes them"),
            TextField("assist.url", "SERVER", "LM Studio's local server (OpenAI-compatible)", "http://localhost:1234/v1",
                      check=self._check_url),
            TextField("assist.model", "MODEL", "empty = pick an instruct model automatically · models listed on the right",
                      "ministral-3-8b-instruct-2512"),
            Actions(Button("TEST", id="as-test", variant="primary"), Button("LOAD / SWITCH MODEL…", id="as-models"),
                    Button("WARM UP", id="as-warm")),
            Sub("YOUR TONE"),
            TextField("assist.tone", "IN A WORD", "added to every prompt", "keep it casual"),
            TextField("assist.name", "YOUR NAME", "empty = from your Microsoft 365 account"),
            TextField("assist.domain", "COMPANY DOMAIN", "mail outside it stays casual-professional (no slang)",
                      "example.com"),
            Static(tone, classes="st-status"),
            Actions(Button("LEARN MY TONE", id="as-learn", variant="primary"), Button("EDIT NOTES", id="as-edit"),
                    Button("FORGET", id="as-forget", variant="error")),
            Static(_txt(f"stored privately in {STYLE_PATH}".replace(str(Path.home()), "~"), px.DIM)),
        ]

    def _check_url(self, v: str) -> str | None:
        from .assist import is_local

        if err := _url(v):
            return err
        if v and not is_local(v) and not get_value("assist.allow_remote"):
            return "not a local address: your messages would leave this machine (set assist.allow_remote to allow)"
        return None

    def _assist(self):
        from .assist import Assist

        a = Assist(get_value("assist") or {})
        own = getattr(self.app, "assist", None)
        if own:
            a.identity = dict(own.identity)
        return a

    @on(Button.Pressed, "#as-test")
    def _as_test(self) -> None:
        async def go() -> None:
            a = self._assist()
            a.cfg = {**a.cfg, "enabled": True}
            self._status(_busy("asking the model…"))
            ctx = "Slack conversation in @dana, oldest first:\ndana: got a minute for the board deck before 3?"
            s = await a.suggest("slack", ctx, "")
            if s:
                self._status(_ok(f"{a.last_ms:.0f}ms · “{s.strip()}”"))
            else:
                self._status(_bad(a.last_error or "no suggestion came back"))

        self.run_worker(go(), group="test")

    @on(Button.Pressed, "#as-models")
    def _as_models(self) -> None:
        from .llmui import ModelScreen

        self.app.push_screen(ModelScreen(), lambda changed: changed and self._show("assist"))

    @on(Button.Pressed, "#as-warm")
    def _as_warm(self) -> None:
        async def go() -> None:
            a = self._assist()
            self._status(_busy("loading the model in LM Studio…"))
            await a.warm()
            self._status(_bad(a.last_error) if a.last_error else _ok(f"ready · {a.last_ms:.0f}ms"))

        self.run_worker(go(), group="test")

    @on(Button.Pressed, "#as-learn")
    def _as_learn(self) -> None:
        async def go() -> None:
            a = self._assist()
            self._status(_busy("reading your recent Slack messages and sent mail, then asking the model…"))
            try:
                st = await a.learn(self.app)
            except Exception as exc:
                self._status(_bad(str(exc)[:200]))
                return
            if getattr(self.app, "assist", None):
                self.app.assist.style = st
            self._show("assist")
            self.notify(f"learned from {len(st.slack)} messages and {len(st.email)} emails", title="✔ TONE", timeout=3)

        self.run_worker(go(), group="test")

    @on(Button.Pressed, "#as-edit")
    def _as_edit(self) -> None:
        from .assist import STYLE_PATH

        if STYLE_PATH.exists():
            subprocess.Popen(["open", "-t", str(STYLE_PATH)])
        else:
            self.notify("learn your tone first", timeout=3)

    @on(Button.Pressed, "#as-forget")
    def _as_forget(self) -> None:
        from .assist import STYLE_PATH, Style as Tone

        STYLE_PATH.unlink(missing_ok=True)
        if getattr(self.app, "assist", None):
            self.app.assist.style = Tone()
        self._show("assist")

    # ── right-hand side: live checks / previews ──────────────────────────────

    async def _side(self, key: str) -> None:
        side = Text()
        side.append("CHECK\n", Style(color=px.CYAN, bold=True))
        try:
            if key == "github":
                login = (await asyncio.create_subprocess_exec(
                    "gh", "api", "user", "--jq", ".login", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)).stdout
                out = (await login.read()).decode().strip() if login else ""
                side.append_text(_ok(f"gh signed in as {out}") if out else _bad("gh isn't signed in"))
            elif key == "outlook":
                from .sources.mail import Outlook

                ol = Outlook(get_value("outlook"))
                accts = await asyncio.to_thread(lambda: ol._msal().get_accounts())
                if not accts:
                    side.append_text(_bad("not signed in"))
                else:
                    side.append_text(_ok(accts[0].get("username", "signed in")))
                    can_send = await asyncio.to_thread(ol.token_for, ["Mail.Send"])
                    side.append("\n\nread mail + calendar ✔\nsend replies " + ("✔" if can_send else "✖ (ALLOW SENDING)"),
                                Style(color=px.INK))
            elif key == "mail":
                side.append_text(await self._mail_preview())
            elif key == "slack":
                from .sources.slackclient import SlackClient

                if not get_secret("slack", env="SLACK_TOKEN"):
                    side.append_text(_bad("no token"))
                else:
                    sc = SlackClient({})
                    d = await sc.call("auth.test")
                    from importlib import resources

                    want = set(re.findall(r"^\s+- ([\w:.]+)",
                                          resources.files("pixeldeck").joinpath("slack-manifest.yml").read_text(), re.M))
                    missing = sorted(want - sc.scopes)
                    side.append_text(_ok(f"@{d['user']} on {d['team']}"))
                    side.append(f"\n\npermissions {len(want) - len(missing)}/{len(want)}", Style(color=px.INK))
                    if missing:
                        side.append("\nmissing: " + ", ".join(missing), Style(color=px.YELLOW))
                    side.append(f"\nstarred: {len(get_value('slack.starred') or [])}", Style(color=px.INK))
            elif key == "linear":
                from .setup import check_linear

                key_ = get_secret("linear", env="LINEAR_API_KEY")
                side.append_text(_ok(await check_linear({"token": key_})) if key_ else _bad("no key"))
            elif key == "claude":
                from .sources import claude as c

                lim = c.read_limits()
                if c.wrapper_installed() and lim:
                    side.append_text(_ok(f"limits updated {int(lim.age // 60)}m ago"))
                    for w in lim.windows:
                        side.append(f"\n{w.name:<4} {w.pct:3.0f}%", Style(color=px.RED if w.pct >= int(
                            get_value('claude.warn_pct') or 80) else px.INK))
                elif c.wrapper_installed():
                    side.append_text(_busy("waiting for a Claude Code session"))
                else:
                    side.append_text(_bad("limits off (usage still shows)"))
            elif key == "releases":
                envs = get_value("releases.env") or []
                side.append_text(_ok(f"{len(envs)} environments") if envs else _bad("none configured"))
                side.append("\n\nTEST checks one env's version and health.", Style(color=px.DIM))
            elif key == "system":
                missing = [r for r in get_value("system.repos") or [] if _git_path(r)]
                side.append_text(_bad("not found: " + ", ".join(missing)) if missing else _ok("all repos found"))
                side.append(f"\n\ndocker: {'on PATH' if shutil.which('docker') else 'not installed'}", Style(color=px.INK))
            elif key == "assist":
                a = self._assist()
                try:
                    models = await a.models()
                    side.append_text(_ok(f"LM Studio answering · {len(models)} chat models"))
                    side.append("\n\n", Style())
                    current = get_value("assist.model") or ""
                    for m in models:
                        side.append(("▶ " if m == current else "  ") + m + "\n",
                                    Style(color=px.HOTPINK if m == current else px.INK))
                    side.append("\ncopy a name into MODEL to switch", Style(color=px.DIM))
                except Exception as exc:
                    side.append_text(_bad(f"no server at {a.url}: {type(exc).__name__}"))
                    side.append("\n\nstart LM Studio's server (Developer → Start Server)", Style(color=px.DIM))
            elif key in ("imap", "apple_mail"):
                ok, note = status(key)
                side.append_text(_ok(note) if ok else _bad(note))
        except Exception as exc:
            side.append_text(_bad(str(exc)[:200]))
        if self.section == key:
            self._side_text(side)

    async def _mail_preview(self) -> Text:
        """How much the current filters hide, measured on your last 100 Microsoft 365 messages."""
        from collections import Counter

        from .sources.mail import MailFilter, Outlook

        if not get_value("outlook.enabled"):
            return _bad("preview needs Microsoft 365")
        d = await Outlook(get_value("outlook"))._get(
            "/me/mailFolders/inbox/messages", {"$top": "100", "$select": "from,subject", "$orderby": "receivedDateTime desc"})
        flt = MailFilter(get_value("mail"))
        hits: Counter[str] = Counter()
        for m in d.get("value", []):
            addr = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
            if flt.hides(addr, m.get("subject", "")):
                hits[addr.rpartition("@")[2] or addr] += 1
        out = Text()
        out.append(f"hides {sum(hits.values())} of your last {len(d.get('value', []))}\n\n",
                   Style(color=px.HOTPINK, bold=True))
        for dom, n in hits.most_common(8):
            out.append(f"{n:4}  {dom}\n", Style(color=px.INK))
        return out
