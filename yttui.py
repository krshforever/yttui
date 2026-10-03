#!/usr/bin/env python3
"""
yttui - interactive yt-dlp front-end.

Why it shells out to the yt-dlp CLI instead of using the Python API:
the Python API silently ignores ~/.config/yt-dlp/config (verified -- js_runtimes
came back as {'deno': {}} and remote_components as set()). Using the CLI means
one config file governs both this app and your terminal.

Threading: subprocesses run on Textual worker threads and only write to a plain
dict. A UI-thread timer copies that dict into the widgets, so no widget is ever
touched off the main thread.

Author: @krshforever
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import urllib.request
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Log,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

# --- Config (mirrors ~/.config/yt-dlp/functions.sh) ----------------------
DOWNLOAD_DIR = Path.home() / "Downloads"
BROWSER = "brave"
BROWSER_PROFILE = "Default"
SEARCH_COUNT = 10

# Thumbnail budget in terminal CELLS. Terminal cells are roughly twice as
# tall as they are wide, and a half-block carries two pixels vertically, so a
# cols x rows cell block yields a cols x (rows*2) pixel image.
THUMB_MAX_COLS = 70
THUMB_MAX_ROWS = 20

MEDIA_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".part",
                  ".ytdl", ".mp3", ".m4a", ".opus", ".flac", ".wav")

AUDIO_TIERS = [
    ("MP3 320 kbps", "mp3", "320"),
    ("MP3 192 kbps", "mp3", "192"),
    ("MP3 128 kbps", "mp3", "128"),
    ("Opus 160 kbps", "opus", "160"),
    ("M4A 128 kbps", "m4a", "128"),
    ("FLAC (lossless)", "flac", None),
]

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def normalize_query(text: str) -> str:
    text = text.strip()
    if not text or is_url(text):
        return text
    # A pasted link that lost its scheme (youtube.com/watch?v=x) must stay a
    # URL; otherwise it would be turned into a nonsense search term.
    if "://" in text or text.startswith("//"):
        return text
    return f"ytsearch1:{text}"


def fmt_size(num) -> str:
    if not num or num < 0:
        return "?"
    v = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def fmt_duration(seconds) -> str:
    if not seconds:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_views(n) -> str:
    if not n:
        return ""
    n = int(n)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(n)


def _loads(raw: str) -> dict:
    """Parse yt-dlp's JSON, tolerating any stray leading output.

    Warnings or progress notices can precede the document, so locate the
    outermost brace instead of assuming stdout starts with '{'.
    """
    raw = (raw or "").strip()
    if not raw:
        raise RuntimeError("yt-dlp returned no data")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise RuntimeError(
                f"could not parse yt-dlp output: {raw[:80]!r}") from None
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            raise RuntimeError(
                f"truncated yt-dlp JSON ({len(raw)} bytes)") from None


def estimate_from_bitrate(kbps, seconds) -> int | None:
    """Estimate bytes from a bitrate in kbps (yt-dlp's tbr/vbr/abr unit).

    bytes = kbps * 1000 bits/kbps * seconds / 8 bits/byte.
    The *1000 is essential: without it every estimate was 1000x too small,
    which showed up as "2160p -> 4.4 MB".
    """
    if kbps and seconds:
        return int(float(kbps) * 1000 * seconds / 8)
    return None


# --------------------------------------------------------------- probing
def _base_cmd(cookies: bool) -> list[str]:
    # -J is essential: without it yt-dlp prints human-readable log lines and
    # json.loads() dies with "Expecting value: line 1 column 2".
    cmd = ["yt-dlp", "-J", "--no-playlist", "--skip-download",
           "--no-simulate", "--no-warnings"]
    if cookies:
        cmd += ["--cookies-from-browser", f"{BROWSER}:{BROWSER_PROFILE}"]
    return cmd


def probe_media(url: str, cookies: bool) -> dict:
    """Full format list for one video (and only one, never a whole playlist)."""
    proc = subprocess.run(_base_cmd(cookies) + [url],
                          capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        lines = [ln for ln in err.splitlines() if ln.strip()]
        raise RuntimeError(lines[-1][:180] if lines else "probe failed")

    info = _loads(proc.stdout)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("no entries found")
        first = entries[0]
        if not first.get("formats"):
            proc2 = subprocess.run(
                _base_cmd(cookies) + [first.get("webpage_url") or first.get("url")],
                capture_output=True, text=True, timeout=240)
            if proc2.returncode == 0:
                first = _loads(proc2.stdout)
        info = first
    return info


def probe_search(query: str, cookies: bool, limit: int = SEARCH_COUNT) -> list[dict]:
    """Shallow search: N results, no per-video probing. Fast.

    --flat-playlist is what keeps this quick: without it yt-dlp fetches full
    format lists for every result, which is ~10x the network time.
    """
    cmd = ["yt-dlp", f"--flat-playlist", "--dump-single-json",
           f"ytsearch{limit}:{query}"]
    if cookies:
        cmd += ["--cookies-from-browser", f"{BROWSER}:{BROWSER_PROFILE}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        lines = [ln for ln in err.splitlines() if ln.strip()]
        raise RuntimeError(lines[-1][:180] if lines else "search failed")
    info = _loads(proc.stdout)
    return [e for e in (info.get("entries") or []) if e]


def _fmt_size_of(fmt: dict, duration: int) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    return estimate_from_bitrate(fmt.get("tbr") or fmt.get("vbr"), duration)


def build_tiers(info: dict) -> list[dict]:
    """One row per distinct resolution, best format at that resolution.

    Iterate the heights that actually exist rather than a fixed cap list: a cap
    list yields duplicate rows (a 1440 cap and a 1080 cap both resolve to 1080p
    when nothing between them is offered), which is exactly the clutter this
    table exists to avoid.
    """
    formats = info.get("formats") or []
    duration = int(info.get("duration") or 0)

    audio_only = [f for f in formats
                  if (f.get("acodec") or "none") != "none"
                  and (f.get("vcodec") or "none") == "none"]
    best_audio = max(audio_only,
                     key=lambda f: (f.get("abr") or f.get("tbr") or 0),
                     default=None)
    audio_size = _fmt_size_of(best_audio, duration) if best_audio else None

    by_height: dict[int, list[dict]] = {}
    for f in formats:
        h = f.get("height")
        if h and (f.get("vcodec") or "none") != "none":
            by_height.setdefault(int(h), []).append(f)

    def rank(f: dict) -> tuple:
        # Prefer video-only: audio gets merged in at the best bitrate rather
        # than taking whatever the muxed variant happened to carry. filesize
        # breaks ties so the pick is deterministic when fps/tbr are absent.
        return ((f.get("acodec") or "none") == "none",
                f.get("fps") or 0,
                f.get("tbr") or f.get("vbr") or 0,
                f.get("filesize") or f.get("filesize_approx") or 0)

    rows = []
    for height in sorted(by_height, reverse=True):
        best = max(by_height[height], key=rank)
        vsize = _fmt_size_of(best, duration)
        muxed = (best.get("acodec") or "none") != "none"
        if muxed:
            total, note = vsize, "single file"
        else:
            total = (vsize + audio_size) if (vsize and audio_size) else None
            note = "+ audio merged" if audio_size else "+ audio (size unknown)"
        rows.append({
            "kind": "video", "height": height, "fps": best.get("fps") or 0,
            "ext": best.get("ext"),
            "vcodec": (best.get("vcodec") or "?").split(".")[0],
            "acodec": (best.get("acodec") or "none").split(".")[0],
            "size": total, "note": note,
            "selector": f"bv*[height<={height}]+ba/b[height<={height}]",
            "label": f"{height}p",
        })
    return rows


def audio_rows(duration: int) -> list[dict]:
    rows = []
    for label, codec, kbps in AUDIO_TIERS:
        size = estimate_from_bitrate(kbps, duration)
        rows.append({"kind": "audio", "label": label, "codec": codec,
                     "size": size, "selector": "bestaudio/best",
                     "note": "lossless" if kbps is None else f"{kbps} kbps"})
    return rows


def parse_progress_payload(payload: str) -> dict | None:
    """Parse one yt-dlp progress line into queue-update fields.

    Kept as a free function (not a method) so it is testable without a running
    app or a worker thread -- both of which _queue_progress requires.

    The template yields 7 fields after the "PGL|" prefix is stripped:
    status, downloaded_bytes, total_bytes, total_bytes_estimate, speed, eta,
    filename. Returns None for anything short or malformed.
    """
    parts = (payload or "").split("|")
    if len(parts) < 7:
        return None

    def num(x):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return 0

    status, done, total, test, speed, eta, fname = (
        parts[0], num(parts[1]), num(parts[2]), num(parts[3]),
        num(parts[4]), num(parts[5]), parts[6],
    )
    total = total or test
    # downloaded can exceed total (yt-dlp revises totals mid-stream), so
    # clamp rather than letting the bar run past its end.
    pct = min(100.0, (done / total * 100)) if total else 0.0
    return {
        "pct": pct,
        "speed": f"{speed / 1048576:.2f} MB/s" if speed else "",
        "eta": f"{eta}s left" if eta else "",
        "file": os.path.basename(fname),
        "path": fname,
        "status": "done" if status == "finished" else "downloading",
    }


# ------------------------------------------------------------- thumbnail
def fetch_thumb(url: str, dest_name: str = "yttui-thumb.jpg") -> Path | None:
    if not url:
        return None
    dest = Path(os.environ.get("TMPDIR", "/tmp")) / dest_name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yttui"})
        with urllib.request.urlopen(req, timeout=25) as r:
            dest.write_bytes(r.read())
        return dest
    except Exception:
        return None


def thumb_cells(img_w: int, img_h: int,
                max_cols: int = THUMB_MAX_COLS,
                max_rows: int = THUMB_MAX_ROWS,
                braille: bool = True) -> tuple[int, int]:
    """Cell counts for an image, clamped to what the cell budget can show.

    Braille mode (U+2800) gives each cell a 2x4 dot grid, so the sample grid is
    (2*cols) x (4*rows) -- twice the linear resolution of half-blocks, which
    only manage 1x2. Both use SQUARE samples, so to show a w:h image
    undistorted:

        braille:     2*cols / (4*rows) == w / h  =>  cols == 2 * rows * (w/h)
        half-block:      cols / (2*rows) == w / h  =>  cols == 2 * rows * (w/h)

    Both land on the same formula; only the resulting detail differs.

    Beyond roughly 5:1 the width cap forces fewer than 4 rows, so the aspect is
    clamped to the most extreme value actually renderable and the renderer
    centre-crops to it. Stretching would misrepresent the image.
    """
    aspect = (img_w / img_h) if img_w and img_h else 16 / 9
    cols = max(8, round(max_rows * 2 * aspect))
    if cols > max_cols:
        cols = max_cols
    rows = max(4, round(cols / (2 * aspect)))
    return cols, rows


def _thumb_grid_aspect(cols: int, rows: int, braille: bool) -> float:
    """Aspect of the sample grid these cells actually produce."""
    return (2 * cols) / (4 * rows) if braille else cols / (2 * rows)


# Braille bit layout (U+2800 + n). Bit order is column-major within rows:
#   0 1   row 0
#   2 3   row 1
#   4 5   row 2
#   6 7   row 3
_BRAILLE_BITS = ((0, 0, 0x01), (0, 1, 0x02), (1, 0, 0x04), (1, 1, 0x08),
                 (0, 2, 0x10), (0, 3, 0x20), (1, 2, 0x40), (1, 3, 0x80))


def render_halfblocks(path: Path | None,
                      max_cols: int = THUMB_MAX_COLS,
                      max_rows: int = THUMB_MAX_ROWS,
                      braille: bool = True) -> tuple[Text | None, int, int]:
    """Turn an image into text cells.

    braille=True  -> U+2800 cells, each a 2x4 dot grid. The top 2x2 dots are
                     painted in the foreground colour and the bottom 2x2 in the
                     background, so one cell carries 4 colour samples instead
                     of half-block's 2. Roughly double the detail per axis,
                     which is the difference between "recognisable" and
                     "flat horizontal streaks".
    braille=False -> '▀' half-blocks: top pixel foreground, bottom background.
                     Kept as a fallback for fonts with no braille glyphs.

    Returns (text, cols, rows) so the caller can size the widget to match.
    """
    if not path:
        return None, 0, 0
    try:
        from PIL import Image
    except ImportError:
        return None, 0, 0
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None, 0, 0

    cols, rows = thumb_cells(*img.size, max_cols, max_rows)
    target = _thumb_grid_aspect(cols, rows, braille)

    # Centre-crop to the aspect we can actually render, THEN resize. Cropping
    # after resizing is what stretched thumbnails in earlier versions.
    src_w, src_h = img.size
    if src_w / src_h > target:
        new_w = max(1, int(round(src_h * target)))
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    elif src_w / src_h < target:
        new_h = max(1, int(round(src_w / target)))
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    txt = Text()
    if braille:
        img = img.resize((cols * 2, rows * 4), Image.LANCZOS)
        px = img.load()

        def avg(xs, ys):
            r = g = b = n = 0
            for yy in ys:
                for xx in xs:
                    c = px[xx, yy]
                    r += c[0]
                    g += c[1]
                    b += c[2]
                    n += 1
            return r // n, g // n, b // n

        for cy in range(rows):
            for cx in range(cols):
                # Top 2x2 (dots 0-3) -> fg, bottom 2x2 (dots 4-7) -> bg.
                fx = (cx * 2, cx * 2 + 1)
                fy = (cy * 4, cy * 4 + 1)
                bx = (cx * 2, cx * 2 + 1)
                by = (cy * 4 + 2, cy * 4 + 3)
                fr, fg_, fb = avg(fx, fy)
                br, bg_, bb = avg(bx, by)
                # Assign each sub-dot to whichever of the two it resembles,
                # which dithers gradients instead of banding them.
                pattern = 0
                for ox, oy, bit in _BRAILLE_BITS:
                    c = px[cx * 2 + ox, cy * 4 + oy]
                    if (c[0] - fr) ** 2 + (c[1] - fg_) ** 2 + \
                            (c[2] - fb) ** 2 <= \
                            (c[0] - br) ** 2 + (c[1] - bg_) ** 2 + \
                            (c[2] - bb) ** 2:
                        pattern |= bit
                txt.append(chr(0x2800 + pattern),
                           style=f"#{fr:02x}{fg_:02x}{fb:02x} on "
                                 f"#{br:02x}{bg_:02x}{bb:02x}")
            txt.append("\n")
        return txt, cols, rows

    img = img.resize((cols, rows * 2), Image.LANCZOS)
    px = img.load()
    for cy in range(rows):
        for cx in range(cols):
            tr, tg, tb = px[cx, cy * 2]
            br, bg_, bb = px[cx, cy * 2 + 1]
            txt.append("▀",
                       style=f"#{tr:02x}{tg:02x}{tb:02x} on "
                             f"#{br:02x}{bg_:02x}{bb:02x}")
        txt.append("\n")
    return txt, cols, rows


class YtTui(App):
    TITLE = "yttui"
    SUB_TITLE = "@krshforever"

    CSS = """
    Screen { background: $surface; }

    /* ---- query bar ---- */
    #query-row { height: 3; padding: 0 1; }
    #query { width: 1fr; border: round $accent; }
    #query:focus { border: round $accent-lighten-2; }
    #fetch { width: 14; min-width: 14; margin-left: 1; }

    /* ---- what to fetch ---- */
    #opts-row { height: 3; padding: 0 1; }
    #opts-row Checkbox { margin-right: 2; }
    #langs { width: 20; margin-left: 1; }

    /* ---- preview panel ---- */
    /* Fixed height, NOT auto: an auto-height panel expands to fill the
       screen and pushes the tables off the bottom. */
    #preview {
        height: 22;
        border: round $primary;
        background: $panel;
        margin: 0 1;
    }
    #thumb { width: 30; padding: 0 1; }
    #info-scroll { width: 1fr; }
    #info { width: 1fr; padding: 1 2; }

    #detail {
        height: 3;
        padding: 0 2;
        color: $text-muted;
        background: $boost;
    }

    /* ---- quality tables ---- */
    Tabs { height: 3; }
    TabPane { padding: 0 1; }
    DataTable {
        height: 1fr;
        background: $surface;
        border: round $panel;
    }
    DataTable > .datatable--header { background: $boost; color: $text; }
    DataTable > .datatable--cursor { background: $accent 30%; }

    /* ---- downloads: permanent panel below the quality table ---- */
    #downloads {
        height: 1fr;
        min-height: 5;
        border: round $success;
        margin: 0 1;
    }
    #downloads-title {
        height: 1;
        color: $text-muted;
        background: $boost;
    }
    #dtab { height: 1fr; border: none; background: $surface; }

    /* ---- progress ---- */
    #progress-row { height: 3; padding: 0 1; }
    ProgressBar { width: 1fr; }
    #dl-btn { width: 18; min-width: 18; margin-left: 1; }

    /* Hidden until toggled: at 50-row terminals a fixed log competes with
       the downloads panel and pushed itself off-screen entirely. */
    #log {
        display: none;
        height: 10;
        border: round $panel;
        background: $panel;
        margin: 0 1;
    }
    #log.visible { display: block; }

    Header { background: $boost; }
    Footer { background: $boost; }

    .hidden { display: none; }
    """

    BINDINGS = [
        ("f", "fetch", "Fetch"),
        ("d", "download", "Download"),
        ("enter", "open", "Open"),
        ("t", "show_video", "Video"),
        ("a", "show_audio", "Audio"),
        ("w", "show_downloads", "Downloads"),
        ("b", "back", "Back"),
        ("c", "cancel", "Cancel"),
        ("l", "clear_log", "Log"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._prog: dict = {"status": "idle", "pct": 0.0, "speed": "",
                            "eta": "", "file": ""}
        # One entry per download. Each owns its own worker group and process
        # handle so downloads cannot cancel each other or a probe.
        self._queue: list[dict] = []
        self._procs: dict[int, subprocess.Popen] = {}
        self._next_id = 1
        self._info: dict | None = None
        self._results: list[dict] = []
        self._current_url: str = ""
        self._vrows: list[dict] = []
        self._arows: list[dict] = []
        self._selected: dict | None = None

    # ------------------------------------------------------------------ UI
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="query-row"):
            yield Input(placeholder="Paste a URL, or type to search YouTube…",
                        id="query")
            yield Button("Fetch", id="fetch", variant="primary")

        with Horizontal(id="opts-row"):
            yield Checkbox("Video", value=True, id="dl-video")
            yield Checkbox("Audio", value=False, id="dl-audio")
            yield Checkbox("Subtitles", value=False, id="dl-subs")
            yield Checkbox("Thumbnail", value=False, id="dl-thumb")
            yield Checkbox("Tags", value=True, id="meta")
            yield Checkbox("Raw", value=False, id="raw")
            yield Checkbox("Cookies", value=False, id="cookies")
            yield Input(value="en.*,en", id="langs", compact=True)

        # Results view (keyword searches only).
        yield DataTable(id="results", cursor_type="row")

        # Detail view (thumbnail + metadata + format tabs).
        with Horizontal(id="preview", classes="hidden"):
            yield Static("", id="thumb", markup=False)
            yield VerticalScroll(Static("", id="info", markup=False),
                                 id="info-scroll")
        yield Static("", id="detail", classes="hidden")

        with TabbedContent(initial="video", classes="hidden", id="tabs"):
            with TabPane("Video", id="video"):
                yield DataTable(id="vtab", cursor_type="row")
            with TabPane("Audio", id="audio"):
                yield DataTable(id="atab", cursor_type="row")

        # Downloads is NOT a tab: it lives permanently below the quality
        # table so progress is always visible instead of hidden behind a tab.
        with Vertical(id="downloads", classes="hidden"):
            yield Static("Downloads", id="downloads-title")
            yield DataTable(id="dtab", cursor_type="row")

        with Horizontal(id="progress-row", classes="hidden"):
            yield ProgressBar(id="bar", show_eta=False)
            yield Button("Download", id="dl-btn", variant="success")

        yield Log(id="log", highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        r = self.query_one("#results", DataTable)
        r.add_columns("#", "Title", "Channel", "Length", "Views")
        for tid in ("vtab", "atab"):
            t = self.query_one(f"#{tid}", DataTable)
            t.add_columns("Quality", "FPS", "Ext", "Video", "Audio",
                          "Size", "Detail")
        self.query_one("#dtab", DataTable).add_columns(
            "", "Item", "Status", "Progress", "Speed / ETA", "Detail")
        self.query_one("#query", Input).focus()
        self.set_interval(0.25, self._pump)
        self.log_line(f"Saving to {DOWNLOAD_DIR}", "ready")
        self._show("results")

    # -------------------------------------------------------- view switching
    def _show(self, which: str) -> None:
        detail = which == "detail"
        self.query_one("#results", DataTable).set_class(detail, "hidden")
        for wid in ("#preview", "#detail", "#tabs", "#progress-row",
                    "#downloads"):
            self.query_one(wid).set_class(not detail, "hidden")
        if detail:
            self.query_one("#vtab", DataTable).focus()
        else:
            self.query_one("#results", DataTable).focus()

    # -------------------------------------------------------------- helpers
    def log_line(self, msg: str, kind: str = "info") -> None:
        """Write one log line.

        Log in Textual 8 does not parse Rich markup (it strips control codes and
        renders a plain Text), so colour tags would print literally. Use a
        leading glyph for status instead.
        """
        glyph = {"ready": "•", "error": "✗", "ok": "✓", "warn": "!"}.get(
            kind, " ")
        try:
            self.query_one("#log", Log).write_line(f"{glyph} {msg}")
        except Exception:
            # Log before the screen exists (early startup, or a reaper test
            # running outside the app context). Never fatal.
            pass

    def set_detail(self, msg: str, kind: str = "info") -> None:
        colour = {"error": "red", "ok": "green", "warn": "yellow"}.get(kind, "")
        try:
            self.query_one("#detail", Static).update(
                Text(msg, style=colour) if colour else Text(msg))
        except Exception:
            pass

    def _cookies(self) -> bool:
        return bool(self.query_one("#cookies", Checkbox).value)

    # ---------------------------------------------------------------- fetch
    @on(Button.Pressed, "#fetch")
    @on(Input.Submitted, "#query")
    def action_fetch(self) -> None:
        raw = self.query_one("#query", Input).value.strip()
        if not raw:
            self.set_detail("Enter a URL or search phrase first.", "warn")
            return
        # A pasted link goes straight to the download view; a keyword produces
        # a result list first.
        self.fetch_worker(raw)

    @work(thread=True, exclusive=True, group="probe")
    def fetch_worker(self, raw: str) -> None:
        try:
            if is_url(raw):
                self.call_from_thread(self.set_detail, f"Fetching {raw} …")
                info = probe_media(raw, self._cookies())
                self.call_from_thread(self._populate_media, info)
            else:
                self.call_from_thread(self.set_detail,
                                      f'Searching "{raw}" …')
                results = probe_search(raw, self._cookies())
                self.call_from_thread(self._populate_results, results, raw)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.set_detail, f"Failed: {exc}", "error")
            self.call_from_thread(self.log_line, f"probe: {exc}", "error")

    def _populate_results(self, results: list[dict], query: str) -> None:
        self._results = results
        t = self.query_one("#results", DataTable)
        t.clear()
        for i, e in enumerate(results):
            views = fmt_views(e.get("view_count"))
            t.add_row(
                str(i + 1),
                Text((e.get("title") or "?")[:70], style="bold"),
                (e.get("uploader") or e.get("channel") or "")[:28],
                fmt_duration(e.get("duration")),
                views,
                key=f"r:{i}",
            )
        self._show("results")
        self.set_detail(
            f"{len(results)} results for “{query}”. "
            "↑/↓ then Enter to open, B to go back.", "ok")

    @on(DataTable.RowHighlighted, "#results")
    def _results_highlight(self, ev: DataTable.RowHighlighted) -> None:
        key = str(ev.row_key.value)
        if not key.startswith("r:"):
            return
        try:
            e = self._results[int(key[2:])]
        except (ValueError, IndexError):
            return
        self.set_detail(
            f"{e.get('title')} · {fmt_duration(e.get('duration'))} — "
            "press Enter to open")

    @on(DataTable.RowSelected, "#results")
    def _results_selected(self, ev: DataTable.RowSelected) -> None:
        """Enter on the results table.

        DataTable binds 'enter' itself (to emit RowSelected), and a focused
        widget's binding beats the App-level ("enter", "open") binding, so
        action_open() never ran from the keyboard. Handle the message instead.
        """
        self._open_row(ev.cursor_row)

    @on(DataTable.RowSelected, "#vtab")
    @on(DataTable.RowSelected, "#atab")
    def _format_selected(self, ev: DataTable.RowSelected) -> None:
        """Enter on a format row downloads it, matching the D key."""
        self.action_download()

    def action_open(self) -> None:
        """Open the highlighted search result (Enter or the binding)."""
        t = self.query_one("#results", DataTable)
        if t.row_count == 0 or not t.display:
            return
        self._open_row(t.cursor_row)

    def _open_row(self, row: int | None) -> None:
        """Probe one search result fully. App thread."""
        if row is None or not (0 <= row < len(self._results)):
            return
        entry = self._results[row]
        url = entry.get("url") or entry.get("webpage_url")
        if not url:
            self.set_detail("That result has no URL.", "error")
            return
        self._current_url = url
        self.set_detail(f"Fetching {entry.get('title')} …")
        self.open_worker(url)

    @work(thread=True, exclusive=True, group="probe")
    def open_worker(self, url: str) -> None:
        try:
            info = probe_media(url, self._cookies())
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.set_detail, f"Failed: {exc}", "error")
            self.call_from_thread(self.log_line, f"open: {exc}", "error")
            return
        self.call_from_thread(self._populate_media, info)

    def action_back(self) -> None:
        if self._results:
            self._show("results")
            self.set_detail("Back to results.")
        else:
            self.set_detail("Nothing to go back to.", "warn")

    # -------------------------------------------------------- media detail
    def _populate_media(self, info: dict) -> None:
        """App thread: fills the thumbnail panel, metadata and format tabs."""
        self._info = info
        self._current_url = (info.get("webpage_url")
                             or info.get("original_url") or self._current_url)
        raw = bool(self.query_one("#raw", Checkbox).value)

        duration = int(info.get("duration") or 0)
        head = Text()
        head.append(info.get("title") or "?", style="bold")
        if info.get("uploader") or info.get("channel"):
            head.append(f"\n{info.get('uploader') or info.get('channel')}",
                        style="italic")
        bits = [fmt_duration(duration)]
        if info.get("view_count"):
            bits.append(f"{int(info['view_count']):,} views")
        if info.get("like_count"):
            bits.append(f"{int(info['like_count']):,} likes")
        if info.get("upload_date"):
            bits.append(str(info["upload_date"]))
        if info.get("extractor_key"):
            bits.append(str(info["extractor_key"]).replace("Youtube", "YouTube"))
        if info.get("description"):
            desc = " ".join(str(info["description"]).split())
            head.append("\n" + desc[:220] + ("…" if len(desc) > 220 else ""),
                        style="dim")
        head.append(f"\n→ {DOWNLOAD_DIR}", style="cyan")
        self.query_one("#info", Static).update(head)

        self._vrows = [] if raw else build_tiers(info)
        self._fill_video(raw)
        self._arows = audio_rows(duration)
        self._fill_audio()
        self._show("detail")

        n = len(self._vrows)
        self.set_detail(
            f"{n} quality tiers. ↑/↓ then Enter, D to download, B to go back."
            if not raw else "Raw formats ready. D to download.", "ok")
        self.thumb_worker(info.get("thumbnail") or "")

    def _fill_video(self, raw: bool) -> None:
        t = self.query_one("#vtab", DataTable)
        t.clear()
        if raw:
            seen = set()
            for f in (self._info or {}).get("formats") or []:
                fid = f.get("format_id")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                t.add_row(
                    str(f.get("height") or "-"), str(f.get("fps") or "-"),
                    str(f.get("ext") or "-"),
                    (f.get("vcodec") or "none").split(".")[0],
                    (f.get("acodec") or "none").split(".")[0],
                    fmt_size(f.get("filesize") or f.get("filesize_approx")),
                    fid, key=f"raw:{fid}",
                )
        else:
            for i, r in enumerate(self._vrows):
                t.add_row(
                    Text(r["label"], style="bold"), str(r["fps"] or "-"),
                    str(r["ext"] or "-"), r["vcodec"], r["acodec"],
                    Text(fmt_size(r["size"]), style="green"), r["note"],
                    key=f"v:{i}",
                )

    def _fill_audio(self) -> None:
        t = self.query_one("#atab", DataTable)
        t.clear()
        for i, r in enumerate(self._arows):
            t.add_row(
                Text(r["label"], style="bold"), "-", r["codec"], "-", "-",
                Text(fmt_size(r["size"]), style="green"), r["note"],
                key=f"a:{i}",
            )

    @on(Checkbox.Changed, "#raw")
    def _raw_toggle(self, ev: Checkbox.Changed) -> None:
        if self._info:
            self._fill_video(ev.value)

    @work(thread=True, exclusive=True, group="thumb")
    def thumb_worker(self, url: str) -> None:
        """Its own thread: network fetch + PIL resize would block the UI.

        Separate group from "net" so a slow thumbnail never cancels an
        in-flight probe (exclusive=True cancels within a group only).
        """
        art, cols, rows = None, 0, 0
        try:
            path = fetch_thumb(url)
            art, cols, rows = render_halfblocks(path)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"thumbnail: {exc}", "warn")
        self.call_from_thread(self._apply_thumb, art, cols, rows)

    def _apply_thumb(self, art, cols: int, rows: int) -> None:
        thumb = self.query_one("#thumb", Static)
        # Size the widget to the real cell grid, otherwise the grid is clipped
        # or stretched to whatever the CSS width happened to be.
        thumb.styles.width = cols
        thumb.styles.height = rows
        if art is None:
            thumb.update(Text("no thumbnail", style="dim"))
            return
        preview = self.query_one("#preview")
        preview.styles.height = max(rows + 2, 8)
        thumb.update(art)

    @on(DataTable.RowHighlighted, "#vtab")
    @on(DataTable.RowHighlighted, "#atab")
    def _highlight(self, ev: DataTable.RowHighlighted) -> None:
        """App thread (a Textual message handler)."""
        key = str(ev.row_key.value)
        if key.startswith("raw:"):
            fid = key[4:]
            self._selected = {"selector": fid, "kind": "video", "label": fid}
            self.set_detail(f"Format {fid} (raw). D to download.")
            return
        # Keys are "<kind>:<index>". Unpack in that order -- reading them the
        # other way round fed "a" into int() and crashed the Audio tab.
        kind, _, idx = key.partition(":")
        try:
            i = int(idx)
        except ValueError:
            return
        rows = self._vrows if kind == "v" else self._arows
        if not (0 <= i < len(rows)):
            return
        self._selected = rows[i]
        if kind == "v":
            self.set_detail(
                f"{rows[i]['label']} · {rows[i]['vcodec']}+"
                f"{rows[i]['acodec']} · {fmt_size(rows[i]['size'])} · "
                f"{rows[i]['note']} — D to download")
        else:
            self.set_detail(f"{rows[i]['label']} · about "
                            f"{fmt_size(rows[i]['size'])} — D to download")

    # ------------------------------------------------------------- download
    @on(Button.Pressed, "#dl-btn")
    def _wanted(self) -> dict:
        """What the user asked to fetch, from the checkbox row."""
        return {k: bool(self.query_one(f"#{k}", Checkbox).value)
                for k in ("dl-video", "dl-audio", "dl-subs", "dl-thumb")}

    def _langs(self) -> str:
        return (self.query_one("#langs", Input).value or "en.*,en").strip()

    def action_download(self, other: str | None = None) -> None:
        """Queue a download. `other` overrides the URL (used by tests)."""
        if not self._current_url:
            self.set_detail("Open a video first.", "warn")
            return

        want = self._wanted()
        if not any(want.values()):
            self.set_detail("Tick something to fetch.", "warn")
            return

        # Audio replaces video unless both are ticked (video wins, since a
        # merged video already carries the audio track).
        want_video = want["dl-video"]
        want_audio = want["dl-audio"] and not want_video
        langs = self._langs()
        embed = bool(self.query_one("#meta", Checkbox).value)
        sel = self._selected
        if not sel and (want_video or want_audio):
            self.set_detail("Pick a quality first.", "warn")
            return
        sel = sel or {"label": "media", "selector": None, "size": None}

        cmd = ["yt-dlp", self._current_url, "--newline",
               "--progress-template",
               "download:PGL|%(progress.status)s|%(progress.downloaded_bytes)s"
               "|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s"
               "|%(progress.speed)s|%(progress.eta)s|%(info.filename)s"]
        if other:
            cmd[1] = other

        label = sel["label"]
        # Two DISTINCT concepts that were previously conflated into one flag:
        #   subs_inline   - put --write-subs on THIS command. Only safe when
        #                   no video is downloading, because a subtitle 429 is
        #                   fatal and would abort the video.
        #   subs_secondpass - run _fetch_subs() after a successful download.
        subs_inline = False
        subs_secondpass = False

        if want_video:
            if not sel.get("selector"):
                self.set_detail("Pick a quality first.", "warn")
                return
            cmd += ["--format", sel["selector"], "--merge-output-format", "mp4"]
            if want["dl-subs"]:
                cmd += ["--no-write-subs", "--no-write-auto-subs"]
                subs_secondpass = True
        elif want_audio:
            codec = sel.get("codec") or "mp3"
            cmd += ["--format", "bestaudio/best", "--extract-audio",
                    "--audio-format", codec]
            if codec != "flac":
                cmd += ["--audio-quality", "0"]
            label = f"{codec} audio"
            subs_inline = want["dl-subs"]
        else:
            # Subtitles and/or thumbnail only: no media stream at all.
            cmd += ["--skip-download"]
            label = "subs" if want["dl-subs"] else "thumb"
            subs_inline = want["dl-subs"]

        if subs_inline:
            cmd += ["--write-subs", "--write-auto-subs",
                    "--sub-langs", langs, "--sub-format", "srt/best",
                    # Safe here: no video is at stake.
                    "--ignore-errors"]
        if want["dl-thumb"]:
            cmd += ["--write-thumbnail", "--convert-thumbnails", "png"]

        if not embed:
            cmd += ["--no-embed-metadata", "--no-embed-thumbnail"]
        if self._cookies():
            cmd += ["--cookies-from-browser", f"{BROWSER}:{BROWSER_PROFILE}"]

        parts = [p for p, on in (("video", want_video),
                                 ("audio", want_audio),
                                 ("subs", subs_inline or subs_secondpass),
                                 ("thumb", want["dl-thumb"])) if on]
        target = "+".join(parts) or "video"

        with self._lock:
            self._prog.update(status="starting", pct=0.0, speed="", eta="",
                              file="")
        self.set_detail("Queued …")
        self.enqueue(cmd, label, sel.get("size"), subs_secondpass, target)

    # ------------------------------------------------------ download queue
    _ITEM_DEFAULTS = {
        "id": 0, "label": "", "cmd": [], "status": "queued", "pct": 0.0,
        "speed": "", "eta": "", "file": "", "size": None, "error": "",
        "code": None, "want_subs": False, "subs": "", "path": "",
        "target": "video",
    }

    def enqueue(self, cmd: list[str], label: str, size=None,
                want_subs: bool = False, target: str = "video") -> None:
        """Add a download to the tracked queue. App thread."""
        with self._lock:
            # Two yt-dlp processes on the SAME output name collide in
            # postprocessing: one deletes the .webp thumbnail while the other
            # is still converting it, killing it with ENOENT. Observed as
            # "No such file or directory: '<name>.png'". Refuse duplicates.
            url = cmd[1] if len(cmd) > 1 else ""
            if any(x["status"] in ("queued", "downloading")
                   and x["cmd"][1:2] == [url] for x in self._queue):
                self.log_line(
                    f"already downloading this one: {label[:40]}", "warn")
                self.set_detail("Already downloading that. Skipped.", "warn")
                return
            item = dict(self._ITEM_DEFAULTS)
            item.update(id=self._next_id, label=label, cmd=list(cmd),
                        size=size, want_subs=want_subs, target=target)
            self._next_id += 1
            self._queue.append(item)
            jid = item["id"]

        # enqueue() runs ON the app thread (action_download is a message
        # handler), so call_from_thread is illegal here -- it raises. Update the
        # row directly.
        self._queue_row_add(jid)
        # Own group per download: exclusive=False so starting a second one
        # never cancels the first, and no shared group to collide with probes.
        self.download_worker(jid, group=f"dl{jid}", exclusive=False)

    def _queue_row_add(self, jid: int) -> None:
        self._render_queue_row(jid)

    def _queue_update(self, jid: int, **kw) -> None:
        """Called from worker threads via call_from_thread."""
        with self._lock:
            for it in self._queue:
                if it["id"] == jid:
                    it.update(kw)
                    break
        self._render_queue_row(jid)

    def _render_queue_row(self, jid: int) -> None:
        try:
            t = self.query_one("#dtab", DataTable)
        except Exception:
            return
        it = next((x for x in self._queue if x["id"] == jid), None)
        if it is None:
            return
        glyph = {"queued": "·", "downloading": "↓", "done": "✓",
                 "error": "✗", "cancelled": "!"}.get(it["status"], "·")
        colour = {"downloading": "yellow", "done": "green",
                  "error": "red", "cancelled": "yellow"}.get(it["status"], "")
        bar = "#" * int(it["pct"] / 5) + "." * (20 - int(it["pct"] / 5))
        speed = " · ".join(x for x in (it["speed"], it["eta"]) if x)
        cells = [
            Text(glyph, style=colour),
            it["label"][:44],
            Text(it["status"], style=colour),
            f"{bar} {it['pct']:5.1f}%",
            speed or "-",
            Text(it["error"][:70] if it["error"]
                 else (f"{it['file'][:52]} · {it['subs']}"[:70]
                       if it["file"] else (it["subs"][:70] or "-"))),
        ]
        key = f"d:{jid}"
        try:
            exists = any(str(k.value) == key for k in list(t.rows))
            if exists:
                t.update_cell(key, *cells, update_width=True)
            else:
                t.add_row(*cells, key=key)
        except Exception:
            # A progress update must never take the app down.
            pass

    @work(thread=True)
    def download_worker(self, jid: int, **kw) -> None:
        with self._lock:
            it = next((x for x in self._queue if x["id"] == jid), None)
            cmd = list(it["cmd"]) if it else []
        self.call_from_thread(self._queue_update, jid, status="downloading")
        code, error = 1, ""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            self._procs[jid] = proc
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("PGL|"):
                    self._queue_progress(jid, line[4:])
                elif "ERROR:" in line:
                    error = line.split("ERROR:", 1)[1].strip()[:200]
                    self.call_from_thread(self.log_line, line[:150], "error")
                elif line.strip():
                    self.call_from_thread(self.log_line, line[:150])
            code = proc.wait()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:200]
        finally:
            self._procs.pop(jid, None)

        status = "done" if code == 0 else ("cancelled" if code < 0 else "error")
        # A killed download leaves yt-dlp's per-stream files behind (e.g. an
        # audio-only .f251.webm sitting next to the finished video). Reap them.
        if status == "done":
            self.call_from_thread(self._reap_orphans, jid)
        else:
            self.call_from_thread(self._reap_orphans, jid, only=[".f"])
        if status == "error":
            # The error text is the whole point; make sure it can be read.
            self.call_from_thread(self._reveal_log)
            note = self.call_from_thread(self._drop_corrupt_output, jid)

        # Subtitles as a separate best-effort pass. --ignore-errors is safe
        # here because nothing but the .srt is at stake.
        subs_note = ""
        if code == 0 and it.get("want_subs"):
            got = self._fetch_subs(cmd)
            subs_note = got or "subs unavailable (rate limited)"

        self.call_from_thread(
            self._queue_update, jid, status=status, code=code,
            error="" if code == 0 else error or f"yt-dlp exited {code}",
            subs=subs_note,
            pct=100.0 if code == 0 else 0.0, speed="", eta="",
        )

    def _fetch_subs(self, video_cmd: list[str]) -> str:
        """Second pass: subtitles only. Returns a short note."""
        url = video_cmd[1]
        fmt = next((c for i, c in enumerate(video_cmd)
                    if i > 1 and video_cmd[i - 1] == "--format"), "best")
        langs = "en.*,en"
        cmd = ["yt-dlp", url, "--skip-download", "--format", fmt,
               "--write-subs", "--write-auto-subs", "--sub-langs", langs,
               "--sub-format", "srt/best", "--ignore-errors",
               "--no-simulate", "--no-warnings"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=300)
        except Exception as exc:  # noqa: BLE001
            return f"subs failed: {exc}"[:80]
        wrote = [ln for ln in (proc.stdout or "").splitlines()
                 if "Writing video subtitles" in ln or "Writing automatic"
                 in ln]
        return f"{len(wrote)} subtitle file(s)" if wrote else \
            "subs unavailable (rate limited)"

    def _drop_corrupt_output(self, jid: int) -> str:
        """Delete truncated output files left by an interrupted run.

        Why this is necessary: `--no-overwrites` (set in the shared yt-dlp
        config) makes yt-dlp treat an existing file as already-downloaded, skip
        the fetch, and run postprocessing against it. If a previous run was
        killed mid-merge, that file has no moov atom, so every later attempt
        fails with "Postprocessing: Error opening input files: Invalid data
        found when processing input" -- forever, with no way out but deleting
        it by hand.

        The tracked `path` is NOT reliable here: it comes from the progress
        template's info.filename, which for a merged download is the last
        *stream* (e.g. ".f251.webm"), already deleted by the time the error
        fires. So candidates are matched on the video id from the URL instead.

        Only files ffprobe cannot read are removed, so valid media is spared.
        """
        with self._lock:
            item = next((x for x in self._queue if x["id"] == jid), None)
        if not item:
            return ""
        url = item["cmd"][1] if len(item["cmd"]) > 1 else ""
        vid = ""
        m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,})", url)
        if m:
            vid = m.group(1)

        candidates: list[Path] = []
        if DOWNLOAD_DIR.is_dir():
            # Match on the video id so only THIS download's files are probed.
            pattern = f"[{vid}]*" if vid else ""
            for path in DOWNLOAD_DIR.glob(f"*{pattern}.*"):
                if path.suffix.lower() in MEDIA_SUFFIXES:
                    candidates.append(path)

        removed = []
        for path in candidates:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=60)
            if probe.returncode == 0:
                continue                      # readable: leave it alone
            try:
                path.unlink()
                removed.append(path.name[:40])
            except OSError:
                pass
        if not removed:
            return ""
        msg = f"removed truncated output {', '.join(removed[:2])}"
        msg += " (it was blocking retries)"
        self.log_line(msg, "warn")
        return msg

    def _reap_orphans(self, jid: int, only: list[str] | None = None) -> None:
        """Delete yt-dlp's leftover per-stream files for this download.

        Matches on the output filename yt-dlp reported during the download,
        NOT the display label (which is just "1080p" etc). yt-dlp names
        intermediates "Title [id].f251.webm" alongside "Title [id].mp4", so
        comparing the stem of the finished file catches every stream leftover.
        """
        with self._lock:
            it = next((x for x in self._queue if x["id"] == jid), None)
            target = (it or {}).get("path") or ""
        if not target:
            return
        root = Path(target).parent
        stem = Path(target).stem
        if not root.is_dir() or not stem:
            return
        removed = 0
        finished = Path(target).exists()
        for path in root.iterdir():
            if not path.is_file() or path == Path(target):
                continue
            name = path.name
            # yt-dlp's per-stream intermediates: "Title [id].f251.webm".
            if f"{stem}.f" in name:
                pass
            # Thumbnail source left behind when --embed-thumbnail could not
            # finish: "Title [id].webp". Only safe to drop once the finished
            # video exists, so a real standalone image is never deleted.
            elif finished and name.startswith(f"{stem}.") and \
                    path.suffix.lower() in (".webp", ".jpg", ".jpeg"):
                pass
            else:
                continue
            if only and not any(t in name for t in only):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            self.log_line(
                f"cleaned {removed} leftover file(s) for {stem[:40]}", "ok")

    def _queue_progress(self, jid: int, payload: str) -> None:
        parsed = parse_progress_payload(payload)
        if parsed is None:
            return
        self.call_from_thread(self._queue_update, jid, **parsed)

    def action_cancel(self) -> None:
        """Cancel the highlighted download, or everything if none selected."""
        t = self.query_one("#dtab", DataTable)
        running = [x for x in self._queue
                   if x["status"] in ("downloading", "queued")]
        target = None
        if t.row_count:
            row = t.cursor_row
            if row is not None and 0 <= row < len(self._queue):
                cand = self._queue[row]
                if cand["status"] in ("downloading", "queued"):
                    target = cand["id"]
        if target is None and len(running) == 1:
            target = running[0]["id"]
        if target is None:
            self.set_detail("No download running to cancel.", "warn")
            return
        proc = self._procs.get(target)
        if proc and proc.poll() is None:
            proc.terminate()
        self.log_line(f"cancelled download #{target}", "warn")
        self.set_detail(f"Cancelled #{target}.", "warn")

    def action_show_downloads(self) -> None:
        """Downloads is a permanent panel now, so this just focuses it."""
        self.query_one("#dtab", DataTable).focus()


    # ----------------------------------------------------------------- pump
    def _pump(self) -> None:
        """Drive the shared bar from whichever download is active."""
        try:
            with self._lock:
                active = [x for x in self._queue
                          if x["status"] in ("downloading", "queued")]
                n_done = sum(1 for x in self._queue if x["status"] == "done")
                n_err = sum(1 for x in self._queue
                            if x["status"] in ("error", "cancelled"))
                total = len(self._queue)
            bar = self.query_one("#bar", ProgressBar)
        except Exception:
            return  # never let the timer kill the app
        if active:
            it = active[0]
            bar.update(progress=max(0.0, min(100.0, it["pct"])))
            bits = [f"{it['pct']:5.1f}%"]
            if it["speed"]:
                bits.append(it["speed"])
            if it["eta"]:
                bits.append(it["eta"])
            if it["file"]:
                bits.append(it["file"][:44])
            suffix = (f"  [{len(active)} active, {n_done} done"
                      f"{f', {n_err} failed' if n_err else ''} of {total}]")
            self.set_detail("  ".join(bits) + suffix)
        elif total:
            bar.update(progress=100.0)
            tail = f", {n_err} failed" if n_err else ""
            self.set_detail(f"{n_done} of {total} downloads finished{tail}",
                            "error" if n_err else "ok")
        else:
            bar.update(progress=0.0)

    def action_show_video(self) -> None:
        self.query_one("#tabs").active = "video"
        self.query_one("#vtab", DataTable).focus()

    def action_show_audio(self) -> None:
        self.query_one("#tabs").active = "audio"
        self.query_one("#atab", DataTable).focus()

    def action_clear_log(self) -> None:
        """Toggle the log pane. It competes with the downloads panel for
        vertical space, so it stays out of the way until asked for -- and is
        forced open whenever a download fails, which is exactly when you need
        to read it."""
        log = self.query_one("#log", Log)
        log.set_class(not log.has_class("visible"), "visible")
        if log.has_class("visible"):
            log.clear()

    def _reveal_log(self) -> None:
        try:
            self.query_one("#log", Log).set_class(True, "visible")
        except Exception:
            pass

    @on(DataTable.RowHighlighted, "#dtab")
    def _dtab_highlight(self, ev: DataTable.RowHighlighted) -> None:
        row = ev.cursor_row
        if row is None or not (0 <= row < len(self._queue)):
            return
        it = self._queue[row]
        extra = f" · {it['error']}" if it["error"] else ""
        self.set_detail(f"#{it['id']} {it['label']} — {it['status']}{extra}")


def main() -> None:
    """Entry point for `yttui` and for the pipx console script."""
    YtTui().run()


if __name__ == "__main__":
    main()
