"""Spotify: what's playing, and play/pause/skip, through the Spotify app on this Mac (AppleScript).

No Spotify account setup or API key: it talks to the desktop app, so the app has to be installed.
macOS asks once to let your terminal control Spotify. Asking whether Spotify is running never
launches it; only a control key does (pressing play on a closed Spotify opens it).
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .base import SourceError, run_cmd

SEP = "\x1f"

NOW_SCRIPT = """
if application "Spotify" is running then
  tell application "Spotify"
    set s to player state as text
    if s is "stopped" then return "stopped"
    set t to current track
    set d to character id 31
    return s & d & (name of t) & d & (artist of t) & d & (album of t) & d & (duration of t) & d & ¬
      ((player position * 1000) as integer) & d & (artwork url of t) & d & (sound volume) & d & ¬
      (shuffling as text) & d & (repeating as text) & d & (id of t)
  end tell
else
  return "closed"
end if
"""

COMMANDS = {
    "playpause": 'tell application "Spotify" to playpause',
    "next": 'tell application "Spotify" to next track',
    "previous": 'tell application "Spotify" to previous track',
    "louder": 'tell application "Spotify" to set sound volume to (sound volume + 10)',
    "quieter": 'tell application "Spotify" to set sound volume to (sound volume - 10)',
    "shuffle": 'tell application "Spotify" to set shuffling to not shuffling',
}


@dataclass
class NowPlaying:
    state: str  # playing | paused | stopped | closed | missing | denied | error
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0  # seconds
    position: float = 0.0  # seconds, as of `fetched`
    art_url: str = ""
    volume: int = 0
    shuffle: bool = False
    repeat: bool = False
    track_id: str = ""
    message: str = ""
    fetched: float = field(default_factory=time.monotonic)

    @property
    def has_track(self) -> bool:
        return self.state in ("playing", "paused") and bool(self.title)

    def position_now(self) -> float:
        """Where the track is now: ticks forward between polls while playing."""
        pos = self.position + (time.monotonic() - self.fetched if self.state == "playing" else 0)
        return min(pos, self.duration) if self.duration else pos


def installed() -> bool:
    return any(Path(p).exists() for p in ("/Applications/Spotify.app", Path.home() / "Applications/Spotify.app"))


async def _osascript(script: str, timeout: float = 8) -> str:
    return (await run_cmd("osascript", "-e", script, timeout=timeout)).strip()


class Spotify:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._art: dict[str, list[list[str | None]] | None] = {}

    async def now(self) -> NowPlaying:
        if not shutil.which("osascript") or not installed():
            return NowPlaying("missing", message="Spotify app not installed")
        try:
            out = await _osascript(NOW_SCRIPT)
        except SourceError as exc:
            msg = str(exc)
            if "-1743" in msg or "not allowed" in msg.lower():
                return NowPlaying("denied", message="allow your terminal to control Spotify: "
                                                    "System Settings › Privacy & Security › Automation")
            return NowPlaying("error", message=msg[:120])
        if out in ("closed", "stopped"):
            return NowPlaying(out)
        f = out.split(SEP)
        if len(f) < 11:
            return NowPlaying("error", message="unexpected reply from Spotify")

        def num(v: str) -> float:
            try:
                return float(v.replace(",", "."))
            except ValueError:
                return 0.0

        return NowPlaying(
            state=f[0], title=f[1], artist=f[2], album=f[3],
            duration=num(f[4]) / 1000, position=num(f[5]) / 1000, art_url=f[6],
            volume=int(num(f[7])), shuffle=f[8] == "true", repeat=f[9] == "true", track_id=f[10],
        )

    async def command(self, what: str) -> None:
        await _osascript(COMMANDS[what])

    async def open_app(self) -> None:
        await run_cmd("open", "-a", "Spotify")

    async def art(self, url: str, size: int = 8) -> list[list[str | None]] | None:
        """Cover art shrunk to size×size colours (cached per URL). None when there's no art."""
        if not url.startswith("https://"):
            return None
        key = f"{size}:{url}"
        if key in self._art:
            return self._art[key]
        grid = None
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
                resp = await http.get(url)
                resp.raise_for_status()
            with tempfile.TemporaryDirectory(prefix="pixeldeck-art-") as tmp:
                src, dst = Path(tmp, "cover"), Path(tmp, "cover.bmp")
                src.write_bytes(resp.content)
                await run_cmd("sips", "-z", str(size), str(size), "-s", "format", "bmp", str(src),
                              "--out", str(dst), timeout=10)
                grid = _read_bmp(dst.read_bytes())
        except (httpx.HTTPError, SourceError, OSError, ValueError, struct.error):
            grid = None
        if len(self._art) > 64:
            self._art.clear()
        self._art[key] = grid
        return grid


def _read_bmp(data: bytes) -> list[list[str | None]]:
    """24/32-bit uncompressed BMP → rows of '#rrggbb'."""
    if data[:2] != b"BM":
        raise ValueError("not a BMP")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp not in (24, 32):
        raise ValueError(f"{bpp}-bit BMP")
    step = bpp // 8
    stride = (width * step + 3) & ~3
    rows = []
    for y in range(abs(height)):
        base = offset + y * stride
        row = []
        for x in range(width):
            b, g, r = data[base + x * step: base + x * step + 3]
            row.append(f"#{r:02x}{g:02x}{b:02x}")
        rows.append(row)
    if height > 0:  # bottom-up
        rows.reverse()
    return rows


def fmt_time(secs: float) -> str:
    secs = max(int(secs), 0)
    return f"{secs // 60}:{secs % 60:02d}"


async def _demo() -> None:  # python -m pixeldeck.sources.spotify
    sp = Spotify({})
    np = await sp.now()
    print(np)
    if np.art_url:
        print(await sp.art(np.art_url))


if __name__ == "__main__":
    asyncio.run(_demo())
