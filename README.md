# yttui

An interactive terminal front-end for [`yt-dlp`](https://github.com/yt-dlp/yt-dlp),
with thumbnails rendered **inside** the terminal.

## Why this exists

Most yt-dlp front-ends are text tables. `yttui` draws the actual thumbnail as
`▀` half-blocks with truecolor — two pixels per character cell — so you can see
what you are downloading before you commit to a quality tier.

## Features

- **Five targets**: video + audio, audio only, subtitles only, thumbnail only,
  or a custom `format_id`.
- **Curated quality tiers** — one row per distinct resolution, showing the real
  combined download size, instead of forty raw `format_id`s.
- **Real size estimates**, cross-checked against yt-dlp's own `filesize`
  (accurate to ~0.15%).
- **Search or paste a URL.** A keyword returns 10 results; a pasted link goes
  straight to the download view.
- **Tracked download queue** — concurrent downloads, per-item progress, speed,
  ETA, and the real yt-dlp error text on failure.
- **Automatic cleanup** of yt-dlp's leftover per-stream and thumbnail files.
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
| `T` / `A` / `W` | Video / Audio / Downloads tab |
| `B` | Back to results |
| `C` | Cancel the highlighted download |
| `L` | Clear log |
| `Q` | Quit |

## Credits

Built by [@krshforever](https://github.com/krshforever).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
