"""The RELEASES page (press the RELEASES tile's number twice): what runs where, how far behind production
is, and how often we ship.

Cards: one per environment (the build it runs, up or down, its deploy history), LATEST (the newest release
and where it is live), DRIFT (commits production doesn't have yet) and RELEASES (recent builds, newest
first). A reload is the tile's own `Releases.pipeline()` call; the page reads what it kept. Read-only:
nothing here deploys, promotes or re-runs anything.
"""

from __future__ import annotations

import re
import statistics
import time
from datetime import datetime, timedelta, timezone

from rich.text import Text

from . import pixel as px
from .pages import Card, Group, SectionScreen, heading, hbar, kv, spark
from .sources.base import Item, ago

WEEKS = 8
PR_RE = re.compile(r"\(#\d+\)\s*$")


def short_version(v: str | None) -> str:
    """dev-00.01.106 → 106, prod-50 → 50: the build number alone fits the card's pixel font on a narrow
    screen; the rows under the card carry the full tag."""
    v = re.sub(r"^[A-Za-z]+-", "", v or "").rsplit(".", 1)[-1]
    return re.sub(r"^0+(?=\d)", "", v) or "?"


def _weekly(times: list[datetime | None], now: datetime) -> list[int]:
    out = [0] * WEEKS
    for at in times:
        if at:
            back = int((now - at).total_seconds() // (7 * 86400))
            if 0 <= back < WEEKS:
                out[WEEKS - 1 - back] += 1
    return out


class ReleasesPage(SectionScreen):
    NAME = "RELEASES"
    TILE = "releases"
    REFRESH = 120.0
    SIDE_TITLE = "SHIPPING"
    STATE = ("ready",)

    def __init__(self, card: str | None = None) -> None:
        super().__init__(card)
        self.ready = False

    @property
    def rel(self):
        return self.app.releases

    def _envs(self) -> list[dict]:
        return self.rel.cfg.get("env", [])

    def _stats_env(self) -> str | None:
        envs = self._envs()
        return next((e["name"] for e in envs if e.get("stats")), envs[-1]["name"] if envs else None)

    def note(self) -> str:
        names = [e["name"].lower() for e in self._envs()]
        return " → ".join([self.rel.cfg.get("branch", "main"), *names]) if names else ""

    def skeleton(self) -> list[Card]:
        cards = [Card(f"env:{e['name']}", e["name"], tone="secondary", empty="no deploys found")
                 for e in self._envs()]
        return cards + [
            Card("latest", "LATEST", tone="accent", empty="no releases yet"),
            Card("drift", "DRIFT", tone="accent", empty="production is current"),
            Card("releases", "RELEASES", tone="primary", empty="no releases yet"),
        ]

    def instant(self) -> tuple[list[Card], float] | None:
        """The RELEASES tile already ran the same pipeline: when it last succeeded, draw from what it kept."""
        from .app import Tile

        tile = next((t for t in self.app.query(Tile) if t.key == self.TILE), None)
        if tile is None or tile.feed is None or not tile.updated or not self.rel.envs:
            return None
        self.ready = True
        return self._cards(), time.time() - (time.monotonic() - tile.updated)

    async def fetch(self) -> list[Card]:
        await self.rel.pipeline()  # refreshes versions, health, drift and the release list in one go
        self.ready = True
        return self._cards()

    # helpers ----------------------------------------------------------------------

    def _tags_of(self, info) -> set[str]:
        """Every name the build an env runs goes by (image, release tag)."""
        if not isinstance(info, dict):
            return set()
        return {t for t in (info.get("version"), info.get("tag"), self.rel.tag_for_sha.get(info.get("sha") or ""))
                if t and t != "?"}

    def _tag(self, info: dict) -> str | None:
        return info.get("tag") or self.rel.tag_for_sha.get(info.get("sha") or "") or None

    def _where(self, tag: str) -> dict[str, bool]:
        return {env["name"]: tag in self._tags_of(info) for env, info in self.rel.envs}

    def _release_ref(self, tag: str) -> dict:
        where = self._where(tag)
        return {"kind": "release", "repo": self.rel.cfg.get("repo"), "tag": tag, "where": where,
                "live": bool(where) and all(where.values())}

    def _uptime(self, name: str) -> tuple[float, float, int] | None:
        """(% up, p50 ms, pings) over this session's health history."""
        hist = list(self.rel.health.get(name) or [])
        if not hist:
            return None
        lat = [ms for ok, ms in hist if ok] or [0.0]
        return sum(ok for ok, _ in hist) / len(hist) * 100, statistics.median(lat), len(hist)

    def _deploy_times(self, info: dict) -> list[datetime | None]:
        if info.get("pins"):
            return [p["when"] for p in info["pins"]]
        return [r["when"] for r in info.get("runs") or [] if r["conclusion"] == "success"]

    # cards ------------------------------------------------------------------------

    def _cards(self) -> list[Card]:
        cards = self.skeleton()
        if not self.ready:
            return cards
        by = {c.key: c for c in cards}
        rel, repo = self.rel, self.rel.cfg.get("repo")
        latest = rel.latest if isinstance(rel.latest, dict) else None
        newest = latest["tag"] if latest else None

        for env, info in rel.envs:
            c = by[f"env:{env['name']}"]
            if isinstance(info, BaseException):
                c.value, c.hint, c.tone = "!", "lookup failed", "error"
                c.rows = [Item(title=f"⚠ {info}"[:160], icon="✖", tone="error")]
                continue
            c.rows = self._env_rows(env, info, repo)
            tag = self._tag(info)
            c.value = short_version(tag or info["version"])
            ping = rel.last_ping.get(env["name"])
            bits = []
            if ping and not ping[0]:
                c.tone, bits = "error", [f"✖ {ping[2]}"[:18]]
            elif env["name"] in rel.deploying:
                c.tone, bits = "warning", [f"◷ deploying {short_version(rel.deploying[env['name']].split('/')[-1])}"]
            elif info.get("failed"):
                c.tone, bits = "warning", [f"✖ {short_version(info['failed'])} failed"]
            else:
                c.tone = "green" if newest and newest in self._tags_of(info) else "secondary"
                bits = ["up" if ping else "no health url"]
            if info.get("when"):  # a warning already fills the hint: the age alone then
                age = ago(info["when"]).removesuffix(" ago")
                bits.append(age if c.tone in ("error", "warning") else f"live {age}")
            c.hint = " · ".join(bits)

        c = by["latest"]
        if isinstance(rel.latest, BaseException):
            c.value, c.hint, c.tone = "!", "lookup failed", "error"
            c.rows = [Item(title=f"⚠ {rel.latest}"[:160], icon="✖", tone="error")]
        elif latest:
            where = self._where(newest)
            on = [n for n, ok in where.items() if ok]
            live = bool(where) and len(on) == len(where)
            c.value, c.tone = short_version(newest), "green" if live else "accent"
            c.hint = "live everywhere" if live else f"on {len(on)} of {len(where)}"
            c.rows = [Item(
                title=newest, meta=f"released {ago(latest['when'])}" if latest.get("when") else "",
                url=latest.get("url"), ref=self._release_ref(newest), icon="★" if live else "◆",
                tone="green" if live else "accent", marks=[(newest, "green" if live else "accent")],
            ), Group("WHERE IT RUNS", "secondary")]
            for env, info in rel.envs:
                ok = where.get(env["name"])
                runs = short_version(self._tag(info) or info.get("version")) if isinstance(info, dict) else "?"
                c.rows.append(Item(
                    title=f"{env['name']:<8}{'runs it' if ok else 'runs ' + runs}",
                    meta=("◷ deploying now · " if env["name"] in rel.deploying else "")
                    + (f"since {ago(info['when'])}" if isinstance(info, dict) and info.get("when") else ""),
                    url=info.get("url") if isinstance(info, dict) else None,
                    ref=info.get("ref") if isinstance(info, dict) else None,
                    icon="✔" if ok else "·", tone="green" if ok else "muted", pulse=env["name"] in rel.deploying,
                ))

        c = by["drift"]
        prod = self._stats_env()
        work = rel.unshipped
        prs = sum(1 for w in work if PR_RE.search(w["subject"]))
        c.value = str(rel.drift) if rel.drift is not None else "?"
        c.tone = "accent" if work else "green"
        oldest = min((w["when"] for w in work if w["when"]), default=None)
        c.hint = (f"{prs} PRs · oldest {ago(oldest).removesuffix(' ago')}" if oldest
                  else f"{(prod or '').lower()} is current")
        c.empty = f"{(prod or 'production').lower()} is current with {rel.cfg.get('branch', 'main')}"
        c.rows = [Item(
            title=w["subject"], meta=f"{w['sha'][:7]} · @{w['author']} · {ago(w['when'])}", url=w.get("url"),
            ref={"kind": "commit", "repo": repo, "sha": w["sha"]}, icon="▲" if PR_RE.search(w["subject"]) else "▸",
            tone="accent" if PR_RE.search(w["subject"]) else "primary",
        ) for w in work]

        c = by["releases"]
        now = datetime.now(timezone.utc)
        rels = rel.recent_releases
        week = sum(1 for r in rels if r["when"] and now - r["when"] < timedelta(days=7))
        month = sum(1 for r in rels if r["when"] and now - r["when"] < timedelta(days=30))
        capped = "+" if len(rels) >= 100 and month == len(rels) else ""  # the fetch stops at 100
        c.value, c.hint = str(week), f"this week · {month}{capped} in 30d"
        c.rows = []
        for r in rels:
            where = self._where(r["tag"])
            on = [n.lower() for n, ok in where.items() if ok]
            c.rows.append(Item(
                title=r["tag"] if r["name"] == r["tag"] else f"{r['tag']}  {r['name']}",
                meta=" · ".join([f"released {ago(r['when'])}" if r["when"] else "",
                                 f"on {', '.join(on)}" if on else "not deployed"]).strip(" ·"),
                url=r.get("url"), ref=self._release_ref(r["tag"]), icon="●" if on else "○",
                tone="green" if on else "muted", marks=[(r["tag"], "green")] if on else [],
            ))
        return cards

    def _env_rows(self, env: dict, info: dict, repo: str) -> list[Item | Group]:
        rows: list[Item | Group] = []
        up = self._uptime(env["name"])
        ping = self.rel.last_ping.get(env["name"])
        if env.get("health"):
            ok = bool(ping and ping[0])
            title = f"{'UP' if ok else 'DOWN'} {ping[1]:.0f}ms" if ping else "health not checked yet"
            if ping and not ok:
                title += f" · {ping[2]}"
            rows.append(Item(
                title=title, meta=f"{up[0]:.0f}% of {up[2]} pings this session · p50 {up[1]:.0f}ms" if up else "",
                url=env.get("app_url"), ref={"kind": "uptime", "env": env["name"], "health": env.get("health")},
                icon="♥" if ok else "✖", tone="green" if ok else "error",
            ))
        if info.get("pins"):
            pins = info["pins"]
            rows.append(Group(f"PINS · {len(pins)}", "secondary"))
            for i, p in enumerate(pins):
                label = p["image"] if not p.get("tag") else f"{p['image']} ← {p['tag']}"
                rows.append(Item(
                    title=label, meta=f"{'live · ' if i == 0 else ''}pinned {ago(p['when'])} · {p['full_sha'][:7]}",
                    url=p["url"], ref={"kind": "commit", "repo": repo, "sha": p["full_sha"]},
                    icon="●" if i == 0 else "○", tone="green" if i == 0 else "primary",
                    marks=[(p["image"], "green")] if i == 0 else [],
                ))
        elif info.get("runs"):
            runs = info["runs"]
            live = next((r["id"] for r in runs if r["conclusion"] == "success"), None)
            rows.append(Group(f"DEPLOY RUNS · {len(runs)}", "secondary"))
            for r in runs:
                if r["status"] != "completed":
                    icon, tone, word = "◷", "warning", r["status"].replace("_", " ")
                elif r["conclusion"] == "success":
                    icon, tone, word = ("●", "green", "live") if r["id"] == live else ("○", "primary", "deployed")
                elif r["conclusion"] in ("cancelled", "skipped"):
                    icon, tone, word = "◌", "muted", r["conclusion"]
                else:
                    icon, tone, word = "✖", "error", r["conclusion"] or "failed"
                rows.append(Item(
                    title=r["tag"], meta=f"{word} · {ago(r['when'])} · {r['sha'][:7]}", url=r["url"],
                    ref={"kind": "run", "repo": repo, "id": r["id"]}, icon=icon, tone=tone,
                    marks=[(r["tag"], "green")] if r["id"] == live else [], pulse=r["status"] != "completed",
                ))
        return rows

    # side panel -------------------------------------------------------------------

    def side(self, width: int) -> Text | None:
        if not self.ready:
            return None
        rel = self.rel
        out = Text()
        envs = [(e, i) for e, i in rel.envs if isinstance(i, dict)]

        ups = [(e["name"], self._uptime(e["name"])) for e, _ in envs if e.get("health")]
        if ups:
            out.append_text(heading("UPTIME THIS SESSION"))
            for name, up in ups:
                pct = round(up[0]) if up else 0
                color = px.GREEN if pct >= 99 else px.YELLOW if pct >= 90 else px.RED
                out.append_text(hbar(name, pct, 100, width, color, label_w=8))
            for name, up in ups:
                if up:
                    out.append_text(kv(name, f"p50 {up[1]:.0f}ms · {up[2]} pings", label_w=8))
            out.append("\n")

        now = datetime.now(timezone.utc)
        series = [(e["name"], _weekly(self._deploy_times(i), now)) for e, i in envs]
        rels = _weekly([r["when"] for r in rel.recent_releases], now)
        top = max([v for _, s in series for v in s] + rels, default=0)
        out.append_text(heading(f"DEPLOYS PER WEEK · {WEEKS}W"))
        out.append_text(spark("releases", rels, px.VIOLET, top))
        for name, s in series:
            out.append_text(spark(name.lower(), s, px.GREEN if name == self._stats_env() else px.CYAN, top))

        prod = next((i for e, i in envs if e["name"] == self._stats_env()), None)
        if prod:
            name = self._stats_env().lower()
            times = sorted((t for t in self._deploy_times(prod) if t), reverse=True)
            out.append("\n")
            out.append_text(heading(name.upper()))
            out.append_text(kv("runs", short_version(self._tag(prod) or prod.get("version"))))
            if times:
                days = (now - times[0]).total_seconds() / 86400
                out.append_text(kv("last deploy", f"{days:.1f} days ago", px.YELLOW if days > 7 else px.GREEN))
                gaps = [(a - b).total_seconds() / 86400 for a, b in zip(times, times[1:])][:10]
                if gaps:
                    out.append_text(kv("deploys every", f"{statistics.median(gaps):.1f}d (median of {len(gaps)})"))
            work = rel.unshipped
            if rel.drift is not None:
                prs = sum(1 for w in work if PR_RE.search(w["subject"]))
                out.append_text(kv("behind", f"{rel.drift} commits · {prs} PRs", px.HOTPINK if rel.drift else px.GREEN))
            oldest = min((w["when"] for w in work if w["when"]), default=None)
            if oldest:
                out.append_text(kv("oldest waiting", ago(oldest).removesuffix(" ago")))
            if isinstance(rel.latest, dict):
                behind = next((n for n, r in enumerate(rel.recent_releases) if r["tag"] in self._tags_of(prod)), None)
                if behind:
                    out.append_text(kv("releases behind", str(behind), px.HOTPINK))

        if rel.deploying:
            out.append("\n")
            out.append_text(heading("DEPLOYING NOW", px.YELLOW))
            for name, ref in rel.deploying.items():
                out.append_text(kv(name, ref.removeprefix("origin/"), px.YELLOW))
        return out
