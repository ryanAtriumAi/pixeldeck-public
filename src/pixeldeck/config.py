"""Config loading and secret lookup.

Config lives at ~/.config/pixeldeck/config.toml (override with PIXELDECK_CONFIG).
Secrets are never stored in the config file: they come from an env var or the
macOS keychain (service "pixeldeck", account = secret name).
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("PIXELDECK_HOME", Path.home() / ".config" / "pixeldeck"))
CONFIG_PATH = Path(os.environ.get("PIXELDECK_CONFIG", CONFIG_DIR / "config.toml"))
KEYCHAIN_SERVICE = "pixeldeck"

DEFAULTS: dict[str, Any] = {
    "github": {"enabled": True, "repos": [], "inbox_repos": [], "ci_repos": [], "ci_branch": "main",
               "inbox_reasons": []},
    "outlook": {
        "enabled": False,
        # Microsoft's public "Graph Command Line Tools" client. Swap for your own
        # Entra app registration if your tenant blocks it.
        "client_id": "14d82eec-204b-4c2f-b7e8-296a70dab67e",
        "authority": "https://login.microsoftonline.com/organizations",
    },
    "mail": {"hide_from": ["github.com"], "hide_subjects": [], "ack_on_open": True},
    "imap": [],
    "apple_mail": {"enabled": False},
    "slack": {"enabled": True, "queries": ["<@ME>", "to:me"], "hide_apps": True, "starred": [], "dm_days": 7},
    "linear": {"enabled": True, "scope": "assigned", "states": ["started", "unstarted", "triage"],
               "sort": "status", "limit": 100, "teams": [], "hide_states": [], "priorities": []},
    "system": {"docker": True, "repos": [], "meters": ["cpu", "mem", "net", "bat"]},
    "claude": {"warn_pct": 80},
    "agenda": {"skip_weekends": False},
    "spotify": {"enabled": True},
    "ask": {"default": "model"},  # what ? opens first: "model" (local, read-only) or "agent" (Hermes)
    "assist": {"enabled": False, "url": "http://localhost:1234/v1", "model": "", "name": "",
               "tone": "keep it casual, like a quick message to a colleague", "allow_remote": False,
               "context": 65536},  # context LM Studio loads models with (Hermes needs at least 64K)
    "releases": {"repo": "", "branch": "main", "env": []},
    "update": {"auto": False, "check_minutes": 15},
    "refresh": {
        "github": 120,
        "mail": 90,
        "calendar": 300,
        "slack": 90,
        "linear": 180,
        "system": 2,
        "repos": 30,
        "releases": 60,
    },
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            return _merge(DEFAULTS, tomllib.load(fh))
    return _merge(DEFAULTS, {})


def get_secret(name: str, env: str | None = None) -> str | None:
    """Env var first, then the macOS keychain. Returns None when unset."""
    env = env or f"PIXELDECK_{name.upper()}"
    for var in (env, name.upper()):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    return value or None


def set_secret(name: str) -> int:
    """Prompt for a secret on the terminal and store it in the keychain.

    `-w` as the last argument makes `security` prompt for the value itself, so
    the secret never appears in argv or shell history.
    """
    return subprocess.call(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"]
    )


def store_secret(name: str, value: str) -> None:
    """Save a secret to the keychain through the Security framework (never via argv)."""
    import keyring

    keyring.set_password(KEYCHAIN_SERVICE, name, value)


def _doc():
    """The config file as a comment-preserving tomlkit document (created from the template if missing)."""
    import tomlkit
    from importlib import resources

    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(resources.files("pixeldeck").joinpath("config.example.toml").read_text())
    return tomlkit.parse(CONFIG_PATH.read_text())


def _write(doc) -> None:
    import tomlkit

    text = tomlkit.dumps(doc)
    tomllib.loads(text)  # never write a file we can't read back
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text(text)
    tmp.replace(CONFIG_PATH)


_MISSING = object()


def get_value(path: str, default: Any = None) -> Any:
    """Read a dotted key from the merged config (file over defaults), e.g. "slack.hide_apps"."""
    node: Any = load_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def file_value(path: str) -> Any:
    """What the file itself says for a dotted key (_MISSING when it isn't set there)."""
    node: Any = _doc()
    for part in path.split("."):
        if not hasattr(node, "get") or part not in node:
            return _MISSING
        node = node[part]
    return node.unwrap() if hasattr(node, "unwrap") else node


def set_value(path: str, value: Any) -> None:
    """Set a dotted key ("github.repos", "releases.env"), creating tables as needed.
    Lists of dicts become arrays of tables. Comments and layout elsewhere are kept."""
    import tomlkit

    doc = _doc()
    *parents, key = path.split(".")
    node = doc
    for part in parents:
        if part not in node:
            node[part] = tomlkit.table()
        node = node[part]
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        aot = tomlkit.aot()
        for v in value:
            aot.append(tomlkit.item(v))
        node[key] = aot
    else:
        node[key] = value
    _write(doc)


def delete_value(path: str) -> None:
    """Remove a dotted key from the file so the built-in default applies again."""
    doc = _doc()
    *parents, key = path.split(".")
    node = doc
    for part in parents:
        if part not in node:
            return
        node = node[part]
    if key in node:
        del node[key]
        _write(doc)


def set_config_value(section: str, key: str, literal: str) -> None:
    """Back-compat: set `key` in `[section]` from a TOML literal string."""
    set_value(f"{section}.{key}", tomllib.loads(f"v = {literal}")["v"])


def append_config_block(block: str) -> None:
    """Back-compat: append arrays-of-tables entries such as an [[imap]] block."""
    parsed = tomllib.loads(block)
    for name, entries in parsed.items():
        current = file_value(name)
        current = list(current) if isinstance(current, list) else []
        set_value(name, current + list(entries))


def delete_secret(name: str) -> None:
    import keyring

    try:
        keyring.delete_password(KEYCHAIN_SERVICE, name)
    except Exception:
        pass


def mask(secret: str | None) -> str:
    return f"set ••••{secret[-4:]}" if secret and len(secret) > 8 else ("set" if secret else "not set")


def toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
