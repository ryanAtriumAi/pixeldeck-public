"""Local machine: CPU/memory/battery, Docker containers, git repos and worktrees."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import psutil

from .base import Feed, Item, SourceError, run_cmd


@dataclass
class Vitals:
    cpu: float
    mem: float
    mem_used_gb: float
    mem_total_gb: float
    disk: float
    battery: float | None
    charging: bool
    net_down: float  # bytes/s
    net_up: float
    load: float


class System:
    def __init__(self) -> None:
        psutil.cpu_percent(None)  # prime the counter
        self._net = psutil.net_io_counters()

    def vitals(self, interval: float) -> Vitals:
        vm = psutil.virtual_memory()
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        net = psutil.net_io_counters()
        down = max(0, net.bytes_recv - self._net.bytes_recv) / max(interval, 0.1)
        up = max(0, net.bytes_sent - self._net.bytes_sent) / max(interval, 0.1)
        self._net = net
        return Vitals(
            cpu=psutil.cpu_percent(None),
            mem=vm.percent,
            mem_used_gb=(vm.total - vm.available) / 1e9,
            mem_total_gb=vm.total / 1e9,
            disk=psutil.disk_usage("/").percent,
            battery=bat.percent if bat else None,
            charging=bool(bat and bat.power_plugged),
            net_down=down,
            net_up=up,
            load=psutil.getloadavg()[0],
        )


async def docker_summary() -> tuple[int, int, list[str]] | None:
    """(running, unhealthy, unhealthy names) or None when Docker isn't available."""
    if not shutil.which("docker"):
        return None
    try:
        out = await run_cmd("docker", "ps", "--format", "{{json .}}", timeout=8)
    except SourceError:
        return None
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    bad = [r["Names"] for r in rows if "unhealthy" in r.get("Status", "")]
    return len(rows), len(bad), bad


async def repo_status(paths: list[str]) -> Feed:
    async def one(raw: str) -> Item:
        path = Path(raw).expanduser()
        name = path.name
        try:
            out = await run_cmd("git", "-C", str(path), "status", "--porcelain=v2", "--branch", timeout=10)
            wt = await run_cmd("git", "-C", str(path), "worktree", "list", "--porcelain", timeout=10)
        except (SourceError, OSError) as exc:
            return Item(title=name, meta=str(exc)[:60], icon="✖", tone="error")
        branch, ahead, behind, dirty = "?", 0, 0, 0
        for line in out.splitlines():
            if line.startswith("# branch.head "):
                branch = line.split(" ", 2)[2]
            elif line.startswith("# branch.ab "):
                a, b = line.split()[2:4]
                ahead, behind = int(a), abs(int(b))
            elif not line.startswith("#"):
                dirty += 1
        trees = sum(1 for line in wt.splitlines() if line.startswith("worktree ")) - 1
        bits = []
        if ahead:
            bits.append(f"↑{ahead}")
        if behind:
            bits.append(f"↓{behind}")
        bits.append(f"{dirty} changed" if dirty else "clean")
        if trees > 0:
            bits.append(f"{trees} worktrees")
        tone = "warning" if dirty else ("accent" if ahead else "success")
        return Item(title=f"{name}  ⎇ {branch}", meta=" · ".join(bits), icon="◈", tone=tone)

    if not paths:
        return Feed(note="add system.repos = [\"~/code/repo\"] to config.toml")
    items = await asyncio.gather(*(one(p) for p in paths))
    return Feed(items=list(items), count=sum(1 for i in items if i.tone == "warning"))
