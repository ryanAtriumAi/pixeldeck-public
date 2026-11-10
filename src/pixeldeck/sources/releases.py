"""Release pipeline: what each environment runs, whether it's up, and how far main is ahead of prod.

Versions come from GitHub, so no cloud credentials are needed:
- `workflow` envs: the newest run of a deploy workflow (its tag is the version).
- `pin_file` envs: the newest commit touching a tfvars file whose message pins the image,
  e.g. "chore(prod): app_image -> prod-12, migrator -> prod-9 (ref dev-1.4.0 a1b2c3d)".
Health is a GET on each env's health URL.

A deploy in progress: a running deploy workflow (`workflow` envs), or a local ship script whose
command line matches the env's `deploy_cmd` regex (e.g. "scripts/(ship|deploy)-staging").
Only that process's REF= is read from its environment, nothing else.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .base import Feed, Item, NotConfigured, ago, parse_time, run_cmd, run_json

PIN_RE = re.compile(r"app_image\s*->\s*([\w.\-]+)")
REF_RE = re.compile(r"\(ref\s+(?:(dev-[\w.\-]+)\s+)?([0-9a-f]{7,40})\)")
SPARK = " ▁▂▃▄▅▆▇█"


def _etime_secs(etime: str) -> int:
    """ps etime "[[dd-]hh:]mm:ss" → seconds."""
    days, _, rest = etime.partition("-") if "-" in etime else ("0", "", etime)
    nums = [int(x) for x in rest.split(":")]
    while len(nums) < 3:
        nums.insert(0, 0)
    return int(days) * 86400 + nums[0] * 3600 + nums[1] * 60 + nums[2]


def _short(secs: int) -> str:
    return f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h{secs % 3600 // 60:02d}m"


class Releases:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.health: dict[str, deque[tuple[bool, float]]] = {}
        self.tag_for_sha: dict[str, str] = {}
        self.drift: int | None = None
        self.deploying: dict[str, str] = {}

    # ── version lookups ──────────────────────────────────────────────────────

    async def _workflow_env(self, repo: str, env: dict) -> dict:
        data = await run_json(
            "gh", "api", f"repos/{repo}/actions/workflows/{env['workflow']}/runs?per_page=10"
        )
        runs = data.get("workflow_runs", [])
        for run in runs:
            self.tag_for_sha[run["head_sha"][:9]] = run["head_branch"]
        live = next((r for r in runs if r["conclusion"] == "success"), None)
        busy = next((r for r in runs if r["status"] != "completed"), None)
        failed = runs[0] if runs and runs[0]["conclusion"] not in (None, "success") else None
        shown = busy or failed or live or {}
        return {
            "ref": {"kind": "run", "repo": repo, "id": shown["id"]} if shown else None,
            "version": live["head_branch"] if live else "?",
            "sha": live["head_sha"][:9] if live else None,
            "when": parse_time(live["updated_at"]) if live else None,
            "url": (busy or failed or live or {}).get("html_url"),
            "busy": busy["head_branch"] if busy else None,
            "failed": failed["head_branch"] if failed else None,
            "verb": "deployed",
        }

    async def _pin_env(self, repo: str, branch: str, env: dict) -> dict:
        path = quote(env["pin_file"])
        commits = await run_json(
            "gh", "api", f"repos/{repo}/commits?path={path}&sha={branch}&per_page=60"
        )
        pins = []
        for c in commits:
            msg = c["commit"]["message"].splitlines()[0]
            m = PIN_RE.search(msg)
            if m:
                ref = REF_RE.search(msg)
                pins.append(
                    {
                        "image": m.group(1).rstrip(","),
                        "tag": ref.group(1) if ref else None,
                        "sha": ref.group(2)[:9] if ref else None,
                        "when": parse_time(c["commit"]["committer"]["date"]),
                        "url": c["html_url"],
                        "full_sha": c["sha"],
                        "subject": msg,
                    }
                )
        if not pins:
            return {"version": "?", "sha": None, "when": None, "url": None, "pins": []}
        top = pins[0]
        for p in pins:
            if p["tag"] and p["sha"]:
                self.tag_for_sha[p["sha"]] = p["tag"]
        return {
            "version": top["image"],
            "tag": top["tag"],
            "sha": top["sha"],
            "when": top["when"],
            "url": top["url"],
            "ref": {"kind": "commit", "repo": repo, "sha": top["full_sha"]},
            "pins": pins,
            "verb": "pinned",
        }

    async def _latest_release(self, repo: str) -> dict | None:
        """The newest published GitHub release (pre-releases count: dev-* builds are pre-releases)."""
        rels = await run_json("gh", "api", f"repos/{repo}/releases?per_page=10")
        rel = next((r for r in rels if not r.get("draft")), None)
        if not rel:
            return None
        return {
            "tag": rel["tag_name"],
            "name": rel.get("name") or rel["tag_name"],
            "when": parse_time(rel.get("published_at") or rel.get("created_at")),
            "url": rel.get("html_url"),
            "prerelease": rel.get("prerelease", False),
        }

    async def local_deploys(self) -> dict[str, dict]:
        """{env name: {"ref", "secs"}} for envs whose deploy script is running on this Mac right now."""
        pats = {e["name"]: re.compile(e["deploy_cmd"]) for e in self.cfg.get("env", []) if e.get("deploy_cmd")}
        if not pats:
            return {}
        try:
            table = await run_cmd("ps", "-axww", "-o", "pid=,etime=,command=", timeout=5)
        except Exception:
            return {}
        hits: dict[str, list[tuple[int, str]]] = {}
        for line in table.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3 or parts[2].split()[0].endswith("grep"):
                continue
            pid, etime, cmd = parts
            for name, pat in pats.items():
                if pat.search(cmd):
                    hits.setdefault(name, []).append((_etime_secs(etime), pid))
        found: dict[str, dict] = {}
        for name, procs in hits.items():
            # the script's own subshells match too; the oldest process is the script itself
            procs.sort(reverse=True)
            ref = "origin/main"  # the ship scripts' default
            for _, pid in procs[:3]:
                try:  # read REF= only; the rest of the environment is ignored
                    env = await run_cmd("ps", "-wwE", "-o", "command=", "-p", pid, timeout=5)
                except Exception:
                    continue
                m = re.search(r"(?:^|\s)REF=(\S+)", env)
                if m:
                    ref = m.group(1)
                    break
            found[name] = {"ref": ref, "secs": procs[0][0]}
        return found

    # ── health ───────────────────────────────────────────────────────────────

    async def _ping(self, client: httpx.AsyncClient, name: str, url: str) -> tuple[bool, float, str]:
        start = time.perf_counter()
        try:
            resp = await client.get(url)
            ms = (time.perf_counter() - start) * 1000
            if resp.headers.get("cf-mitigated"):
                ok, why = False, "cloudflare challenge"
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                ok = resp.status_code == 200 and body.get("status", "healthy") == "healthy"
                why = body.get("status") or f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            ms, ok, why = (time.perf_counter() - start) * 1000, False, type(exc).__name__
        self.health.setdefault(name, deque(maxlen=60)).append((ok, ms))
        return ok, ms, why

    # ── feed ─────────────────────────────────────────────────────────────────

    async def pipeline(self) -> Feed:
        envs = self.cfg.get("env", [])
        repo = self.cfg.get("repo")
        if not repo or not envs:
            raise NotConfigured("add [releases] repo + [[releases.env]] blocks to config.toml")
        branch = self.cfg.get("branch", "main")

        async def version(env: dict) -> dict:
            if env.get("workflow"):
                return await self._workflow_env(repo, env)
            if env.get("pin_file"):
                return await self._pin_env(repo, branch, env)
            return {"version": "?", "sha": None, "when": None, "url": None}

        async with httpx.AsyncClient(
            timeout=8, follow_redirects=True, headers={"User-Agent": "pixeldeck/0.1"}
        ) as client:
            results = await asyncio.gather(
                self.local_deploys(),
                self._latest_release(repo),
                *(version(e) for e in envs),
                *(self._ping(client, e["name"], e["health"]) for e in envs if e.get("health")),
                return_exceptions=True,
            )
        local, latest, results = results[0], results[1], results[2:]
        local = local if isinstance(local, dict) else {}
        self.deploying = {}  # env name → what's going out
        for env, info in zip(envs, results[: len(envs)]):
            if env["name"] in local:
                self.deploying[env["name"]] = local[env["name"]]["ref"]
            elif not isinstance(info, BaseException) and info.get("busy"):
                self.deploying[env["name"]] = info["busy"]
        infos = results[: len(envs)]
        pings = iter(results[len(envs):])

        def tags_of(info) -> set[str]:
            if isinstance(info, BaseException):
                return set()
            return {t for t in (info.get("version"), info.get("tag"),
                                self.tag_for_sha.get(info.get("sha") or "")) if t and t != "?"}

        items: list[Item] = []
        prod_i = next((i for i, e in enumerate(envs) if e.get("stats")), len(envs) - 1)
        rolling = None  # the latest tag while it isn't on every env yet: envs running it show pink
        if isinstance(latest, BaseException):
            items.append(Item(title=f"{'LATEST':<8}⚠ {latest}"[:120], icon="✖", tone="error", rule_after=True))
        elif latest:
            tag = latest["tag"]
            where = {e["name"]: tag in tags_of(info) for e, info in zip(envs, infos)}
            live = bool(where) and all(where.values())  # green only once every env runs it
            rolling = None if live else tag
            on = [n.lower() for n, ok in where.items() if ok]
            off = [n.lower() for n, ok in where.items() if not ok]
            spot = ("live everywhere" if not off else
                    f"on {', '.join(on)} · not on {', '.join(off)}" if on else "not deployed anywhere yet")
            going = [n.lower() for n, ref in self.deploying.items() if ref == tag or ref.endswith("/" + tag)]
            if going:
                spot = f"◷ deploying to {', '.join(going)} · {spot}"
            items.append(Item(
                title=f"{'LATEST':<8}{tag}",
                meta=f"{spot} · released {ago(latest['when'])}" if latest.get("when") else spot,
                url=latest["url"],
                ref={"kind": "release", "repo": repo, "tag": tag, "where": where, "live": live},
                icon="★" if live else "◆",
                tone="green" if live else "accent",
                marks=[(tag, "green" if live else "accent")],
                pulse=bool(going),
                rule_after=True,
            ))

        for env, info in zip(envs, infos):
            ping = next(pings) if env.get("health") else None
            if isinstance(info, BaseException):
                items.append(Item(title=f"{env['name']:<8} ⚠ {info}", icon="✖", tone="error"))
                continue
            tag = info.get("tag") or self.tag_for_sha.get(info.get("sha") or "")
            label = info["version"] if not tag or tag == info["version"] else f"{info['version']} ← {tag}"
            bits = [f"{info.get('verb', 'live')} {ago(info['when'])}"] if info.get("when") else []
            tone, icon = "success", "●"
            if isinstance(ping, tuple):
                ok, ms, why = ping
                bits.append(f"{'up' if ok else why} {ms:.0f}ms")
                if not ok:
                    tone, icon = "error", "✖"
            if env["name"] in local:
                d = local[env["name"]]
                bits.insert(0, f"◷ deploying {d['ref'].removeprefix('origin/')} · {_short(d['secs'])}")
                tone = "warning" if tone != "error" else tone
            elif info.get("busy"):
                bits.insert(0, f"◷ deploying {info['busy']}")
                tone = "warning" if tone != "error" else tone
            elif info.get("failed"):
                bits.insert(0, f"✖ {info['failed']} failed")
                tone = "error"
            deployed = list(dict.fromkeys(t for t in (info["version"], tag) if t and t != "?"))  # live: green
            items.append(
                Item(
                    title=f"{env['name']:<8}{label}",
                    meta=" · ".join(bits),
                    url=info.get("url") or env.get("app_url"),
                    ref=info.get("ref"),
                    icon=icon,
                    tone=tone,
                    # pink while this env runs a build that isn't everywhere yet; green once it is
                    marks=[(t, "accent" if rolling and rolling in tags_of(info) else "green") for t in deployed],
                    pulse=env["name"] in self.deploying,
                )
            )

        # prod analytics: drift, cadence, health history
        prod_env, prod = envs[prod_i], infos[prod_i]
        if not isinstance(prod, BaseException):
            items += await self._stats(repo, branch, prod_env, prod)
        return Feed(items=items, count=self.drift)

    async def _stats(self, repo: str, branch: str, env: dict, info: dict) -> list[Item]:
        out: list[Item] = []
        name = env["name"]
        if info.get("sha"):
            try:
                cmp = await run_json("gh", "api", f"repos/{repo}/compare/{info['sha']}...{branch}")
                msgs = [c["commit"]["message"].splitlines()[0] for c in cmp.get("commits", [])]
                # pin/chore commits are the release machinery, not shippable work
                work = [m for m in msgs if not re.match(r"chore\((prod|staging)\)", m)]
                prs = sum(1 for m in work if re.search(r"\(#\d+\)\s*$", m))
                self.drift = len(work)
                newest = work[-1] if work else None
                out.append(
                    Item(
                        title=f"SHIP     {len(work)} commits on {branch} not in {name.lower()}",
                        meta=f"{prs} PRs · newest: {newest}" if newest else f"{name.lower()} is current with {branch}",
                        url=cmp.get("html_url"),
                        ref={"kind": "compare", "repo": repo, "base": info["sha"], "head": branch, "env": name},
                        icon="▲" if work else "★",
                        tone="accent" if work else "success",
                        rule_after=True,
                    )
                )
            except Exception as exc:
                out.append(Item(title=f"SHIP     ⚠ {exc}", icon="✖", tone="error"))

        pins = info.get("pins") or []
        if pins:
            now = datetime.now(timezone.utc)
            week = sum(1 for p in pins if p["when"] and now - p["when"] < timedelta(days=7))
            month = sum(1 for p in pins if p["when"] and now - p["when"] < timedelta(days=30))
            gaps = [
                (a["when"] - b["when"]).total_seconds() / 86400
                for a, b in zip(pins, pins[1:])
                if a["when"] and b["when"]
            ][:10]
            every = f" · every {statistics.median(gaps):.1f}d" if gaps else ""
            out.append(
                Item(
                    title=f"CADENCE  {week} {name.lower()} deploys in 7d, {month} in 30d",
                    meta=f"last {ago(pins[0]['when'])}{every}",
                    url=f"https://github.com/{repo}/commits/{branch}/{env['pin_file']}",
                    ref={"kind": "pins", "env": name, "pins": pins},
                    icon="◆",
                    tone="secondary",
                )
            )

        hist = self.health.get(name)
        if hist:
            oks = [ok for ok, _ in hist]
            lat = [ms for ok, ms in hist if ok] or [0.0]
            top = max(lat) or 1
            spark = "".join(SPARK[max(1, round(ms / top * 8))] for _, ms in list(hist)[-24:])
            out.append(
                Item(
                    title=f"UPTIME   {sum(oks) / len(oks) * 100:.0f}% · p50 {statistics.median(lat):.0f}ms · max {max(lat):.0f}ms",
                    meta=f"{spark}  {len(hist)} pings this session",
                    url=env.get("app_url"),
                    ref={"kind": "uptime", "env": name, "health": env.get("health")},
                    icon="♥" if all(oks[-3:]) else "✖",
                    tone="accent" if all(oks[-3:]) else "error",
                )
            )
        return out
