#!/usr/bin/env python3
"""
bake_frames.py — turn a video into directories of pre-rendered ANSI frames,
one directory per terminal-width bucket.

Architectural descendant of jquast's network23/pre-render.py (public domain),
but uses the `chafa` and `ffmpeg` command-line tools instead of the libchafa
Python bindings so we have fewer build-time dependencies.

Pipeline:

    video ──ffmpeg──> PNG frames at target fps (one-shot extract to tmp dir)
                │
                └── for each width W in the bucket list:
                        parallel pool of chafa workers
                        each worker: one PNG → one .ans file at W x H(W)
                        → frames/<W>/frame-<nnnnn>.ans

The output format is plain ANSI text per frame: whatever `chafa` prints to
stdout for that image at that cell grid. The telnet server later just reads
the file verbatim and writes it to the socket, with per-line cursor moves
to handle centering.

Example (sanity-check bake at width=80 only):

    python tools/bake_frames.py second_reality_short.webm --single-width 80

Full bake (all buckets, default set):

    python tools/bake_frames.py second_reality_short.webm

Requires: ffmpeg, ffprobe, chafa on PATH.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_WIDTHS = (40, 60, 80, 100, 120, 140, 160, 180, 200)
DEFAULT_FPS = 30
DEFAULT_CELL_ASPECT = 2.0  # terminal cells are ~2x taller than wide
DEFAULT_SYMBOLS = "block+border+space"  # matches 1984.ws visual style
DEFAULT_COLORS = "full"  # chafa -c value; "full" = truecolor, "256" = 8-bit palette
# Accepted chafa -c values — passed through as-is so we don't have to track
# chafa's full matrix ourselves. Common useful ones: full, 256, 240, 16, 8, 2.
VALID_COLORS = ("full", "256", "240", "16", "8", "2", "none", "16/8")


def check_tools() -> None:
    """Verify required CLIs are on PATH, error out cleanly if not."""
    missing = [t for t in ("ffmpeg", "ffprobe", "chafa") if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"[error] required tools not found on PATH: {', '.join(missing)}\n"
            f"        install with: brew install ffmpeg chafa"
        )


def probe(video: Path) -> tuple[int, int, float]:
    """Return (width_px, height_px, duration_seconds) of the first video stream."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    if len(lines) != 3:
        sys.exit(f"[error] unexpected ffprobe output: {lines!r}")
    return int(lines[0]), int(lines[1]), float(lines[2])


def compute_height(width_cells: int, src_w: int, src_h: int, cell_aspect: float) -> int:
    """Given a target width in cells, compute the height in cells that preserves
    the source image's aspect ratio on a terminal whose cells are `cell_aspect`
    times taller than they are wide.

        width_cells cells span src_w pixels horizontally.
        Each cell covers (src_w / width_cells) pixels wide,
        and (src_w / width_cells) * cell_aspect pixels tall.
        To cover src_h pixels vertically:
            H = src_h / ((src_w/W) * cell_aspect)
              = W * src_h / (src_w * cell_aspect)
    """
    return max(1, round(width_cells * src_h / (src_w * cell_aspect)))


def extract_frames(video: Path, fps: int, tmp_dir: Path) -> list[Path]:
    """Explode the video into PNG frames at `fps` fps. Reuses existing PNGs
    in `tmp_dir` if a prior run was interrupted — delete the dir to force a
    clean extract."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(tmp_dir.glob("frame-*.png"))
    if existing:
        print(
            f"[extract] reusing {len(existing)} PNG frames in {tmp_dir} "
            f"(delete the directory to force a clean re-extract)",
            file=sys.stderr,
        )
        return existing

    pattern = tmp_dir / "frame-%06d.png"
    print(f"[extract] ffmpeg → {pattern} at {fps} fps", file=sys.stderr)
    t0 = time.time()
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video),
            "-vf", f"fps={fps}",
            str(pattern),
        ],
        check=True,
    )
    frames = sorted(tmp_dir.glob("frame-*.png"))
    dt = time.time() - t0
    print(f"[extract] {len(frames)} frames in {dt:.1f}s", file=sys.stderr)
    if not frames:
        sys.exit("[error] ffmpeg extracted zero frames")
    return frames


def bake_one(job: tuple[Path, Path, int, int, str, str]) -> str:
    """Single worker: render one PNG through chafa and write the ANSI to disk.
    Returns the output path (or empty string if skipped)."""
    png, out_path, width, height, symbols, colors = job
    if out_path.exists() and out_path.stat().st_size > 0:
        return ""  # already baked, resume-safe
    with open(out_path, "wb") as f:
        subprocess.run(
            [
                "chafa",
                "--format", "symbols",
                "--symbols", symbols,
                "-c", colors,
                "--size", f"{width}x{height}",
                str(png),
            ],
            stdout=f, check=True,
        )
    return str(out_path)


def bake_bucket(
    frames: list[Path],
    width: int,
    height: int,
    out_dir: Path,
    symbols: str,
    colors: str,
    workers: int,
) -> None:
    """Render every extracted PNG into out_dir at the given cell grid, in
    parallel. Output filenames are frame-<n:05x>.ans so sorted glob is stable
    for up to ~1 million frames per bucket."""
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[Path, Path, int, int, str, str]] = []
    for n, png in enumerate(frames):
        out_path = out_dir / f"frame-{n:05x}.ans"
        jobs.append((png, out_path, width, height, symbols, colors))

    print(
        f"[bake {width}x{height}] {len(jobs)} frames → {out_dir} "
        f"({workers} workers)",
        file=sys.stderr,
    )
    t0 = time.time()
    done = 0
    with mp.Pool(workers) as pool:
        for _ in pool.imap_unordered(bake_one, jobs, chunksize=8):
            done += 1
            if done % 500 == 0 or done == len(jobs):
                pct = 100 * done / len(jobs)
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(jobs) - done) / rate if rate > 0 else 0
                print(
                    f"  [{width}] {done}/{len(jobs)} ({pct:5.1f}%)  "
                    f"{rate:5.1f} fps  eta {eta:5.0f}s",
                    file=sys.stderr, flush=True,
                )
    dt = time.time() - t0
    print(f"[bake {width}x{height}] done in {dt:.1f}s", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("video", type=Path, help="source video (anything ffmpeg can decode)")
    ap.add_argument(
        "--fps", type=int, default=DEFAULT_FPS,
        help=f"target frame rate (default {DEFAULT_FPS})",
    )
    ap.add_argument(
        "--widths", type=str,
        default=",".join(str(w) for w in DEFAULT_WIDTHS),
        help=f"comma-separated width buckets (default {','.join(str(w) for w in DEFAULT_WIDTHS)})",
    )
    ap.add_argument(
        "--single-width", type=int, default=None,
        help="bake only this single width bucket (overrides --widths, useful for sanity runs)",
    )
    ap.add_argument(
        "--cell-aspect", type=float, default=DEFAULT_CELL_ASPECT,
        help=f"terminal cell height-to-width ratio in pixels (default {DEFAULT_CELL_ASPECT})",
    )
    ap.add_argument(
        "--symbols", type=str, default=DEFAULT_SYMBOLS,
        help=f"chafa --symbols value (default '{DEFAULT_SYMBOLS}')",
    )
    ap.add_argument(
        "--colors", type=str, default=DEFAULT_COLORS,
        choices=VALID_COLORS,
        help=f"chafa -c value (default '{DEFAULT_COLORS}'). 'full' is 24-bit "
             f"truecolor (biggest wire footprint, maximum fidelity); '256' "
             f"uses chafa's 8-bit palette (~30-40%% smaller on color-heavy "
             f"frames, still looks great on modern terminals).",
    )
    ap.add_argument(
        "--output", type=Path, default=Path("frames"),
        help="output directory root (default ./frames)",
    )
    ap.add_argument(
        "--workers", type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
        help="parallel chafa workers (default: CPU count minus one)",
    )
    ap.add_argument(
        "--tmp-dir", type=Path, default=None,
        help="directory for extracted PNG frames (default: <output>/_tmp_png)",
    )
    ap.add_argument(
        "--keep-tmp", action="store_true",
        help="don't delete the extracted PNG dir after baking",
    )
    args = ap.parse_args()

    check_tools()

    if not args.video.exists():
        sys.exit(f"[error] source video not found: {args.video}")

    src_w, src_h, duration = probe(args.video)
    print(
        f"[probe] {args.video.name}: {src_w}x{src_h} px, {duration:.1f}s "
        f"(aspect {src_w/src_h:.3f}:1)",
        file=sys.stderr,
    )

    tmp_dir = args.tmp_dir or (args.output / "_tmp_png")
    frames = extract_frames(args.video, args.fps, tmp_dir)

    if args.single_width is not None:
        widths: tuple[int, ...] = (args.single_width,)
    else:
        widths = tuple(int(w) for w in args.widths.split(","))

    print(
        f"[plan] {len(widths)} bucket(s) @ {args.colors} color, "
        f"symbols='{args.symbols}': "
        + ", ".join(
            f"{w}x{compute_height(w, src_w, src_h, args.cell_aspect)}"
            for w in widths
        ),
        file=sys.stderr,
    )

    total_t0 = time.time()
    for w in widths:
        h = compute_height(w, src_w, src_h, args.cell_aspect)
        bake_bucket(
            frames=frames,
            width=w,
            height=h,
            out_dir=args.output / str(w),
            symbols=args.symbols,
            colors=args.colors,
            workers=args.workers,
        )
    total_dt = time.time() - total_t0

    if not args.keep_tmp:
        print(f"[cleanup] removing {tmp_dir}", file=sys.stderr)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"[done] all {len(widths)} bucket(s) baked in {total_dt:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
