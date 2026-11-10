"""Open an email's attachments on their own, without opening the whole email.

The email page links each attachment as ``pixeldeck-att:<token>``. Opening one fetches just that
file (Microsoft 365) or uses the bytes already downloaded (IMAP), saves it in a private cache
folder (marked as downloaded, so macOS treats it like any other download), then:

* images, PDFs, video/audio and text files open in your default browser, in a small viewer page;
  text-like files, HTML included, are shown as text and never run;
* documents (Word, Excel, PowerPoint, Pages, Numbers, Keynote, RTF) are rendered read-only by
  macOS Quick Look and shown in the browser (macros never run); if Quick Look can't render one it
  opens in its app instead, and the attachment picker can always open it there on purpose;
* anything else (archives, installers, scripts, executables) is only shown in Finder, never opened.

Cached files are swept after a day.
"""

from __future__ import annotations

import asyncio
import hashlib
import plistlib
import html
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from .app import PixelDeck

SCHEME = "pixeldeck-att:"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pixeldeck" / "attachments"
KEEP_SECONDS = 24 * 3600
MIN_INLINE = 8 * 1024  # inline images smaller than this are signature logos and social icons
MAX_TEXT = 2_000_000  # characters shown in the text viewer

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico")
APP_IMAGE_EXT = (".heic", ".heif", ".tif", ".tiff")  # browsers can't show these: Preview can
VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm")
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".oga", ".flac", ".opus")
TEXT_EXT = (".txt", ".log", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".md", ".markdown", ".ini",
            ".toml", ".conf", ".cfg", ".html", ".htm", ".eml", ".diff", ".patch", ".sql", ".py", ".js", ".ts",
            ".sh", ".ps1", ".bat", ".rb", ".go", ".java", ".c", ".h", ".css", ".har", ".ics", ".vcf")
# documents: shown read-only in the browser through macOS Quick Look (no macros, no editing)
DOC_EXT = (".docx", ".docm", ".doc", ".xlsx", ".xlsm", ".xls", ".pptx", ".pptm", ".ppt", ".pages", ".numbers",
           ".key", ".rtf", ".odt", ".ods", ".odp")

# how each kind is opened, and its icon in the email page
KINDS = {
    "image": ("🖼", "opens in browser"),
    "pdf": ("📄", "opens in browser"),
    "video": ("🎞", "opens in browser"),
    "audio": ("🎵", "opens in browser"),
    "text": ("📝", "browser, as text"),
    "app_image": ("🖼", "opens in Preview"),
    "doc": ("📎", "read-only in browser"),
    "other": ("📦", "Finder only, not run"),
}

_refs: dict[str, dict] = {}


def kind_of(name: str, ctype: str | None) -> str:
    n, ct = (name or "").lower(), (ctype or "").lower()
    if n.endswith(APP_IMAGE_EXT) or ct in ("image/heic", "image/heif", "image/tiff"):
        return "app_image"
    if n.endswith(IMAGE_EXT) or (ct.startswith("image/") and "." not in Path(n).suffix):
        return "image"
    if n.endswith(".pdf") or ct == "application/pdf":
        return "pdf"
    if n.endswith(VIDEO_EXT):
        return "video"
    if n.endswith(AUDIO_EXT):
        return "audio"
    if n.endswith(TEXT_EXT) or (ct.startswith("text/") and not Path(n).suffix):
        return "text"
    if n.endswith(DOC_EXT):
        return "doc"
    return "other"


def is_image(name: str, ctype: str | None) -> bool:
    return kind_of(name, ctype) in ("image", "app_image")


def register(ref: dict) -> str:
    """Remember how to fetch an attachment; returns the link to put in the page."""
    key = "|".join(str(ref.get(k, "")) for k in ("via", "msg", "att", "name", "size"))
    token = hashlib.sha1(key.encode()).hexdigest()[:16]
    _refs[token] = ref
    return SCHEME + token


def md_link(ref: dict) -> str:
    icon, _ = KINDS[kind_of(ref.get("name", ""), ref.get("ctype"))]
    text = re.sub(r"([\[\]\\*_`<>])", r"\\\1", ref.get("name") or "attachment")
    return f"[{icon} {text}]({register(ref)})"


def _safe_name(name: str, ctype: str | None) -> str:
    base = re.sub(r"[^\w.\- ]", "_", Path(name or "attachment").name).strip(" .") or "attachment"
    if not Path(base).suffix and (ctype or "").startswith("image/"):
        ext = ctype.split("/")[-1].split("+")[0].lower().replace("jpeg", "jpg")
        base += "." + (ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else "png")
    stem, suffix = os.path.splitext(base)
    return stem[: 100 - len(suffix)] + suffix


def _sweep() -> None:
    if not CACHE.exists():
        return
    cutoff = time.time() - KEEP_SECONDS
    for d in CACHE.iterdir():
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _quarantine(path: Path) -> None:
    """Mark the file as downloaded, the way a browser would, so macOS checks it before running anything."""
    try:
        subprocess.run(["xattr", "-w", "com.apple.quarantine", f"0081;{int(time.time()):x};pixeldeck;", str(path)],
                       capture_output=True, timeout=5)
    except Exception:
        pass


VIEWER_CSS = """
  html,body{margin:0;background:#0d0221;color:#e8dcff;font:14px ui-monospace,Menlo,monospace;height:100%}
  body{display:flex;flex-direction:column}
  header{padding:10px 16px;border-bottom:2px solid #ff2e88;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
  header b{color:#ff71ce} header span{color:#8f7ab8} a{color:#05d9e8}
  main{flex:1;display:flex;justify-content:center;align-items:flex-start;padding:16px;min-height:0}
  img{max-width:100%;height:auto;background:repeating-conic-gradient(#1a0b33 0 25%,#221142 0 50%) 0 0/16px 16px}
  video{max-width:100%;max-height:85vh} audio{width:min(640px,100%);margin-top:40px}
  iframe{width:100%;height:100%;min-height:85vh;border:0;background:#fff}
  pre{margin:0;width:100%;white-space:pre-wrap;word-break:break-word;color:#e8dcff;line-height:1.45}
"""


def _viewer(kind: str, name: str, data: bytes, note: str, src: str | None = None) -> str:
    n, src = html.escape(name), quote(src or name)
    if kind == "image":
        body = f'<img src="{src}" alt="{n}">'
    elif kind == "pdf":
        body = f'<iframe src="{src}" title="{n}"></iframe>'
    elif kind == "video":
        body = f'<video src="{src}" controls></video>'
    elif kind == "audio":
        body = f'<audio src="{src}" controls></audio>'
    else:  # text: always shown as text, never rendered or run
        text = data.decode("utf-8", errors="replace")
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n\n… (cut here, download for the rest)"
        body = f"<pre>{html.escape(text)}</pre>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{n}</title>
<style>{VIEWER_CSS}</style></head>
<body><header><b>{n}</b><span>{len(data) / 1024:.0f} KB{html.escape(note)}</span>
<a href="{src}" download>download</a></header>
<main>{body}</main></body></html>
"""


def _quicklook(path: Path, out: Path) -> Path | None:
    """Render a document with Quick Look into `out`; returns the page (or PDF/image) to show."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    try:
        subprocess.run(["qlmanage", "-p", "-o", str(out), str(path)], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for q in out.glob("*.qlpreview"):
        try:
            props = plistlib.loads((q / "PreviewProperties.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException):
            props = {}
        for f in q.iterdir():
            os.chmod(f, 0o600)
        mime = props.get("MimeType", "")
        main = next(q.glob("Preview.*"), None)
        if main is not None and (mime in ("text/html", "application/pdf") or mime.startswith("image/")):
            return main
    return None


async def _fetch(app: PixelDeck, ref: dict) -> bytes:
    if ref.get("data") is not None:
        return ref["data"]
    if ref.get("via") == "graph":
        return await app.outlook._get_bytes(f"/me/messages/{ref['msg']}/attachments/{ref['att']}/$value")
    raise RuntimeError("don't know how to fetch this attachment")


async def open_link(app: PixelDeck, url: str, reveal: bool = False, in_app: bool = False) -> tuple[str, Path]:
    """Fetch the attachment behind a pixeldeck-att: link and show it. Returns (how, path)."""
    token = url[len(SCHEME):]
    ref = _refs.get(token)
    if ref is None:
        raise RuntimeError("that attachment link is stale, reload the email (ctrl+r)")
    data = await _fetch(app, ref)
    _sweep()
    folder = CACHE / token
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(CACHE, 0o700)
    os.chmod(folder, 0o700)
    name = _safe_name(ref.get("name", ""), ref.get("ctype"))
    path = folder / name
    path.write_bytes(data)
    os.chmod(path, 0o600)
    _quarantine(path)
    kind = kind_of(name, ref.get("ctype"))
    src = None
    if reveal or kind == "other":
        subprocess.Popen(["open", "-R", str(path)])  # select it in Finder; never run it
        return "finder", path
    if kind == "doc" and not in_app:
        shown = await asyncio.to_thread(_quicklook, path, folder / "preview")
        if shown is not None:
            if shown.suffix.lower() == ".html":
                subprocess.Popen(["open", shown.as_uri()])
                return "browser", shown
            kind, src, data = ("pdf" if shown.suffix.lower() == ".pdf" else "image"), \
                os.path.relpath(shown, folder), shown.read_bytes()
        else:
            in_app = True  # Quick Look can't read it: fall back to its app
    if kind in ("doc", "app_image") or in_app:
        subprocess.Popen(["open", str(path)])
        return "app", path
    page = folder / "index.html"
    subject = ref.get("subject") or ""
    page.write_text(_viewer(kind, name, data, f" · from “{subject[:80]}”" if subject else "", src))
    os.chmod(page, 0o600)
    subprocess.Popen(["open", page.as_uri()])  # .html → your default browser
    return "browser", page
