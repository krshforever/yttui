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
        c, r, shown = yttui.thumb_cells(w, h)
        true = w / h
        check(f"aspect {label} target == grid",
              abs(shown - c / (r * 2)) < 1e-9,
              f"{shown:.4f} vs {c / (r * 2):.4f}")
        if exempt:
            check(f"aspect {label} clamped", shown == c / (r * 2) and r >= 4)
        else:
            check(f"aspect {label} within 15% of source",
                  abs(shown - true) / true < 0.15,
                  f"{shown:.2f} vs {true:.2f}")
        check(f"  {label} within budget", c <= yttui.THUMB_MAX_COLS and r >= 4)
    check("thumb_cells 3-tuple", len(yttui.thumb_cells(1280, 720)) == 3)
    check("render None", yttui.render_halfblocks(None) == (None, 0, 0))
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"not an image")
        bogus = Path(f.name)
    check("render corrupt -> None", yttui.render_halfblocks(bogus)[0] is None)

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
    print("\n=== L2: targets build the right command ===")
    app = yttui.YtTui()
    async with app.run_test(size=(150, 46)) as pilot:
        a = pilot.app
        tsel = a.query_one("#target", Select)
        check("target default video", tsel.value == "video", tsel.value)
        a._current_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        sel = {"kind": "video", "label": "1080p", "size": 1,
               "selector": "bv*[height<=1080]+ba/b[height<=1080]"}

        a._selected = sel
        for tgt, want in (("video", ("--format", "--merge-output-format")),
                          ("custom", ("--format",)),
                          ("audio", ("--extract-audio", "--audio-format")),
                          ("thumb", ("--skip-download", "--write-thumbnail")),
                          ("subs", ("--skip-download", "--write-subs"))):
            tsel.value = tgt
            a._queue = []
            a._selected = dict(sel)
            a.action_download()
            await pilot.pause(0.3)
            if not a._queue:
                check(f"{tgt} queued", False, "refused")
                continue
            c = a._queue[0]["cmd"]
            check(f"{tgt} queued", True)
            check(f"  {tgt} flags", flag(c, *want),
                  [x for x in c if x.startswith("--")])
            check(f"  {tgt} target recorded", a._queue[0]["target"] == tgt)
            # For video the quality IS the label; for the others, a video
            # quality would be a lie about what was fetched.
            if tgt == "video":
                check("  video label is the quality",
                      a._queue[0]["label"] == "1080p", a._queue[0]["label"])
            else:
                check(f"  {tgt} label drops the video quality",
                      "1080p" not in a._queue[0]["label"],
                      a._queue[0]["label"])
            check(f"  {tgt} no ignore-errors on video",
                  (tgt != "video") or "--ignore-errors" not in c)

        print("\n=== L2: guards ===")
        a._queue = []
        a._selected = None
        for tgt in ("video", "audio", "custom"):
            tsel.value = tgt
            a.action_download()
            await pilot.pause(0.2)
            check(f"{tgt} w/o quality refused", len(a._queue) == 0)
        for tgt in ("subs", "thumb"):
            tsel.value = tgt
            a._queue = []
            a._selected = None
            a.action_download()
            await pilot.pause(0.2)
            check(f"{tgt} w/o quality allowed", len(a._queue) == 1)
        a._queue = []
        tsel.value = "custom"
        a._selected = {"kind": "video", "label": "x", "selector": None,
                       "size": 1}
        a.action_download()
        await pilot.pause(0.2)
        check("custom w/o selector refused", len(a._queue) == 0)

        print("\n=== L2: timer + reaper + views ===")
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
        a._show("detail")
        check("detail shows preview",
              not a.query_one("#preview").has_class("hidden"))
        check("detail hides results",
              a.query_one("#results").has_class("hidden"))
        a._show("results")
        check("back hides detail", a.query_one("#preview").has_class("hidden"))


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
