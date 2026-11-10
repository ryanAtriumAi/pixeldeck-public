"""Ask the agent: the Ask screen's other mode.

Instead of the local model's read-only searches, the question goes to Hermes (`hermes chat`), which
answers with its own tools and memory plus PIXELDECK's through `pixeldeck mcp`. Follow-ups continue
the same Hermes session until ctrl+n starts a new chat.

Hermes can act, not just read, so every question carries a short note: answer in Markdown, and don't
send, post or change anything unless the question asks for it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .ask import Turn

if TYPE_CHECKING:
    from .app import PixelDeck

Event = Callable[[str, str], None]

FRAME = (
    "[Asked from the PIXELDECK Ask screen. Answer in short Markdown. Use your tools to look things up "
    "(the pixeldeck tools see the dashboard, briefs and tasks). Don't send, post, reply, schedule or "
    "change anything unless this message explicitly asks you to.]\n\n"
)


def hermes_exe() -> str | None:
    exe = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    return exe if Path(exe).exists() else None


def hermes_model() -> str:
    """model.default from ~/.hermes/config.yaml, without a YAML dependency."""
    try:
        text = (Path.home() / ".hermes" / "config.yaml").read_text()
    except OSError:
        return ""
    m = re.search(r"(?m)^model:\s*\n((?:[ \t]+.*\n?)*)", text)
    d = re.search(r"(?m)^\s+default:\s*['\"]?([^'\"\n#]+)", m.group(1)) if m else None
    return d.group(1).strip() if d else ""


def _friendly(error: str) -> str:
    m = re.search(r"context window of ([\d,]+) tokens.*?minimum ([\d,]+)", error, re.S)
    if m:
        return (f"Hermes needs a model loaded with at least {m.group(2)} tokens of context, and LM Studio is "
                f"serving {m.group(1)}. Press l on the dashboard and load the model again (PIXELDECK loads "
                "it with 64K now), or load it in LM Studio with a bigger context length.")
    if "connect" in error.lower() and "1234" in error:
        return "Hermes can't reach LM Studio. Start its server (Developer → Start Server), then ask again."
    return error


class _NoHits:
    """The agent cites no PIXELDECK hits; this keeps the Ask screen's citation code happy."""

    hits: dict = {}

    def sources(self) -> list[str]:
        return []


class AgentSession:
    """A conversation with Hermes. Survives closing the chat until ctrl+n."""

    kind = "agent"

    def __init__(self, app: PixelDeck) -> None:
        self.app = app
        self.tools = _NoHits()
        self.turns: list[Turn] = []
        self.session_id = ""  # Hermes' own session, so follow-ups keep the thread
        self.model = ""
        self.ctx = 0
        self.exe = ""

    async def prepare(self) -> None:
        exe = hermes_exe()
        if not exe:
            raise RuntimeError("Hermes isn't installed (no `hermes` on PATH)")
        self.exe = exe
        self.model = "Hermes · " + (hermes_model() or "its default model")

    async def ask(self, question: str, emit: Event) -> Turn:
        turn = Turn(question)
        self.turns.append(turn)
        t0 = time.perf_counter()
        proc: asyncio.subprocess.Process | None = None
        try:
            if not self.exe:
                await self.prepare()
            args = [self.exe, "chat", "--query-file", "-", "--format", "stream-json", "--source", "tool"]
            if self.session_id:
                args += ["--resume", self.session_id]
            # run from home, not whatever repo PIXELDECK was started in (Hermes reads AGENTS.md from its cwd)
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(Path.home()), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=1 << 22, start_new_session=True)
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write((FRAME + question).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            streamed = ""
            result: dict | None = None
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "text":
                    streamed += ev.get("text", "")
                    emit("text", ev.get("text", ""))
                elif kind == "tool_use":
                    emit("reset", "")  # what it wrote before reaching for a tool is preamble
                    streamed = ""
                    name = ev.get("name") or "tool"
                    emit("tool", name)
                    arg = ev.get("input") or {}
                    hint = next((str(v) for v in arg.values() if isinstance(v, str) and v.strip()), "") \
                        if isinstance(arg, dict) else ""
                    line = f"⚙ {name}" + (f" “{hint[:60]}”" if hint else "")
                    turn.trace.append(line)
                    emit("traced", line)
                elif kind == "tool_result" and ev.get("is_error"):
                    line = f"✖ {ev.get('name') or 'tool'} failed"
                    turn.trace.append(line)
                    emit("traced", line)
                elif kind == "result":
                    result = ev
            await proc.wait()
            if result is None:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                raise RuntimeError(err.splitlines()[-1] if err else f"hermes exited ({proc.returncode})")
            if result.get("session_id"):
                self.session_id = result["session_id"]
            if result.get("error"):
                turn.error = _friendly(str(result["error"]))[:400]
            else:
                turn.answer = (result.get("text") or streamed).strip()
        except asyncio.CancelledError:
            turn.error = "stopped"
            turn.secs = time.perf_counter() - t0
            _stop(proc)
            raise
        except Exception as exc:
            turn.error = _friendly(f"{exc}")[:400] or type(exc).__name__
            _stop(proc)
        turn.secs = time.perf_counter() - t0
        return turn

    def sources_for(self, turn: Turn) -> list:
        return []


def _stop(proc: asyncio.subprocess.Process | None) -> None:
    if proc and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
