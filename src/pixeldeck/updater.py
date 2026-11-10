"""Self-update.

Two triggers:
- local: the package's own source changed on disk (an editable install picks the new
  code up on restart). We byte-compile first so a half-saved file never takes the deck down.
- remote: the checkout's upstream branch has new commits. We fast-forward, reinstall if the
  dependencies changed, and restart in place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .sources.base import SourceError, run_cmd

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent.parent  # src/pixeldeck -> repo root


def _sources() -> list[Path]:
    return [*PKG.rglob("*.py"), *PKG.rglob("*.toml"), ROOT / "pyproject.toml"]


class Updater:
    def __init__(self) -> None:
        self.repo = ROOT if (ROOT / ".git").exists() else None
        self.stamp = self._fingerprint()
        self.deps_stamp = self._deps_fingerprint()
        self.version = self._version()

    def _deps_fingerprint(self) -> float:
        return max(((ROOT / f).stat().st_mtime for f in ("pyproject.toml", "uv.lock") if (ROOT / f).exists()),
                   default=0.0)

    def deps_changed(self) -> bool:
        return self._deps_fingerprint() > self.deps_stamp

    async def imports(self) -> str | None:
        """Import the new code in a separate process: None if it loads, else the error.
        Catches what a syntax check can't (missing modules, bad imports, CSS-free typos)."""
        try:
            await run_cmd(sys.executable, "-c", "import pixeldeck.app", timeout=60)
        except SourceError as exc:
            return str(exc)
        return None

    def _fingerprint(self) -> float:
        return max((p.stat().st_mtime for p in _sources() if p.exists()), default=0.0)

    def _version(self) -> str:
        from importlib.metadata import version

        try:
            ver = version("pixeldeck")
        except Exception:
            ver = "dev"
        if self.repo:
            sha = subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            if sha:
                ver = f"{ver}+{sha}"
        return ver

    # ── local edits ──────────────────────────────────────────────────────────

    def local_changed(self) -> bool:
        return self._fingerprint() > self.stamp

    def compiles(self) -> str | None:
        """None when every module compiles, else the first error."""
        for path in PKG.rglob("*.py"):
            try:
                compile(path.read_text(), str(path), "exec")
            except SyntaxError as exc:
                return f"{path.name}:{exc.lineno}: {exc.msg}"
            except (OSError, UnicodeDecodeError) as exc:
                return f"{path.name}: {exc}"
        return None

    # ── remote ───────────────────────────────────────────────────────────────

    async def _git(self, *args: str, timeout: float = 60) -> str:
        return (await run_cmd("git", "-C", str(self.repo), *args, timeout=timeout)).strip()

    async def has_upstream(self) -> bool:
        if not self.repo:
            return False
        try:
            await self._git("rev-parse", "--abbrev-ref", "@{u}")
            return True
        except SourceError:
            return False

    async def pending(self) -> list[str]:
        """Subjects of upstream commits we don't have yet."""
        if not await self.has_upstream():
            return []
        await self._git("fetch", "--quiet")
        out = await self._git("log", "--format=%s", "HEAD..@{u}")
        return [line for line in out.splitlines() if line]

    async def pull(self) -> list[str]:
        """Fast-forward to upstream; reinstall when dependencies changed. Returns new subjects."""
        before = await self._git("rev-parse", "HEAD")
        subjects = await self.pending()
        if not subjects:
            return []
        await self._git("merge", "--ff-only", "@{u}")
        changed = await self._git("diff", "--name-only", before, "HEAD")
        if {"pyproject.toml", "uv.lock"} & set(changed.splitlines()):
            await self.reinstall()
        return subjects

    async def reinstall(self) -> None:
        uv = shutil.which("uv")
        if not uv:
            raise SourceError("dependencies changed but uv is not on PATH")
        tool = shutil.which("pixeldeck")
        if tool and ".local/bin" in tool:
            await run_cmd(uv, "tool", "install", "--editable", str(ROOT), "--force", timeout=300)
        else:
            await run_cmd(uv, "sync", "--project", str(ROOT), timeout=300)


def restart(note: str | None = None) -> None:
    """Replace this process with a fresh one running the new code."""
    args = [a for a in sys.argv[1:] if a not in ("--no-splash", "setup")]
    env = dict(os.environ)
    if note:
        env["PIXELDECK_UPDATED"] = note
    os.execve(sys.executable, [sys.executable, "-m", "pixeldeck", "--no-splash", *args], env)
