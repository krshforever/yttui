# yttui

An interactive terminal front-end for [`yt-dlp`](https://github.com/yt-dlp/yt-dlp),
with thumbnails rendered **inside** the terminal.

## Why this exists

Most yt-dlp front-ends are text tables. `yttui` draws the actual thumbnail as
`▀` half-blocks with truecolor — two pixels per character cell — so you can see
what you are downloading before you commit to a quality tier.

## Features

- **Thumbnails rendered in the terminal** with braille (U+2800) — 140×80 colour
  samples, 4× what a `▀` half-block grid can carry.
- **Choose what to fetch**: tick any combination of video, audio, subtitles and
  thumbnails. They combine — "video + subtitles" is one job.
- **Curated quality tiers** — one row per distinct resolution, showing the real
  combined download size, instead of forty raw `format_id`s.
- **Real size estimates**, cross-checked against yt-dlp's own `filesize`
  (accurate to ~0.15%).
- **Search or paste a URL.** A keyword returns 10 results; a pasted link goes
  straight to the download view.
- **Tracked download queue** in a permanent panel below the quality table —
  concurrent downloads, per-item progress, speed, ETA, and the real yt-dlp error
  text on failure.
- **Self-healing cleanup**: removes yt-dlp's leftover per-stream and thumbnail
  files, and deletes truncated output that would otherwise block every retry.
- **Subtitles fetched separately**, so a YouTube rate-limit on subtitle requests
  can never cost you the video.

## Requirements

- `yt-dlp` on `PATH`
- `ffmpeg` on `PATH` (needed to merge video + audio)
- Python ≥ 3.11

```bash
# Debian/Ubuntu
sudo apt install ffmpeg
python3 -m pip install --user yt-dlp     # or your distro's package
```

## Install

```bash
pipx install git+https://github.com/krshforever/yttui
```

Then run:

```bash
yttui
```

## Configure

`yttui` shells out to the `yt-dlp` CLI, so it uses your existing
`~/.config/yt-dlp/config`. Anything set there applies to `yttui` too.

If YouTube downloads fail or silently degrade to low quality, you probably need
these two lines in that file:

```ini
--js-runtimes node:/usr/bin/node
--remote-components ejs:github
```

## Keys

| Key | Action |
| --- | --- |
| `F` | Fetch / search |
| `Enter` | Open the highlighted result, or download the highlighted format |
| `D` | Download |
| `T` / `A` | Video / Audio quality tab |
| `W` | Focus the downloads panel |
| `B` | Back to results |
| `C` | Cancel the highlighted download |
| `L` | Toggle the log (opens by itself on any failure) |
| `Q` | Quit |

## Credits

Built by [@krshforever](https://github.com/krshforever).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
