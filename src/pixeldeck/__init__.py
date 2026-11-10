"""PIXELDECK command line."""

from __future__ import annotations

import argparse
import shutil
import sys
from importlib import resources

from .config import CONFIG_PATH, get_secret, load_config, set_secret


def _init() -> int:
    if CONFIG_PATH.exists():
        print(f"config already exists: {CONFIG_PATH}")
        return 0
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    template = resources.files("pixeldeck").joinpath("config.example.toml").read_text()
    CONFIG_PATH.write_text(template)
    print(f"wrote {CONFIG_PATH}")
    return 0


def _doctor() -> int:
    from .sources.mail import CACHE_PATH

    cfg = load_config()
    checks = [
        ("config", CONFIG_PATH.exists(), "pixeldeck init"),
        ("github (gh CLI)", bool(shutil.which("gh")), "gh auth login"),
        ("slack token", bool(get_secret("slack", env="SLACK_TOKEN")), "pixeldeck secret slack"),
        ("linear key", bool(get_secret("linear", env="LINEAR_API_KEY")), "pixeldeck secret linear"),
        (
            "microsoft 365",
            bool(cfg["outlook"].get("enabled") and CACHE_PATH.exists()),
            "[outlook] enabled = true, then pixeldeck login outlook",
        ),
    ]
    for acct in cfg.get("imap", []):
        secret = acct.get("secret", f"imap-{acct.get('name', acct['user'])}")
        checks.append((f"imap {acct.get('name', acct['user'])}", bool(get_secret(secret)), f"pixeldeck secret {secret}"))
    for name, ok, fix in checks:
        mark = "\033[38;2;76;201;240m✔\033[0m" if ok else "\033[38;2;247;37;133m✖\033[0m"
        print(f" {mark} {name:<18} {'' if ok else fix}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="pixeldeck", description="8-bit productivity console")
    parser.add_argument("--no-splash", action="store_true", help="skip the title screen")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("init", help="write a starter config.toml")
    sub.add_parser("doctor", help="show which sources are connected")
    sub.add_parser("setup", help="open the setup wizard")
    sub.add_parser("mcp", help="serve sources + task list to an agent over MCP (stdio)")
    login = sub.add_parser("login", help="sign in to a service")
    login.add_argument("service", choices=["outlook"])
    secret = sub.add_parser("secret", help="store a token in the macOS keychain")
    secret.add_argument("name", help="slack | linear | imap-<name>")
    args = parser.parse_args()

    if args.cmd == "init":
        sys.exit(_init())
    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "mcp":
        from .mcpserver import main as mcp_main

        sys.exit(mcp_main())
    if args.cmd == "secret":
        print(f"paste the value for '{args.name}' (input is hidden)")
        sys.exit(set_secret(args.name))
    if args.cmd == "login":
        from .sources.mail import Outlook

        Outlook(load_config()["outlook"]).login()
        sys.exit(0)

    from .app import PixelDeck
    from .updater import restart

    app = PixelDeck(splash=not args.no_splash and args.cmd != "setup", setup=args.cmd == "setup")
    app.run()
    if app.restart_note is not None:
        restart(app.restart_note)
