# AGENTS.md

Instruction file for agents working on **yttui** — an interactive terminal
front-end for `yt-dlp` with in-terminal thumbnail rendering.

## Layout

```
yttui.py          the entire app (single module, ~1100 lines)
tests/test_yttui.py   self-contained suite, no pytest, exits non-zero on failure
pyproject.toml    console script: yttui = "yttui:main"
LICENSE           GPL-3.0-or-later
```

There is no package directory, no build step, and no test runner. `yttui.py` is
importable and executable.

### The source/install split (easy to get wrong)

- `yttui.py` (here) is the **source of truth**.
- `~/.local/bin/yttui` is the **installed copy** the user actually runs.
- `tests/test_yttui.py` loads `yttui.py` from the repo root.

After editing `yttui.py`, the installed copy is stale and the user keeps
running old code. Re-install:

```bash
install -m 0755 yttui.py ~/.local/bin/yttui
```

There is a second, older copy of the tests at
`~/.local/share/yttui-tests/test_yttui.py` — it predates this repo and is not
the source of truth.

## Commands

```bash
python3 yttui.py                    # run the app (interactive, blocks)
python3 tests/test_yttui.py         # full suite; L3 needs network
```

Do **not** run `yttui --version` (or any flag) to probe the app. There is no
argument parsing — it launches the full-screen TUI and hangs the shell. Import
the module to inspect it instead:

```bash
python3 -c "import yttui; print(yttui.TARGETS)"
```

## The one architectural rule: shell out, never use the Python API

`yttui` invokes `yt-dlp` as a subprocess. **Never refactor this to
`yt_dlp.YoutubeDL`.** The Python API silently ignores
`~/.config/yt-dlp/config`:

```
$ python3 -c "from yt_dlp import YoutubeDL; print(YoutubeDL({'quiet':True}).params.get('js_runtimes'))"
{'deno': {}}          # config says node
```

It returns `remote_components: set()` and `paths: None` too. Every URL-based
setting in the user's config — JS runtime, challenge solver, output path,
filename template — is silently dropped, and YouTube extraction degrades to
HLS stubs with no error. The CLI honours the same config
(`yt-dlp -v` shows `[debug] JS runtimes: node-26.10.0`).

## yt-dlp config traps

`~/.config/yt-dlp/config` is parsed by yt-dlp, not Python. Verified behaviours:

- **Values with spaces must be quoted.** `--output "%(title)s [%(id)s].%(ext)s"`
  unquoted splits into extra tokens and yt-dlp tries to download a URL named
  `[%(id)s].%(ext)s`.
- **`$HOME` expands in `--paths` but NOT in `--js-runtimes`.** An unresolved
  path there yields `JS runtimes: none` with no warning.
- **YouTube needs both** `--js-runtimes node:<absolute path>` *and*
  `--remote-components ejs:github`. The runtime alone is not enough — challenge
  solving still fails, giving `n challenge solving failed` /
  `Requested format is not available` and silently lower-quality output.

## Download correctness rules

These were each found by a real failure. Keep them.

- **Never pass `--ignore-errors` on the video path.** It downgrades genuine
  video failures to warnings, which produced a file containing only audio that
  the app reported as a success. Subtitles are fetched in a *separate* second
  pass (`_fetch_subs`) where `--ignore-errors` is safe — only a `.srt` is at
  stake.
- **Subtitles must never be on the video command.** YouTube rate-limits subtitle
  requests (HTTP 429) and yt-dlp treats that as fatal, aborting the whole
  download. Hence the two-pass design.
- **Duplicate URLs are refused** in `enqueue()`. Two yt-dlp processes on one
  output name race in `--embed-thumbnail` (one deletes the `.webp` while the
  other is still converting it to `.png`) and kill each other with ENOENT.
- **The reaper matches on the output path reported by yt-dlp**, never on the
  display label (which is just `"1080p"`). It also clears orphaned `.webp`
  thumbnails, but only once the finished video exists.
- **Sizes:** yt-dlp's `tbr`/`vbr`/`abr` are in **kbps**. `bytes = kbps*1000*sec/8`.
  Missing the `*1000` made every tier report ~4 MB regardless of resolution.

## Thumbnail geometry

Terminal cells are ≈2× taller than wide, and a `▀` half-block carries two pixels
stacked. So a `cols × rows` cell block is a `cols × rows*2` grid of **square**
pixels:

```
cols / (rows * 2) == width / height
```

- The crop target **must equal `cols/(rows*2)` exactly**. Returning the source
  aspect instead leaves a mismatch, and since the final `resize()` ignores
  aspect, that silently stretches every thumbnail. This bug shipped once
  *inside its own fix*.
- Beyond ~4:1 the cell budget cannot express the ratio (the width cap forces
  fewer than 4 rows). Clamp the target and centre-crop — do not stretch.
- Ceiling is roughly 58×32 **pixels**. To do better you need Kitty/Sixel, which
  the user's VTE terminal does not support.

## Textual 8 quirks

All of these caused real crashes or silently-dead UI:

- `@work` on a **sync** function requires `thread=True`.
- A **focused widget's binding wins over an App-level binding.** `DataTable`
  binds `enter` itself, so `BINDINGS = [("enter", "open", ...)]` never fires.
  Handle `DataTable.RowSelected` instead.
- `call_from_thread` raises if called **on** the app thread. Message handlers
  (`on_button_pressed`, `RowHighlighted`) are already there — call directly.
- `Log.write_line` does **not** parse Rich markup; it strips control codes.
  Use a leading glyph, never `[red]...[/]` (and filenames contain `[id]`,
  which markup would try to parse).
- `Static` stores content in the name-mangled slot `_Static__content`;
  `styles.width` returns a `Scalar` (use `.value`).
- Worker threads must not touch widgets. Workers write to a plain dict; a
  `set_interval` timer on the UI thread copies it in. `enqueue()` is app-thread;
  `download_worker()` is a worker thread — do not confuse the two.

`DataTable` also needs explicit key checks: a bare `except: add_row(...)` on a
failed `update_cell` re-inserts an existing key and raises `DuplicateKey`
during ordinary progress updates.

## Testing

- `tests/test_yttui.py` is three layers: pure functions, app state inside
  `run_test()`, and live network. It prints `PASS`/`FAIL` per assertion and
  exits non-zero if any fail.
- **All app interaction must happen inside `async with app.run_test()`**.
  Calling widget methods outside it raises `ScreenStackError`. Several fixture
  bugs came from this.
- **Do not put tests in `/tmp`.** `/tmp/opencode` was wiped mid-session and took
  the suite with it.
- When writing assertions, check the code is right before assuming it is wrong.
  Several "failures" were bad fixtures (`||||||` parses fine — 7 empty fields
  is a valid shape; `-5` sizes; a video-only format with no audio track has
  `size=None` by design).

## Environment

- Ubuntu 24.04 / Zorin OS 18.1, GNOME, Wayland. Python 3.12, textual 8.2.8,
  pillow 10.2.0, ffmpeg 7.0.2-static, yt-dlp 2026.08.19.
- **`yt-dlp` here is a pip install, not a standalone binary**, so `yt-dlp -U`
  always fails. Update with
  `python3 -m pip install --user --break-system-packages -U yt-dlp`.
  `--break-system-packages` is required because Ubuntu 24.04 enforces PEP 668;
  with `--user` it can only write to `~/.local`.
- `~/Downloads` is the download dir. Filenames include `[<id>]` to avoid
  collisions.

## Known unverified — do not assume these work

- **The entire visual layer.** Wayland screenshot capture is blocked on this
  machine, so CSS (borders, panel backgrounds, table header/cursor highlighting)
  is written blind and has never been seen. Only the user can judge it.
- **The original "download suddenly exits" bug was never reproduced.** A
  shared-worker-group collision was the suspected cause and is fixed, but that
  was inference, not observation. If it recurs, the Downloads tab's Detail
  column carries the real yt-dlp error text.
- **No screenshot is committed** and `README.md` deliberately does not link one.
  Terminal capture is blocked here; if the user supplies an image, drop it at
  `docs/screenshot.png` and re-add the `![tui](docs/screenshot.png)` line.
