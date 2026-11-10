"""Pick, load and unload the LM Studio model that writes reply suggestions."""

from __future__ import annotations

import time

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, OptionList, Static
from textual.widgets.option_list import Option

from . import pixel as px
from .config import set_value

LLM_CSS = f"""
ModelScreen {{ align: center middle; background: {px.VOID} 70%; }}
ModelScreen > Vertical {{ width: 96; max-width: 95%; height: auto; max-height: 85%; background: {px.DEEP};
                          border: panel {px.HOTPINK}; border-title-color: {px.VOID}; border-title-style: bold;
                          padding: 1 2; }}
#llm-list {{ height: auto; max-height: 18; background: {px.DEEP}; border: none; margin: 1 0; }}
#llm-list > .option-list--option-highlighted {{ background: #3a1670; }}
#llm-list:focus > .option-list--option-highlighted {{ background: #4a1a7a; }}
#llm-free {{ background: {px.DEEP}; border: none; }}
#llm-status {{ height: auto; margin-top: 1; }}
"""


class ModelScreen(ModalScreen[bool]):
    CSS = LLM_CSS
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("u", "unload", "Unload highlighted"),
        Binding("U", "unload_all", "Unload all"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.models: list[dict] = []
        self.changed = False
        self._note = ""  # last action's result, kept in front of the summary line

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = " ◆ REPLY MODEL · LM STUDIO "
            yield Static(Text("Enter loads the model and makes it the one that writes suggestions · "
                              "u unloads it · U unloads everything · esc closes", Style(color=px.DIM)))
            yield OptionList(id="llm-list")
            yield Checkbox("unload other models when loading (frees memory)", value=True, id="llm-free")
            yield Static("", id="llm-status")

    def on_mount(self) -> None:
        self.query_one("#llm-list", OptionList).focus()
        self.action_refresh()

    def _status(self, text: Text | str, color: str = px.INK) -> None:
        self.query_one("#llm-status", Static).update(text if isinstance(text, Text) else Text(text, Style(color=color)))

    @work(exclusive=True, group="list")
    async def action_refresh(self) -> None:
        assist = self.app.assist
        try:
            self.models = await assist.catalog()
        except Exception as exc:
            self._status(f"✖ LM Studio isn't answering at {assist.api_root} ({type(exc).__name__}). "
                         "Start its server: Developer → Start Server.", px.RED)
            return
        current = assist.model or await assist.pick_model()
        lst = self.query_one("#llm-list", OptionList)
        keep = lst.highlighted
        opts = []
        for m in self.models:
            t = Text(no_wrap=True, overflow="ellipsis")
            loaded = bool(m["instances"])
            t.append("● " if loaded else "○ ", Style(color=px.CYAN if loaded else px.DIM, bold=loaded))
            t.append(f"{m['name'][:38]:<38}", Style(color=px.INK, bold=m["key"] == current))
            t.append(f" {m['params']:>5} {str(m['quant'] or ''):<6} {m['size_gb']:5.1f} GB", Style(color=px.DIM))
            if m["key"] == current:
                t.append("  ◆ REPLIES", Style(color=px.VOID, bgcolor=px.HOTPINK, bold=True))
            opts.append(Option(t, id=m["key"]))
        lst.set_options(opts)
        if keep is not None and keep < len(opts):
            lst.highlighted = keep
        else:
            idx = next((i for i, m in enumerate(self.models) if m["key"] == current), 0)
            lst.highlighted = idx
        loaded = [m for m in self.models if m["instances"]]
        mem = sum(m["size_gb"] for m in loaded)
        note, self._note = self._note, ""
        self._status(f"{note + '   ' if note else ''}{len(loaded)} loaded ({mem:.1f} GB) · replies use {current or 'auto'}",
                     px.CYAN)

    @on(OptionList.OptionSelected, "#llm-list")
    def _pick(self, e: OptionList.OptionSelected) -> None:
        self.run_worker(self._load(e.option.id), exclusive=True, group="load")

    async def _load(self, key: str) -> None:
        assist = self.app.assist
        m = next((x for x in self.models if x["key"] == key), {"name": key, "size_gb": 0, "instances": []})
        free = self.query_one("#llm-free", Checkbox).value
        try:
            if free:
                for other in self.models:
                    if other["key"] != key and other["instances"]:
                        self._status(f"◷ unloading {other['name']}…", px.YELLOW)
                        await assist.unload(other["key"])
            if not m["instances"]:
                start = time.monotonic()
                self.app.llm_loading = (m["name"], start)
                ticker = self.set_interval(0.5, lambda: self._status(
                    f"◷ loading {m['name']} ({m['size_gb']:.1f} GB)… {time.monotonic() - start:.0f}s", px.YELLOW))
                try:
                    secs = await assist.load(key)
                finally:
                    ticker.stop()
                    self.app.llm_loading = None
                msg = f"✔ {m['name']} loaded in {secs:.0f}s"
            else:
                msg = f"✔ {m['name']} was already loaded"
        except Exception as exc:
            self.app.llm_loading = None
            self._status(f"✖ {exc}"[:200], px.RED)
            return
        set_value("assist.model", key)  # the config watcher hands the new model to the dashboard
        assist.model = key
        self.changed = True
        self._note = msg
        self.app.refresh_llm()
        self.action_refresh()

    def action_unload(self) -> None:
        opt = self.query_one("#llm-list", OptionList).highlighted_option
        if opt:
            self.run_worker(self._unload(opt.id), exclusive=True, group="load")

    def action_unload_all(self) -> None:
        self.run_worker(self._unload(None), exclusive=True, group="load")

    async def _unload(self, key: str | None) -> None:
        self._status("◷ unloading…", px.YELLOW)
        try:
            n = await self.app.assist.unload(key)
        except Exception as exc:
            self._status(f"✖ {exc}"[:200], px.RED)
            return
        self._note = f"✔ unloaded {n} instance(s)"
        self.app.refresh_llm()
        self.action_refresh()

    def action_close(self) -> None:
        self.dismiss(self.changed)
