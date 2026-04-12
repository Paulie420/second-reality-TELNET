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
   bucket, saving the ANSI output as `frames-30fps/<W>/frame-<n:05x>.ans`
   by default (or `frames-20fps/<W>/...` if you pass `--output
   frames-20fps --fps 20`).
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

On Arch / Manjaro / Omarchy:

```bash
sudo pacman -S ffmpeg chafa
```

On Fedora:

```bash
sudo dnf install ffmpeg chafa
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
cat frames-30fps/80/frame-00100.ans
cat frames-30fps/80/frame-01000.ans
```

Each should draw a recognizable frame of the source video in your terminal.
If they look right, proceed to the full bake.

## Full bake

```bash
python tools/bake_frames.py second_reality_short.webm --workers 5
```

Defaults match the project's design:

- 30 fps
- widths `40, 60, 80, 100, 120, 140, 160, 180, 200`
- chafa symbols `block+border+space` (matches 1984.ws visual style)
- truecolor (`chafa -c full`)
- cell aspect ratio `2.0` (tall-cell assumption — typical monospace fonts)
- workers = CPU count minus one (the `--workers 5` above caps at ~62% of
  an 8-core laptop so you can keep using the machine while it bakes)

## 30fps or 20fps?

The server can play back at either; the bake fps chooses which. 20fps
frames are ~28% smaller per second on the wire and much more playable on
cellular / hotel-wifi / constrained-uplink deployments. 30fps is
noticeably smoother on motion-heavy demo sections.

Recommendation for most operators: **bake both** and let the admin flip
between them on the live server with `tools/switch_fps.sh` (see
[`docs/deploy.md`](deploy.md#switching-fps-on-a-live-server)). Disk cost
is ~23 GB total uncompressed, but `switch_fps.sh` compresses the
inactive set down to ~3 GB automatically after each flip.

### Bake 30fps (default)

```bash
python tools/bake_frames.py second_reality_short.webm \
    --output frames --fps 30 --workers 5
```

Output: `frames/40/`, `frames/60/`, …, `frames/200/`. ~15k frames per
bucket. ~14 GB total.

### Bake 20fps

```bash
python tools/bake_frames.py second_reality_short.webm \
    --output frames-20fps --fps 20 --workers 5
```

Output: `frames-20fps/40/`, `frames-20fps/60/`, …, `frames-20fps/200/`.
~10k frames per bucket. ~9.4 GB total.

When running the server against a 20fps bake, pass the scaled
end-trim and skip flags so the wall-clock edits match the 30fps
default:

```bash
python -m srtelnet.server \
    --frames frames-20fps \
    --fps 20 \
    --max-frames 10163 \
    --skip 897:1117
```

(Those numbers are `DEFAULT_MAX_FRAMES × 20/30` and the skip range
scaled to 20fps; `tools/switch_fps.sh` bakes them into the systemd unit
automatically when you flip.)

### Bake both at once

Both bakes reuse the same extracted PNG intermediate, so back-to-back
bakes are almost free after the first:

```bash
# First: extract + bake 30fps, keep the PNG tmp dir
python tools/bake_frames.py second_reality_short.webm \
    --output frames --fps 30 --workers 5 --keep-tmp --tmp-dir /tmp/sr-png-30

# Second: different fps means we need a fresh extract (20fps vs 30fps
# are different PNG sequences). This one also keeps its tmp.
python tools/bake_frames.py second_reality_short.webm \
    --output frames-20fps --fps 20 --workers 5 --keep-tmp --tmp-dir /tmp/sr-png-20
```

After both complete you can drop the tmp dirs:

```bash
rm -rf /tmp/sr-png-30 /tmp/sr-png-20
```

## Color mode

Default is chafa truecolor (`-c full`). A `--colors` flag is available
to experiment with 256-color (`--colors 256`) or other chafa palettes,
but 256-color visibly bands the plasma/fire gradients that make up much
of *Second Reality* — not recommended for this specific source. Stick
with truecolor unless you're rendering something with a flatter palette.

Expected output size on disk: roughly **3–5 GB** for the 9-bucket bake of a
~9-minute 30 fps source. That's why `frames-30fps/` / `frames-20fps/`
are gitignored and the baked directories get shipped to the LXC via
scp/rsync rather than git.

## Useful flags

| Flag | What it does |
|---|---|
| `--single-width N` | Bake only the width-`N` bucket. Sanity runs, resolution tests, quick experiments. |
| `--fps N` | Override frame rate. Lower for disk savings, higher for smoother motion. |
| `--widths 40,60,80` | Explicit custom bucket list. |
| `--symbols SYMBOLS` | Override `chafa --symbols`. Try `all` for max fidelity or `block` for the cleanest-looking "chunky pixel" aesthetic. |
| `--cell-aspect F` | If your viewers' terminal cells aren't close to 2:1, set this to their actual ratio. Too low → stretched wide; too high → stretched tall. |
| `--output DIR` | Where to write `<DIR>/<W>/frame-*.ans`. Default `./frames-30fps`. Use `./frames-20fps` with `--fps 20`. |
| `--workers N` | Number of parallel chafa processes. Default `CPU count − 1`. |
| `--keep-tmp` | Keep the intermediate `_tmp_png/` directory around after baking, useful if you want to re-run chafa with different parameters without re-decoding the video. |
| `--tmp-dir DIR` | Put the intermediate PNGs somewhere specific (e.g. on a tmpfs for speed). |

## Resuming an interrupted bake

The bake script **skips any `.ans` file that already exists and is non-empty**.
If you interrupt a run (Ctrl-C, power loss, kernel OOM), just run the same
command again; it picks up where it left off. The extracted PNGs in
`_tmp_png/` are also reused automatically as long as the directory isn't
deleted.

To force a clean rebuild, delete `frames-30fps/<W>/` (or
`frames-20fps/<W>/`) for the buckets you want to redo, and optionally
delete `<output>/_tmp_png/` to force a fresh ffmpeg extract.

## Shipping frames to the LXC

The project's deployment story keeps frames off GitHub. Once baking is
complete on your workstation:

```bash
# on your workstation, from the project root.
# Adjust --output paths below to match whichever fps you baked.
tar -C frames-30fps -cf - . | zstd -T4 -6 > /tmp/sr-frames-30fps.tar.zst
scp /tmp/sr-frames-30fps.tar.zst <you>@<lxc-ip>:/tmp/
```

Then on the LXC:

```bash
cd ~/second-reality-TELNET
mkdir -p frames-30fps
zstd -dc /tmp/sr-frames-30fps.tar.zst | tar -C frames-30fps -xf -
rm /tmp/sr-frames-30fps.tar.zst
```

Repeat for `frames-20fps` if you baked both. After shipping, the
`tools/switch_fps.sh` helper (run on the LXC) wires the systemd unit
to the right frame set and fps flags — see
[`deploy.md`](deploy.md#switching-fps-on-a-live-server).
