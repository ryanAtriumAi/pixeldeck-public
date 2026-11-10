"""In-app setup wizard: walk through each source, test the key, store it."""

from __future__ import annotations

import asyncio
import imaplib
import re
import shutil
import subprocess
from dataclasses import dataclass, field

import httpx
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static

from .widgets import TextInput
from textual.widgets.option_list import Option

from . import pixel as px
from .sources.base import ago, parse_time
from .config import (
    append_config_block,
    get_secret,
    load_config,
    set_config_value,
    store_secret,
    toml_str,
)

SETUP_CSS = f"""
SetupMenu, FormScreen, OutlookScreen {{ align: center middle; background: {px.VOID} 70%; }}
.box {{
    width: 92; max-width: 95%; height: auto; max-height: 90%;
    background: {px.DEEP}; border: panel {px.HOTPINK};
    border-title-color: {px.VOID}; border-title-style: bold; padding: 1 2;
}}
.box OptionList {{ height: auto; max-height: 16; background: {px.DEEP}; border: none; }}
.box OptionList > .option-list--option-highlighted {{ background: #4a1a7a; }}
.steps {{ margin-bottom: 1; }}
.field-label {{ color: {px.CYAN}; text-style: bold; margin-top: 1; }}
.box Input {{ background: {px.PANEL}; border: tall {px.PURPLE}; }}
.box Input:focus {{ border: tall {px.HOTPINK}; }}
.status {{ margin-top: 1; height: auto; }}
.buttons {{ height: auto; margin-top: 1; align-horizontal: right; }}
.buttons Button {{ margin-left: 1; min-width: 14; }}
.box Button {{ background: {px.PANEL}; color: {px.INK}; border: tall {px.PURPLE}; }}
.box Button.-primary {{ background: {px.PINK}; color: {px.VOID}; border: tall {px.HOTPINK}; text-style: bold; }}
.box Button:focus {{ text-style: bold reverse; }}
#code {{ height: auto; margin: 1 0; }}
"""


def _ok(msg: str) -> Text:
    t = Text()
    t.append(" ✔ OK ", Style(color=px.VOID, bgcolor=px.CYAN, bold=True))
    t.append(f" {msg}", Style(color=px.INK))
    return t


def _bad(msg: str) -> Text:
    t = Text()
    t.append(" ✖ NOPE ", Style(color=px.VOID, bgcolor=px.RED, bold=True))
    t.append(f" {msg}", Style(color=px.INK))
    return t


def _busy(msg: str) -> Text:
    return Text(f"◷ {msg}", Style(color=px.YELLOW))


def _steps(lines: list[str]) -> Text:
    out = Text()
    for i, line in enumerate(lines, 1):
        if i > 1:
            out.append("\n")
        out.append(f" {i} ", Style(color=px.VOID, bgcolor=px.PURPLE, bold=True))
        out.append(f" {line}", Style(color=px.INK))
    return out


# ── Service definitions ──────────────────────────────────────────────────────


@dataclass
class FieldSpec:
    key: str
    label: str
    placeholder: str = ""
    password: bool = False
    default: str = ""


@dataclass
class Service:
    key: str
    name: str
    blurb: str
    steps: list[str] = field(default_factory=list)
    link: str | None = None
    fields: list[FieldSpec] = field(default_factory=list)


SLACK_MANIFEST_HINT = "user scopes: search:read (that's all PIXELDECK needs)"

SERVICES: dict[str, Service] = {
    "slack": Service(
        "slack",
        "SLACK",
        "Mentions and DMs, via a Slack user token.",
        [
            "Press COPY MANIFEST, open api.slack.com/apps → your app → App Manifest, paste it, Save.",
            "Install App → Install (or Reinstall) to Workspace, approve.",
            "Copy the User OAuth Token (starts xoxp-) and paste it below.",
            "It's checked against Slack, then kept in your macOS keychain. Only you can use it.",
        ],
        "https://api.slack.com/apps",
        [FieldSpec("token", "USER TOKEN", "xoxp-…", password=True)],
    ),
    "linear": Service(
        "linear",
        "LINEAR",
        "Issues assigned to you, via a personal API key.",
        [
            "Open Linear → Settings → Security & access → Personal API keys.",
            "Create a key (read access is enough) and copy it (starts lin_api_).",
            "Paste it below. It is checked against Linear, then kept in your macOS keychain.",
        ],
        "https://linear.app/settings/account/security",
        [FieldSpec("token", "API KEY", "lin_api_…", password=True)],
    ),
    "imap": Service(
        "imap",
        "IMAP MAIL",
        "Any inbox that speaks IMAP: Gmail, iCloud, Fastmail…",
        [
            "Create an app password (Gmail: myaccount.google.com/apppasswords; iCloud: appleid.apple.com).",
            "Fill in the server and login below. Gmail is imap.gmail.com, iCloud imap.mail.me.com.",
            "PIXELDECK logs in read-only to test it, then keeps the password in your keychain.",
        ],
        "https://myaccount.google.com/apppasswords",
        [
            FieldSpec("name", "NAME", "personal", default="personal"),
            FieldSpec("host", "IMAP SERVER", "imap.gmail.com", default="imap.gmail.com"),
            FieldSpec("user", "LOGIN", "you@example.com"),
            FieldSpec("password", "APP PASSWORD", "xxxx xxxx xxxx xxxx", password=True),
            FieldSpec("web_url", "OPEN IN (optional)", "https://mail.google.com", default="https://mail.google.com"),
        ],
    ),
    "apple_mail": Service(
        "apple_mail",
        "APPLE MAIL",
        "Read the unified inbox of Mail.app (whatever accounts it already has).",
        [
            "Mail.app must be set up with your accounts.",
            "Press TEST: macOS asks once to let your terminal control Mail. Click OK.",
        ],
    ),
}


def status(key: str) -> tuple[bool, str]:
    cfg = load_config()
    if key == "github":
        if not shutil.which("gh"):
            return False, "install gh, then `gh auth login`"
        g = cfg["github"]
        mon, inbox = len(g.get("repos", [])), len(g.get("inbox_repos", []))
        return True, f"watch {mon or 'all'} · inbox {inbox or 'all'}"
    if key == "slack":
        on_ = bool(get_secret("slack", env="SLACK_TOKEN"))
        return on_, "token in keychain" if on_ else "not connected"
    if key == "linear":
        on_ = bool(get_secret("linear", env="LINEAR_API_KEY"))
        return on_, "key in keychain" if on_ else "not connected"
    if key == "outlook":
        from .sources.mail import CACHE_PATH

        on_ = bool(cfg["outlook"].get("enabled") and CACHE_PATH.exists())
        return on_, "signed in" if on_ else "not connected"
    if key == "imap":
        n = len(cfg.get("imap", []))
        return n > 0, f"{n} account(s)" if n else "add an inbox"
    if key == "apple_mail":
        on_ = bool(cfg["apple_mail"].get("enabled"))
        return on_, "enabled" if on_ else "off"
    if key == "claude":
        from .sources import claude as c

        on_ = c.wrapper_installed()
        return on_, "limits on" if on_ else "usage on · limits off"
    if key == "releases":
        envs = cfg["releases"].get("env") or []
        on_ = bool(cfg["releases"].get("repo") and envs)
        return on_, f"{len(envs)} environments" if on_ else "not set up"
    return False, ""


MENU = [
    ("outlook", "MICROSOFT 365", "mail + calendar, sign in with a code"),
    ("slack", "SLACK", "mentions and DMs"),
    ("linear", "LINEAR", "your assigned issues"),
    ("imap", "IMAP MAIL", "Gmail / iCloud / Fastmail inbox"),
    ("apple_mail", "APPLE MAIL", "Mail.app inbox"),
    ("github", "GITHUB", "pick repos to monitor"),
    ("claude", "CLAUDE", "usage + plan limits"),
    ("releases", "RELEASES", "deploy pipeline from config.toml"),
]


# ── Validators / savers ──────────────────────────────────────────────────────


async def check_slack(v: dict) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {v['token']}"})
    data = r.json()
    if not data.get("ok"):
        raise ValueError(f"Slack said {data.get('error')}")
    if not v["token"].startswith("xoxp-"):
        raise ValueError("that's not a user token: use the User OAuth Token (xoxp-), not the bot token")
    have = {s.strip() for s in r.headers.get("x-oauth-scopes", "").split(",") if s.strip()}
    from importlib import resources

    manifest = resources.files("pixeldeck").joinpath("slack-manifest.yml").read_text()
    want = set(re.findall(r"^\s+- ([\w:.]+)", manifest, re.M))
    missing = sorted(want - have)
    note = f" · missing {', '.join(missing)} (re-paste the manifest, reinstall)" if missing else " · all scopes present"
    return f"signed in as @{data['user']} on {data['team']}{note}"


async def check_linear(v: dict) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            "https://api.linear.app/graphql",
            json={"query": "{ viewer { name email } }"},
            headers={"Authorization": v["token"], "Content-Type": "application/json"},
        )
    data = r.json()
    if data.get("errors") or not data.get("data"):
        raise ValueError((data.get("errors") or [{}])[0].get("message", f"HTTP {r.status_code}"))
    me = data["data"]["viewer"]
    return f"signed in as {me['name']} <{me['email']}>"


async def check_imap(v: dict) -> str:
    if not re.fullmatch(r"[\w\-]+", v["name"] or ""):
        raise ValueError("NAME must be letters, numbers, - or _")

    def login() -> str:
        conn = imaplib.IMAP4_SSL(v["host"], 993, timeout=20)
        try:
            conn.login(v["user"], v["password"])
            _, st = conn.status("INBOX", "(UNSEEN)")
            return st[0].decode()
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    try:
        info = await asyncio.to_thread(login)
    except imaplib.IMAP4.error as exc:
        raise ValueError(f"login refused: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"can't reach {v['host']}: {exc}") from exc
    unseen = re.search(r"UNSEEN (\d+)", info)
    return f"logged in, {unseen.group(1) if unseen else '?'} unread"


async def check_apple_mail(v: dict) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", 'tell application "Mail" to get unread count of inbox',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    if proc.returncode:
        raise ValueError(err.decode().strip().splitlines()[-1] if err else "Mail.app refused")
    return f"Mail.app answered: {out.decode().strip()} unread"


def save(key: str, v: dict) -> None:
    if key in ("slack", "linear"):
        store_secret(key, v["token"].strip())
    elif key == "imap":
        secret = f"imap-{v['name']}"
        store_secret(secret, v["password"])
        cfg = load_config()
        if not any(a.get("name") == v["name"] for a in cfg.get("imap", [])):
            block = "\n".join(
                [
                    "[[imap]]",
                    f"name = {toml_str(v['name'])}",
                    f"host = {toml_str(v['host'])}",
                    f"user = {toml_str(v['user'])}",
                    f"secret = {toml_str(secret)}",
                ]
                + ([f"web_url = {toml_str(v['web_url'])}"] if v.get("web_url") else [])
            )
            append_config_block(block)
    elif key == "apple_mail":
        set_config_value("apple_mail", "enabled", "true")


CHECKS = {"slack": check_slack, "linear": check_linear, "imap": check_imap, "apple_mail": check_apple_mail}


# ── Screens ──────────────────────────────────────────────────────────────────


class SetupMenu(ModalScreen[bool]):
    """Pick a source to connect."""

    CSS = SETUP_CSS
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self.changed = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="box") as box:
            box.border_title = " ⚙ SETUP · CONNECT YOUR STUFF "
            yield Static(
                Text("Pick a source. Keys are tested live, then stored in the macOS keychain;"
                     " config edits keep your comments.", Style(color=px.DIM)),
                classes="steps",
            )
            yield OptionList(id="menu")
            with Horizontal(classes="buttons"):
                yield Button("DONE", id="done", variant="primary")

    def on_mount(self) -> None:
        self._fill()
        self.query_one("#menu", OptionList).focus()

    def _fill(self) -> None:
        menu = self.query_one("#menu", OptionList)
        keep = menu.highlighted
        opts = []
        for key, name, desc in MENU:
            ok, note = status(key)
            t = Text(no_wrap=True)
            t.append(" ✔ " if ok else " ✖ ", Style(color=px.VOID, bgcolor=px.CYAN if ok else px.PINK, bold=True))
            t.append(f" {name:<15}", Style(color=px.INK, bold=True))
            t.append(f"{desc}", Style(color=px.DIM))
            t.append(f"  {note}", Style(color=px.CYAN if ok else px.YELLOW))
            opts.append(Option(t, id=key))
        menu.set_options(opts)
        menu.highlighted = keep if keep is not None else 0

    @on(OptionList.OptionSelected, "#menu")
    def _pick(self, event: OptionList.OptionSelected) -> None:
        key = event.option.id
        if key == "outlook":
            self.app.push_screen(OutlookScreen(), self._after)
        elif key in SERVICES:
            self.app.push_screen(FormScreen(SERVICES[key]), self._after)
        elif key == "github":
            if shutil.which("gh"):
                self.app.push_screen(GitHubScreen(), self._after)
            else:
                self.notify("Install the GitHub CLI and run `gh auth login`, then come back here.",
                            title="GITHUB", timeout=6)
        elif key == "claude":
            self.app.push_screen(ClaudeScreen(), self._after)
        elif key == "releases":
            from .config import CONFIG_PATH

            subprocess.Popen(["open", "-t", str(CONFIG_PATH)])
            self.notify("Opened config.toml: edit [releases], then press r on the dashboard.",
                        title="RELEASES", timeout=6)

    def _after(self, saved: bool | None) -> None:
        if saved:
            self.changed = True
        self._fill()

    @on(Button.Pressed, "#done")
    def action_close(self) -> None:
        self.dismiss(self.changed)


class FormScreen(ModalScreen[bool]):
    CSS = SETUP_CSS
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, svc: Service) -> None:
        super().__init__()
        self.svc = svc

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="box") as box:
            box.border_title = f" ⚙ CONNECT {self.svc.name} "
            yield Static(Text(self.svc.blurb, Style(color=px.HOTPINK, bold=True)), classes="steps")
            yield Static(_steps(self.svc.steps), classes="steps")
            for f in self.svc.fields:
                yield Label(f.label, classes="field-label")
                yield TextInput(value=f.default, placeholder=f.placeholder, password=f.password, id=f"f-{f.key}")
            yield Static("", id="status", classes="status")
            with Horizontal(classes="buttons"):
                if self.svc.key == "slack":
                    yield Button("COPY MANIFEST", id="manifest")
                if self.svc.link:
                    yield Button("OPEN PAGE ↗", id="open")
                yield Button("CANCEL", id="cancel")
                yield Button("TEST + SAVE", id="save", variant="primary")

    def on_mount(self) -> None:
        inputs = self.query(Input)
        (inputs.first() if inputs else self.query_one("#save", Button)).focus()

    def _values(self) -> dict:
        return {f.key: self.query_one(f"#f-{f.key}", Input).value.strip() for f in self.svc.fields}

    @on(Button.Pressed, "#manifest")
    def _manifest(self) -> None:
        from importlib import resources

        text = resources.files("pixeldeck").joinpath("slack-manifest.yml").read_text()
        subprocess.run(["pbcopy"], input=text.encode())
        self.query_one("#status", Static).update(_ok("manifest copied: paste it under App Manifest (YAML)"))

    @on(Button.Pressed, "#open")
    def _open(self) -> None:
        subprocess.Popen(["open", self.svc.link])

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        i = inputs.index(event.input)
        if i + 1 < len(inputs):
            inputs[i + 1].focus()
        else:
            self._save()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        values = self._values()
        missing = [f.label for f in self.svc.fields if not values[f.key] and "optional" not in f.label.lower()]
        if missing:
            self.query_one("#status", Static).update(_bad(f"fill in {', '.join(missing)}"))
            return
        self._run(values)

    @work(exclusive=True)
    async def _run(self, values: dict) -> None:
        st = self.query_one("#status", Static)
        btn = self.query_one("#save", Button)
        btn.disabled = True
        st.update(_busy(f"talking to {self.svc.name.lower()}…"))
        try:
            msg = await CHECKS[self.svc.key](values)
            await asyncio.to_thread(save, self.svc.key, values)
        except Exception as exc:
            st.update(_bad(str(exc)[:200]))
            btn.disabled = False
            return
        st.update(_ok(f"{msg} · saved"))
        await asyncio.sleep(1.2)
        self.dismiss(True)


class OutlookScreen(ModalScreen[bool]):
    """Microsoft device-code sign-in, entirely inside the TUI."""

    CSS = SETUP_CSS
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, extra_scopes: list[str] | None = None) -> None:
        super().__init__()
        self.flow: dict | None = None
        self.extra = extra_scopes or []

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="box") as box:
            cal = "Calendars.ReadWrite" in self.extra
            box.border_title = (" ⚙ CONNECT MICROSOFT 365 " if not self.extra else
                                " ⚙ ALLOW CALENDAR CHANGES " if cal else " ⚙ ALLOW SENDING MAIL ")
            blurb = ("Inbox + calendar via Microsoft Graph (read-only: Mail.Read, Calendars.Read)." if not self.extra else
                     "Creating events needs Calendars.ReadWrite. Microsoft asks you to approve just that, once."
                     if cal else "Replying needs Mail.Send. Microsoft asks you to approve just that, once.")
            yield Static(Text(blurb, Style(color=px.HOTPINK, bold=True)), classes="steps")
            yield Static(_steps([
                "Press START: PIXELDECK asks Microsoft for a one-time code.",
                "Your browser opens microsoft.com/devicelogin; the code is already on your clipboard.",
                "Sign in with your work account and approve. This window finishes by itself.",
            ]), classes="steps")
            yield Static("", id="code")
            yield Static("", id="status", classes="status")
            with Horizontal(classes="buttons"):
                yield Button("CANCEL", id="cancel")
                yield Button("START", id="start", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#start", Button).focus()

    @on(Button.Pressed, "#start")
    def _start(self) -> None:
        self.query_one("#start", Button).disabled = True
        self._signin()

    @work(exclusive=True)
    async def _signin(self) -> None:
        from .sources.mail import SCOPES, Outlook

        st = self.query_one("#status", Static)
        cfg = load_config()["outlook"]
        ol = Outlook(cfg)
        app = ol._msal()
        st.update(_busy("asking Microsoft for a code…"))
        flow = await asyncio.to_thread(app.initiate_device_flow, scopes=SCOPES + self.extra)
        if "user_code" not in flow:
            st.update(_bad(flow.get("error_description", "device flow refused")[:200]))
            self.query_one("#start", Button).disabled = False
            return
        self.flow = flow
        code = flow["user_code"]
        subprocess.run(["pbcopy"], input=code.encode())
        subprocess.Popen(["open", flow["verification_uri"]])
        art = px.render(px.text_grid(code if all(c in px.FONT for c in code.upper()) else ""))
        big = Text(justify="center")
        if art.plain.strip():
            big.append_text(art)
            big.append("\n")
        big.append(f"CODE  {code}", Style(color=px.HOTPINK, bold=True))
        big.append(f"   at {flow['verification_uri']}  (copied to clipboard)", Style(color=px.DIM))
        self.query_one("#code", Static).update(big)
        st.update(_busy("waiting for you to approve in the browser…"))
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)
        if "access_token" not in result:
            st.update(_bad(result.get("error_description", "sign-in cancelled")[:200]))
            self.query_one("#start", Button).disabled = False
            return
        ol._save()
        set_config_value("outlook", "enabled", "true")
        who = result.get("id_token_claims", {}).get("preferred_username", "you")
        st.update(_ok(f"signed in as {who} · saved"))
        await asyncio.sleep(1.2)
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if self.flow:
            self.flow["expires_at"] = 0  # makes msal's polling loop give up
        self.dismiss(False)


# ── GitHub repo picker ───────────────────────────────────────────────────────

GH_CSS = f"""
GitHubScreen {{ align: center middle; background: {px.VOID} 70%; }}
GitHubScreen .box {{ width: 120; height: 90%; }}
#gh-filter {{ margin-bottom: 1; }}
#gh-lists {{ height: 1fr; }}
#gh-lists > Vertical {{ width: 1fr; height: 1fr; }}
#gh-inbox-col {{ margin-left: 2; }}
#gh-lists SelectionList {{ height: 1fr; background: {px.DEEP}; border: tall {px.PURPLE}; }}
#gh-lists SelectionList:focus {{ border: tall {px.HOTPINK}; }}
#gh-lists SelectionList > .selection-list--button-selected {{ color: {px.HOTPINK}; }}
"""


class GitHubScreen(ModalScreen[bool]):
    """Choose which repos feed the PR/CI tiles and which feed the GitHub inbox."""

    CSS = SETUP_CSS + GH_CSS
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+a", "all", "All shown"),
        Binding("ctrl+d", "none", "None shown"),
    ]

    def __init__(self) -> None:
        super().__init__()
        cfg = load_config()["github"]
        self.mon: set[str] = set(cfg.get("repos", []))
        self.inbox: set[str] = set(cfg.get("inbox_repos", []))
        self.repos: list[tuple[str, str, bool]] = []
        self._filling = False

    def compose(self) -> ComposeResult:
        from textual.widgets import SelectionList

        with Vertical(classes="box") as box:
            box.border_title = " ⚙ GITHUB · PICK YOUR REPOS "
            yield Static(_steps([
                "MONITOR feeds MY PULL REQUESTS and CI PIPELINE (REVIEW QUEUE covers every repo).",
                "INBOX feeds GITHUB INBOX (notifications). Nothing ticked = show everything.",
                "Type to filter · space toggles · ctrl+a / ctrl+d tick or clear what's shown · ctrl+s saves.",
            ]), classes="steps")
            yield TextInput(placeholder="filter repos…", id="gh-filter")
            with Horizontal(id="gh-lists"):
                with Vertical():
                    yield Label("MONITOR · PRs + CI", classes="field-label")
                    yield SelectionList(id="gh-mon")
                with Vertical(id="gh-inbox-col"):
                    yield Label("INBOX · notifications", classes="field-label")
                    yield SelectionList(id="gh-inbox")
            yield Static("", id="status", classes="status")
            with Horizontal(classes="buttons"):
                yield Button("INBOX = MONITOR", id="copy")
                yield Button("CANCEL", id="cancel")
                yield Button("SAVE", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#status", Static).update(_busy("asking GitHub which repos you can see…"))
        self._load()

    @work(exclusive=True)
    async def _load(self) -> None:
        from .sources.github import list_repos

        try:
            self.repos = await list_repos()
        except Exception as exc:
            self.query_one("#status", Static).update(_bad(f"gh: {exc}"[:200]))
            return
        self._fill()
        self._summary()
        self.query_one("#gh-filter", Input).focus()

    def _fill(self) -> None:
        from textual.widgets import SelectionList

        needle = self.query_one("#gh-filter", Input).value.strip().lower()
        shown = [r for r in self.repos if needle in r[0].lower()]
        self._filling = True
        for wid, chosen in (("#gh-mon", self.mon), ("#gh-inbox", self.inbox)):
            sl = self.query_one(wid, SelectionList)
            sl.clear_options()
            opts = []
            for name, pushed, archived in shown:
                t = Text(no_wrap=True)
                owner, _, repo = name.partition("/")
                t.append(f"{owner}/", Style(color=px.DIM))
                t.append(repo, Style(color=px.INK if not archived else px.DIM, bold=not archived))
                t.append(f"  {ago(parse_time(pushed))}" + ("  archived" if archived else ""), Style(color=px.DIM))
                opts.append((t, name, name in chosen))
            sl.add_options(opts)
        self._filling = False

    def _summary(self) -> None:
        def say(chosen: set[str]) -> str:
            return f"{len(chosen)} repo(s)" if chosen else "all repos"

        self.query_one("#status", Static).update(
            Text(f"MONITOR: {say(self.mon)}   ·   INBOX: {say(self.inbox)}   ·   {len(self.repos)} repos visible",
                 Style(color=px.CYAN))
        )

    @on(Input.Changed, "#gh-filter")
    def _filter(self) -> None:
        self._fill()

    def on_selection_list_selected_changed(self, event) -> None:
        if self._filling:
            return
        sl = event.selection_list
        target = self.mon if sl.id == "gh-mon" else self.inbox
        visible = {sl.get_option_at_index(i).value for i in range(sl.option_count)}
        target -= visible
        target |= set(sl.selected)
        self._summary()

    def _focused_list(self):
        from textual.widgets import SelectionList

        f = self.focused
        return f if isinstance(f, SelectionList) else self.query_one("#gh-mon", SelectionList)

    def action_all(self) -> None:
        self._focused_list().select_all()

    def action_none(self) -> None:
        self._focused_list().deselect_all()

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        self.inbox = set(self.mon)
        self._fill()
        self._summary()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        def arr(values: set[str]) -> str:
            return "[" + ", ".join(toml_str(v) for v in sorted(values)) + "]"

        set_config_value("github", "repos", arr(self.mon))
        set_config_value("github", "inbox_repos", arr(self.inbox))
        self.dismiss(True)


# ── Claude limits ────────────────────────────────────────────────────────────


class ClaudeScreen(ModalScreen[bool]):
    CSS = SETUP_CSS
    BINDINGS = [Binding("escape", "close", "Close")]

    def _finish(self) -> None:
        """Close from a timer: call dismiss without returning its awaitable."""
        if self.is_current:
            self.dismiss(True)

    def compose(self) -> ComposeResult:
        from .sources import claude as c

        on_ = c.wrapper_installed()
        with VerticalScroll(classes="box") as box:
            box.border_title = " ⚙ CLAUDE · USAGE + LIMITS "
            yield Static(Text("Token usage needs nothing: it's read from Claude Code's local session logs.",
                              Style(color=px.HOTPINK, bold=True)), classes="steps")
            yield Static(_steps([
                "Plan limits (5-hour and weekly %) only reach Claude Code's status line.",
                "ENABLE wraps your current status-line command: the wrapper saves rate_limits to "
                "~/.config/pixeldeck/claude-limits.json (private), then runs your original command "
                "with the same input, so Orca's hook keeps working.",
                "Limits refresh whenever a Claude Code session updates its status line. "
                "A backup of settings.json is kept as settings.json.pixeldeck-bak.",
                "If Orca rewrites the status line later, this shows as off again; just re-enable.",
            ]), classes="steps")
            yield Static("", id="status", classes="status")
            with Horizontal(classes="buttons"):
                yield Button("CLOSE", id="close")
                if on_:
                    yield Button("TURN OFF", id="off")
                else:
                    yield Button("ENABLE LIMITS", id="on", variant="primary")

    def on_mount(self) -> None:
        from .sources import claude as c

        lim = c.read_limits()
        st = self.query_one("#status", Static)
        if c.wrapper_installed() and lim:
            parts = " · ".join(f"{w.name} {w.pct:.0f}%" for w in lim.windows)
            st.update(_ok(f"on · {parts} · updated {int(lim.age // 60)}m ago"))
        elif c.wrapper_installed():
            st.update(_busy("on · waiting for a Claude Code session to update its status line"))
        else:
            st.update(Text("off", Style(color=px.YELLOW)))

    @on(Button.Pressed, "#on")
    def _on(self) -> None:
        from .sources import claude as c

        try:
            msg = c.install_wrapper()
        except Exception as exc:
            self.query_one("#status", Static).update(_bad(str(exc)[:200]))
            return
        self.query_one("#status", Static).update(_ok(msg))
        self.set_timer(1.5, self._finish)

    @on(Button.Pressed, "#off")
    def _off(self) -> None:
        from .sources import claude as c

        self.query_one("#status", Static).update(_ok(c.uninstall_wrapper()))
        self.set_timer(1.5, self._finish)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(False)
