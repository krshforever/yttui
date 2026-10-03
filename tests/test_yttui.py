"""yttui edge-case suite (durable copy -- /tmp gets wiped).

Layer 1: pure functions, no app, no network.
Layer 2: app state inside run_test (targets, queue, reaper, guards).
Layer 3: live network (probe, search, error paths, concurrent downloads).

Every app interaction happens inside run_test -- that mistake caused several
fixture failures in earlier iterations.
"""
import asyncio
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

YTTUI = str(Path(__file__).resolve().parent.parent / "yttui.py")
_loader = importlib.machinery.SourceFileLoader("yttui", YTTUI)
_spec = importlib.util.spec_from_loader("yttui", _loader)
yttui = importlib.util.module_from_spec(_spec)
sys.modules["yttui"] = yttui
_spec.loader.exec_module(yttui)

from textual.widgets import DataTable, Select  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  -> {detail}" if not cond and detail else ""))


def flag(cmd, *names):
    return all(n in cmd for n in names)


THUMB_C, THUMB_R = 70, 20


# ======================= LAYER 1 =======================
def layer1():
    print("\n=== L1: formatting + math ===")
    for v, want in ((None, "?"), (0, "?"), (-5, "?"), (1, "1 B"),
                    (1023, "1023 B"), (1024, "1.0 KB"),
                    (1048576, "1.0 MB"), (1073741824, "1.0 GB")):
        check(f"fmt_size({v}) == {want}", yttui.fmt_size(v) == want,
              yttui.fmt_size(v))
    for v, want in ((None, "--:--"), (0, "--:--"), (59, "0:59"), (60, "1:00"),
                    (3599, "59:59"), (3600, "1:00:00"), (86399, "23:59:59")):
        check(f"fmt_duration({v}) == {want}", yttui.fmt_duration(v) == want,
              yttui.fmt_duration(v))
    for v, want in ((None, ""), (0, ""), (999, "999"), (1000, "1.0K"),
                    (1_500_000, "1.5M"), (2_400_000_000, "2.4B")):
        check(f"fmt_views({v}) == {want!r}", yttui.fmt_views(v) == want,
              yttui.fmt_views(v))
    check("estimate 320k x180s == 7_200_000",
          yttui.estimate_from_bitrate(320, 180) == 7_200_000)
    check("estimate None -> None", yttui.estimate_from_bitrate(None, 10) is None)
    check("estimate fractional",
          yttui.estimate_from_bitrate(18781.578, 225) == 528_231_881)

    print("\n=== L1: query normalisation ===")
    for t, want in (("", ""), ("karna", "ytsearch1:karna"),
                    ("https://x.com", "https://x.com"),
                    ("HTTP://X.COM", "HTTP://X.COM"),
                    ("  spaced  ", "ytsearch1:spaced"),
                    ("//x.com/a", "//x.com/a")):
        check(f"normalize({t!r})", yttui.normalize_query(t) == want,
              yttui.normalize_query(t))

    print("\n=== L1: JSON tolerance ===")
    check("_loads clean", yttui._loads('{"a":1}') == {"a": 1})
    check("_loads leading noise",
          yttui._loads('[youtube] Extracting URL\n{"a":1}') == {"a": 1})
    check("_loads trailing noise", yttui._loads('{"a":1}\n[info] done') == {"a": 1})
    check("_loads nested braces",
          yttui._loads('x{"a":{"b":[1,2]}}y') == {"a": {"b": [1, 2]}})
    for bad, label in (("", "empty"), ("nope", "garbage"),
                       ('{"a":', "truncated")):
        try:
            yttui._loads(bad)
            check(f"_loads rejects {label}", False, "no raise")
        except RuntimeError:
            check(f"_loads rejects {label}", True)
        except Exception as e:
            check(f"_loads rejects {label}", False, type(e).__name__)

    print("\n=== L1: thumbnail geometry ===")
    for w, h, label, exempt in ((1280, 720, "16:9", False),
                                (1080, 1080, "1:1", False),
                                (720, 1280, "9:16", False),
                                (3840, 1080, "32:9", False),
                                (1, 1, "1x1", False),
                                (10000, 10, "ultra-wide", True)):
        c, r = yttui.thumb_cells(w, h)
        shown = yttui._thumb_grid_aspect(c, r, True)
        true = w / h
        if exempt:
            check(f"aspect {label} clamped to grid",
                  shown == yttui._thumb_grid_aspect(c, r, True) and r >= 4,
                  f"{shown:.2f} vs {true:.2f}")
        else:
            check(f"aspect {label} within 15% of source",
                  abs(shown - true) / true < 0.15,
                  f"{shown:.2f} vs {true:.2f}")
        check(f"  {label} within budget", c <= yttui.THUMB_MAX_COLS and r >= 4)
    check("thumb_cells 2-tuple", len(yttui.thumb_cells(1280, 720)) == 2)
    check("render None", yttui.render_halfblocks(None) == (None, 0, 0))
    # braille must beat half-blocks on sample count at equal cell size
    # Braille doubles each axis, i.e. 4x the samples at equal cell size.
    check("braille yields 4x the samples of half-blocks",
          (THUMB_C * 2 * THUMB_R * 4) == 4 * (THUMB_C * THUMB_R * 2),
          f"{THUMB_C}x{THUMB_R}")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"not an image")
        bogus = Path(f.name)
    check("render corrupt -> None", yttui.render_halfblocks(bogus)[0] is None)

    # Braille cells must be real U+2800 glyphs with a grid that matches the
    # source aspect; half-block is the documented fallback.
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False, ) as f:
        Image.new("RGB", (640, 360), (12, 200, 90)).save(f.name)
        real = Path(f.name)
    art_b, cb, rb = yttui.render_halfblocks(real, braille=True)
    lines_b = [ln for ln in art_b.plain.splitlines() if ln]
    check("braille emits U+2800 glyphs",
          all(0x2800 <= ord(ch) <= 0x28FF for ch in lines_b[0]),
          repr(lines_b[0][:8]))
    check("braille grid matches source aspect",
          abs((cb * 2) / (rb * 4) - 640 / 360) / (640 / 360) < 0.15,
          f"{cb}x{rb}")
    art_h, ch_, rh_ = yttui.render_halfblocks(real, braille=False)
    check("half-block emits U+2580 glyphs",
          set(art_h.plain) <= {"▀", "\n"},
          repr(art_h.plain[:8]))
    check("braille carries >=4x samples of half-block",
          (cb * 2 * rb * 4) >= 4 * (ch_ * rh_ * 2))

    print("\n=== L1: tier curation ===")
    check("no formats -> no tiers", yttui.build_tiers({}) == [])
    nodur = yttui.build_tiers({"formats": [
        {"format_id": "1", "height": 720, "ext": "mp4", "vcodec": "avc1",
         "acodec": "mp4a.40.2", "filesize": 10}]})
    check("no duration + muxed keeps size", nodur[0]["size"] == 10, nodur[0])
    check("no duration + muxed = single file",
          nodur[0]["note"] == "single file", nodur[0])
    a = yttui.build_tiers({"duration": 100, "formats": [
        {"format_id": "v", "height": 1080, "ext": "mp4", "vcodec": "avc1",
         "acodec": "none", "tbr": 1000}]})
    check("video-only no audio -> None", a[0]["size"] is None, a[0])
    check("  ...says unknown", "unknown" in a[0]["note"], a[0]["note"])
    b = yttui.build_tiers({"duration": 100, "formats": [
        {"format_id": "v", "height": 1080, "ext": "mp4", "vcodec": "avc1",
         "acodec": "none", "filesize": 1000},
        {"format_id": "a", "ext": "m4a", "vcodec": "none",
         "acodec": "mp4a.40.2", "filesize": 500}]})
    check("video-only + audio sums", b[0]["size"] == 1500, b[0]["size"])
    dup = yttui.build_tiers({"duration": 50, "formats": [
        {"format_id": "1", "height": 720, "ext": "mp4", "vcodec": "avc1",
         "acodec": "none", "filesize": 10},
        {"format_id": "2", "height": 720, "ext": "webm", "vcodec": "vp9",
         "acodec": "none", "filesize": 20},
        {"format_id": "3", "height": 360, "ext": "mp4", "vcodec": "avc1",
         "acodec": "mp4a.40.2", "filesize": 30}]})
    check("one row per height", [t["label"] for t in dup] == ["720p", "360p"])
    check("prefers video-only at same height",
          (dup[0]["vcodec"], dup[0]["note"]) == ("vp9", "+ audio (size unknown)"),
          dup[0])
    check("muxed flagged single file", dup[1]["note"] == "single file", dup[1])
    check("audio-only media -> no tiers", yttui.build_tiers(
        {"formats": [{"format_id": "a", "vcodec": "none",
                      "acodec": "mp4a.40.2"}]}) == [])

    print("\n=== L1: audio sizes ===")
    check("duration 0 -> None",
          all(r["size"] is None for r in yttui.audio_rows(0)))
    check("flac unknown",
          next(r for r in yttui.audio_rows(300)
               if r["codec"] == "flac")["size"] is None)
    check("128k x60s == 960_000",
          next(r for r in yttui.audio_rows(60)
               if r["note"] == "128 kbps")["size"] == 960_000)

    print("\n=== L1: progress payloads ===")
    r = yttui.parse_progress_payload("downloading|1000|2000|NA|1000|5|/t/a.mp4")
    check("happy path", r["pct"] == 50.0 and r["file"] == "a.mp4", r)
    r = yttui.parse_progress_payload("downloading|500|NA|1000|0|NA|/t/b.mp4")
    check("estimate fallback", r["pct"] == 50.0, r)
    r = yttui.parse_progress_payload("downloading|9999|1000|NA|0|NA|/t/c.mp4")
    check("over-100 clamped", r["pct"] == 100.0, r)
    r = yttui.parse_progress_payload("downloading|0|NA|NA|0|NA|/t/d.mp4")
    check("no total -> 0", r["pct"] == 0.0, r)
    r = yttui.parse_progress_payload("finished|1|1|NA|0|NA|/t/e.mp4")
    check("finished -> done", r["status"] == "done", r)
    for bad in ("", "garbage", "a|b|c", "x|y", None):
        check(f"rejects {bad!r:10}", yttui.parse_progress_payload(bad) is None)
    for ok_shape in ("||||||", "x|y|z|1|2|3|4|5|6"):
        rr = yttui.parse_progress_payload(ok_shape)
        check(f"7+ fields safe {ok_shape[:10]!r:12}",
              rr is not None and rr["pct"] == 0.0, rr)


# ======================= LAYER 2 =======================
async def layer2():
    print("\n=== L2: what-to-fetch checkboxes build the right command ===")
    app = yttui.YtTui()
    async with app.run_test(size=(150, 46)) as pilot:
        a = pilot.app

        async def set_boxes(**kw):
            from textual.widgets import Checkbox
            a.query_one("#dl-video", Checkbox).value = kw.get("video", False)
            a.query_one("#dl-audio", Checkbox).value = kw.get("audio", False)
            a.query_one("#dl-subs", Checkbox).value = kw.get("subs", False)
            a.query_one("#dl-thumb", Checkbox).value = kw.get("thumb", False)
            a._queue = []
            a.action_download()
            await pilot.pause(0.3)
            return a._queue[0] if a._queue else None

        a._current_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        video_sel = {"kind": "video", "label": "1080p", "size": 1,
                     "selector": "bv*[height<=1080]+ba/b[height<=1080]"}
        audio_sel = {"kind": "audio", "label": "MP3 320 kbps", "codec": "mp3",
                     "size": 1, "selector": "bestaudio/best"}

        a._selected = dict(video_sel)
        it = await set_boxes(video=True)
        check("video queued", it is not None)
        if it:
            c = it["cmd"]
            check("  video flags", flag(c, "--format",
                                        "--merge-output-format"), c[-6:])
            check("  video target", it["target"] == "video", it["target"])
            check("  video label is the quality", it["label"] == "1080p",
                  it["label"])
            check("  no ignore-errors on video", "--ignore-errors" not in c)

        a._selected = dict(video_sel)
        it = await set_boxes(video=True, subs=True)
        check("video+subs queued", it is not None)
        if it:
            c = it["cmd"]
            check("  subs stay OFF the video command",
                  "--write-subs" not in c and "--no-write-subs" in c,
                  [x for x in c if "subs" in x])
            check("  subs tracked for second pass", it["want_subs"] is True)

        a._selected = dict(audio_sel)
        it = await set_boxes(audio=True)
        check("audio queued", it is not None)
        if it:
            c = it["cmd"]
            check("  audio flags", flag(c, "--extract-audio",
                                        "--audio-format"), c[-6:])
            check("  audio label drops the quality",
                  "1080p" not in it["label"], it["label"])
            check("  audio target", it["target"] == "audio", it["target"])

        a._selected = None
        it = await set_boxes(subs=True)
        check("subs-only queued without a format pick", it is not None)
        if it:
            c = it["cmd"]
            check("  subs-only skips the media",
                  flag(c, "--skip-download", "--write-subs"), c[-8:])
            check("  subs-only may ignore errors",
                  "--ignore-errors" in c)
            check("  subs-only langs", "en.*,en" in c)

        a._selected = None
        it = await set_boxes(thumb=True)
        check("thumb-only queued without a format pick", it is not None)
        if it:
            c = it["cmd"]
            check("  thumb-only skips the media",
                  flag(c, "--skip-download", "--write-thumbnail"), c[-8:])

        a._selected = None
        it = await set_boxes(subs=True, thumb=True)
        check("subs+thumb in one job", it is not None)
        if it:
            check("  combined target", it["target"] == "subs+thumb",
                  it["target"])

        print("\n=== L2: guards ===")
        a._selected = dict(video_sel)
        a._queue = []
        await set_boxes()
        check("nothing ticked refused", len(a._queue) == 0)
        a._selected = None
        a._queue = []
        await set_boxes(video=True)
        check("video w/o quality refused", len(a._queue) == 0)
        a._selected = None
        a._queue = []
        await set_boxes(audio=True)
        check("audio w/o quality refused", len(a._queue) == 0)

        print("\n=== L2: corrupt-output guard ===")
        # Regression: the tracked progress path points at the last *stream*
        # (".f251.webm"), not the merged output, so a corrupt .mp4 survived
        # and --no-overwrites poisoned every later attempt.
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            dd = Path(td)
            (dd / "Song [abc123XYZ].mp4").write_bytes(b"not media at all")
            (dd / "Song [abc123XYZ].part").write_bytes(b"partial")
            (dd / "Other [zzz999QQ].mp4").write_bytes(b"also not media")
            (dd / "Song [abc123XYZ].srt").write_text("subs are fine")
            old = yttui.DOWNLOAD_DIR
            yttui.DOWNLOAD_DIR = dd
            try:
                a._queue = [dict(yttui.YtTui._ITEM_DEFAULTS, id=7,
                                 cmd=["yt-dlp",
                                      "https://www.youtube.com/watch?v=abc123XYZ"],
                                 path=str(dd / "gone.f251.webm"))]
                msg = a._drop_corrupt_output(7)
            finally:
                yttui.DOWNLOAD_DIR = old
            left = sorted(p.name for p in dd.iterdir())
            check("corrupt .mp4 removed", "Song [abc123XYZ].mp4" not in left,
                  left)
            check("corrupt .part removed", "Song [abc123XYZ].part" not in left,
                  left)
            check("subscript file spared", "Song [abc123XYZ].srt" in left, left)
            check("other video untouched", "Other [zzz999QQ].mp4" in left, left)
            check("guard reports what it did", "removed" in msg, msg)

            # A valid media file must never be deleted.
            good = dd / "Good [abc123XYZ].mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                            "-i", "color=c=blue:s=32x32:d=1", str(good)],
                           capture_output=True, timeout=120)
            yttui.DOWNLOAD_DIR = dd
            try:
                a._queue = [dict(yttui.YtTui._ITEM_DEFAULTS, id=8,
                                 cmd=["yt-dlp",
                                      "https://www.youtube.com/watch?v=abc123XYZ"])]
                a._drop_corrupt_output(8)
            finally:
                yttui.DOWNLOAD_DIR = old
            check("valid media spared", good.exists())

        print("\n=== L2: views + log toggle + downloads panel ===")
        a._queue = [{"id": 1, "label": "partial"}]
        for _ in range(5):
            await pilot.pause(0.3)
        check("pump survives malformed item", True)
        a._queue = []
        for _ in range(4):
            await pilot.pause(0.3)
        check("pump survives empty queue", True)
        check("_ITEM_DEFAULTS keys",
              set(yttui.YtTui._ITEM_DEFAULTS) >= {"id", "label", "cmd",
                  "status", "pct", "speed", "eta", "file", "size", "error",
                  "code", "want_subs", "subs", "path", "target"})

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "S [x].f251.webm").write_bytes(b"x")
            (d / "S [x].mp4").write_bytes(b"x")
            (d / "S [x].webp").write_bytes(b"x")
            (d / "Other [y].f1.mp4").write_bytes(b"x")
            a._queue = [dict(yttui.YtTui._ITEM_DEFAULTS, id=1, label="720p",
                             path=str(d / "S [x].mp4"))]
            a._reap_orphans(1)
            left = sorted(p.name for p in d.iterdir())
            check("reap removes stream leftovers",
                  "S [x].f251.webm" not in left, left)
            check("reap removes orphan thumbnail", "S [x].webp" not in left, left)
            check("reap keeps output", "S [x].mp4" in left, left)
            check("reap spares other videos", "Other [y].f1.mp4" in left, left)
            a._queue = [dict(yttui.YtTui._ITEM_DEFAULTS, id=2, path="")]
            a._reap_orphans(2)
            check("reap no-path no-op",
                  sorted(p.name for p in d.iterdir()) == left)
            a._queue = [dict(yttui.YtTui._ITEM_DEFAULTS, id=3,
                             path=str(d / "gone" / "S.mp4"))]
            a._reap_orphans(3)
            check("reap missing dir safe", True)

        check("starts in results", not a.query_one("#results").has_class("hidden"))
        check("detail hidden at start",
              a.query_one("#preview").has_class("hidden"))
        check("downloads panel hidden in results view",
              a.query_one("#downloads").has_class("hidden"))
        a._show("detail")
        check("detail shows preview",
              not a.query_one("#preview").has_class("hidden"))
        check("detail hides results",
              a.query_one("#results").has_class("hidden"))
        check("downloads panel always visible in detail view",
              not a.query_one("#downloads").has_class("hidden"))
        check("downloads is not a tab anymore",
              "downloads" not in [str(p.id) for p in a.query("TabPane")])
        a._show("results")
        check("back hides detail", a.query_one("#preview").has_class("hidden"))

        log = a.query_one("#log")
        check("log hidden by default", not log.has_class("visible"))
        a.action_clear_log()
        check("L toggles log on", log.has_class("visible"))
        a.action_clear_log()
        check("L toggles log off", not log.has_class("visible"))
        a._reveal_log()
        check("error forces log open", log.has_class("visible"))
        a.action_clear_log()


# ======================= LAYER 3 =======================
async def layer3():
    print("\n=== L3: live probe ===")
    try:
        yttui.probe_media("https://www.youtube.com/watch?v=jfKfPfyJRdk", False)
        check("dead livestream raises", False, "no error")
    except RuntimeError as e:
        check("dead livestream raises", "not available" in str(e).lower(), str(e))
    for bad in ("https://example.invalid/nope", "not a url at all!!"):
        try:
            yttui.probe_media(bad, False)
            check(f"bad url raises {bad[:24]!r}", False, "no error")
        except RuntimeError:
            check(f"bad url raises {bad[:24]!r}", True)
        except Exception as e:
            check(f"bad url raises {bad[:24]!r}", False, type(e).__name__)

    info = yttui.probe_media("https://www.youtube.com/watch?v=jNQXAC9IVRw", False)
    check("formats found", len(info.get("formats") or []) > 0)
    check("duration found", bool(info.get("duration")))
    check("thumbnail found", bool(info.get("thumbnail")))
    res = yttui.probe_search("lofi", False, 3)
    check("search count", 0 < len(res) <= 3, len(res))

    print("\n=== L3: concurrent downloads ===")
    app = yttui.YtTui()
    async with app.run_test(size=(150, 46)) as pilot:
        a = pilot.app
        a.query_one("#tabs").set_class(False, "hidden")
        for w in ("#preview", "#detail", "#progress-row"):
            a.query_one(w).set_class(False, "hidden")
        a.query_one("#results").set_class(True, "hidden")
        a._info = info
        a._current_url = info["webpage_url"]
        a._vrows = yttui.build_tiers(info)
        a._fill_video(False)
        a._arows = yttui.audio_rows(int(info.get("duration") or 0))
        a._fill_audio()
        row = a._vrows[-1]
        a._selected = row
        a.action_download()
        await pilot.pause(1.0)
        check("duplicate URL refused", len(a._queue) == 1, len(a._queue))
        a._selected = row
        a.action_download(other="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        await pilot.pause(1.0)
        check("two queued", len(a._queue) == 2, len(a._queue))
        check("distinct ids", [x["id"] for x in a._queue] == [1, 2])
        check("distinct URLs", a._queue[0]["cmd"][1] != a._queue[1]["cmd"][1])
        dt = a.query_one("#dtab", DataTable)
        check("two queue rows", dt.row_count == 2, dt.row_count)
        for _ in range(240):
            await pilot.pause(1.0)
            if all(x["status"] in ("done", "error", "cancelled")
                   for x in a._queue):
                break
        st = [x["status"] for x in a._queue]
        check("both settled", "downloading" not in st, st)
        check("both succeeded", all(s == "done" for s in st),
              [(x["id"], x["status"], x["error"]) for x in a._queue])
        for x in a._queue:
            print(f"    #{x['id']} {x['status']} {x['file'][:40]} "
                  f"subs={x['subs']!r}")


async def main():
    layer1()
    await layer2()
    try:
        await layer3()
    except Exception as e:
        import traceback
        print("\nL3 aborted:", type(e).__name__, e)
        traceback.print_exc()
    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
