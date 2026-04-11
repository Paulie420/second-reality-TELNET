# Baking frames

The telnet server reads pre-rendered ANSI frames from disk. Those frames are
produced once, on a machine that has `ffmpeg` and `chafa` installed (typically
your development machine, **not** the Proxmox LXC — the LXC stays slim and
never needs either tool). The script that does it is
[`tools/bake_frames.py`](../tools/bake_frames.py).

## What the bake does

For a given source video:

1. Uses `ffprobe` to learn the source resolution and duration.
2. Uses `ffmpeg` to extract every frame at the target frame rate (default
   `30 fps`) as PNG files in a temporary directory.
3. For each target terminal width in the bucket list (default `40, 60, 80,
   100, 120, 140, 160, 180, 200` cells), computes the matching height that
   preserves the source aspect ratio on a terminal with cells that are twice
   as tall as they are wide.
4. Fans out across CPU cores and runs `chafa` on every PNG for every width
   bucket, saving the ANSI output as `frames/<W>/frame-<n:05x>.ans`.
5. Deletes the temporary PNG directory (unless `--keep-tmp` is passed).

Each `.ans` file is a self-contained block of truecolor ANSI escape sequences
that, when written to a 24-bit color terminal, draws that one frame of the
video. The telnet server reads the file verbatim and writes it to the socket,
adding cursor-positioning escapes around each line for centering.

## Prerequisites

On macOS:

```bash
brew install ffmpeg chafa
```

On Debian / Ubuntu:

```bash
sudo apt install ffmpeg chafa
```

Then from the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

(The bake script itself has no third-party Python dependencies — only the
stdlib, `ffmpeg`, and `chafa` — but the project's venv is a convenient place
to run it from.)

## Sanity-check bake first

Before committing to a full bake (which produces hundreds of thousands of
files and takes a while), do a **single-width** run to confirm the pipeline
works and the visual output is correct:

```bash
python tools/bake_frames.py second_reality_short.webm --single-width 80
```

This runs the full ffmpeg extract (one-time cost) and bakes only the 80-cell
width bucket. When it's done you can eyeball a few frames:

```bash
cat frames/80/frame-00100.ans
cat frames/80/frame-01000.ans
```

Each should draw a recognizable frame of the source video in your terminal.
If they look right, proceed to the full bake.

## Full bake

```bash
python tools/bake_frames.py second_reality_short.webm
```

No flags needed — defaults match the project's design:

- 30 fps
- widths `40, 60, 80, 100, 120, 140, 160, 180, 200`
- chafa symbols `block+border+space` (matches 1984.ws visual style)
- truecolor (`chafa -c full`)
- cell aspect ratio `2.0` (tall-cell assumption — typical monospace fonts)
- workers = CPU count minus one

Expected output size on disk: roughly **3–5 GB** for the 9-bucket bake of a
~9-minute 30 fps source. That's why `frames/` is gitignored and the baked
directory gets shipped to the LXC via scp/rsync rather than git.

## Useful flags

| Flag | What it does |
|---|---|
| `--single-width N` | Bake only the width-`N` bucket. Sanity runs, resolution tests, quick experiments. |
| `--fps N` | Override frame rate. Lower for disk savings, higher for smoother motion. |
| `--widths 40,60,80` | Explicit custom bucket list. |
| `--symbols SYMBOLS` | Override `chafa --symbols`. Try `all` for max fidelity or `block` for the cleanest-looking "chunky pixel" aesthetic. |
| `--cell-aspect F` | If your viewers' terminal cells aren't close to 2:1, set this to their actual ratio. Too low → stretched wide; too high → stretched tall. |
| `--output DIR` | Where to write `<DIR>/<W>/frame-*.ans`. Default `./frames`. |
| `--workers N` | Number of parallel chafa processes. Default `CPU count − 1`. |
| `--keep-tmp` | Keep the intermediate `_tmp_png/` directory around after baking, useful if you want to re-run chafa with different parameters without re-decoding the video. |
| `--tmp-dir DIR` | Put the intermediate PNGs somewhere specific (e.g. on a tmpfs for speed). |

## Resuming an interrupted bake

The bake script **skips any `.ans` file that already exists and is non-empty**.
If you interrupt a run (Ctrl-C, power loss, kernel OOM), just run the same
command again; it picks up where it left off. The extracted PNGs in
`_tmp_png/` are also reused automatically as long as the directory isn't
deleted.

To force a clean rebuild, delete `frames/<W>/` for the buckets you want to
redo, and optionally delete `frames/_tmp_png/` to force a fresh ffmpeg
extract.

## Shipping frames to the LXC

The project's deployment story keeps frames off GitHub. Once baking is
complete on your Mac:

```bash
# on your Mac, from the project root:
tar -C frames -cf - . | zstd -T0 -6 > /tmp/sr-frames.tar.zst
scp /tmp/sr-frames.tar.zst paulie420@<lxc-ip>:/tmp/
```

Then on the LXC:

```bash
mkdir -p ~/second-reality-TELNET/frames
cd ~/second-reality-TELNET/frames
zstd -d < /tmp/sr-frames.tar.zst | tar -xf -
```

The server reads from `~/second-reality-TELNET/frames/<W>/...` — no
configuration needed.
