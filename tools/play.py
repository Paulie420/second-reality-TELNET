#!/usr/bin/env python3
"""
play.py — play a directory of pre-baked ANSI frames in the current terminal.

No networking. No telnetlib3. No blessed. Just reads `frame-*.ans` files in
sorted order and writes them to stdout at a target frame rate. This is the
sanity check for the bake output: if a directory plays back here with good
motion and no visual garbage, the telnet server will show the same thing to
remote users.

Usage:
    python tools/play.py frames/80
    python tools/play.py frames/120 --fps 30
    python tools/play.py frames/160 --fps 30 --loop
    python tools/play.py frames/80  --start 500 --end 1000

Controls:
    Ctrl-C          quit cleanly (cursor + colors restored)

Design notes:
    - Frames are pre-loaded into memory as raw bytes so playback timing isn't
      affected by disk IO. For a 9-minute bake at 30 fps with mean 44 KB per
      frame, one bucket is ~650 MB resident. That's fine for local testing.
    - Each frame is written as a single blob to stdout, then the cursor is
      moved home. No per-line positioning here — that's the telnet server's
      job (because it needs to center with margins).
    - If the terminal can't keep up, frames are SKIPPED, not queued.
      jquast's shell.py does the same trick implicitly with asyncio.sleep.
    - Exit trap restores the cursor and resets colors even on Ctrl-C /
      unhandled exceptions, so you don't end up in a terminal with a hidden
      cursor and garbage colors.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

# Raw ANSI control sequences. We intentionally do NOT import blessed here —
# this tool is a dumb sanity check and should have zero third-party deps so
# it can be run before `pip install -e .` has even happened.
HOME = b"\x1b[H"
CLEAR = b"\x1b[2J"
HIDE_CURSOR = b"\x1b[?25l"
SHOW_CURSOR = b"\x1b[?25h"
RESET = b"\x1b[0m"


def restore_terminal() -> None:
    """Reset colors and show the cursor. Safe to call multiple times."""
    try:
        sys.stdout.buffer.write(RESET + SHOW_CURSOR + b"\r\n")
        sys.stdout.buffer.flush()
    except Exception:
        pass


def load_frames(
    frame_dir: Path,
    start: int,
    end: int | None,
    skips: list[tuple[int, int]] | None = None,
    max_frames: int = 0,
) -> list[bytes]:
    """Load frame-*.ans files from frame_dir in sorted order, as raw bytes.

    Order of operations matters:
      1. truncate to max_frames (cuts trailing credit card)
      2. apply skip ranges (cuts interior still section)
      3. apply start/end slice (user's debug window)
    """
    files = sorted(frame_dir.glob("frame-*.ans"))
    if not files:
        sys.exit(f"[error] no frame-*.ans files in {frame_dir}")
    if max_frames and max_frames < len(files):
        files = files[:max_frames]
    if skips:
        for s_start, s_end in sorted(skips, reverse=True):
            if s_start < 0 or s_end <= s_start or s_start >= len(files):
                continue
            del files[s_start:min(s_end, len(files))]
    if end is None:
        end = len(files)
    files = files[start:end]
    print(
        f"[load] reading {len(files)} frames from {frame_dir} "
        f"(slice {start}:{end})...",
        file=sys.stderr,
    )
    t0 = time.time()
    data = [p.read_bytes() for p in files]
    total = sum(len(b) for b in data)
    dt = time.time() - t0
    print(
        f"[load] {len(data)} frames, {total/1024/1024:.1f} MB, {dt:.1f}s",
        file=sys.stderr,
    )
    return data


def play(frames: list[bytes], fps: float, loop: bool) -> None:
    """Play the pre-loaded frames at target fps. Skips frames if we fall
    behind the schedule. Exits on Ctrl-C."""
    frame_interval = 1.0 / fps
    out = sys.stdout.buffer
    out.write(HIDE_CURSOR + CLEAR + HOME)
    out.flush()

    print(
        f"[play] {len(frames)} frames at {fps} fps  "
        f"(interval {frame_interval*1000:.1f} ms)",
        file=sys.stderr,
    )
    print(f"[play] Ctrl-C to exit", file=sys.stderr)
    time.sleep(0.5)

    while True:
        t_start = time.monotonic()
        skipped = 0
        for i, blob in enumerate(frames):
            target = t_start + i * frame_interval
            now = time.monotonic()
            # If we're more than a full frame behind, skip this one
            if now - target > frame_interval:
                skipped += 1
                continue
            # If we're early, sleep until it's time
            ahead = target - now
            if ahead > 0:
                time.sleep(ahead)
            out.write(HOME)
            out.write(blob)
            out.flush()
        if not loop:
            break
        # In loop mode, tiny pause between cycles
        time.sleep(0.25)

    # End-of-stream stats
    elapsed = time.monotonic() - t_start
    effective_fps = (len(frames) - skipped) / elapsed if elapsed > 0 else 0
    print(
        f"\n[play] done. elapsed={elapsed:.1f}s  skipped={skipped}  "
        f"effective_fps={effective_fps:.1f}",
        file=sys.stderr,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("frame_dir", type=Path, help="directory of frame-*.ans files")
    ap.add_argument("--fps", type=float, default=30.0, help="playback frame rate (default 30)")
    ap.add_argument("--loop", action="store_true", help="loop forever instead of stopping at end")
    ap.add_argument("--start", type=int, default=0, help="start at frame index N (default 0)")
    ap.add_argument("--end", type=int, default=None, help="stop at frame index N (default: last)")
    ap.add_argument(
        "--skip", action="append", default=None, metavar="START:END",
        help="drop frames [START, END) from playback (can be repeated). "
             "Default skips the 11s still landscape section (1346:1676). "
             "Pass '--skip none' to disable all skips.",
    )
    ap.add_argument(
        "--max-frames", type=int, default=15244,
        help="stop at frame N of the original bake (default 15244, trims "
             "the trailing credit card with a 5-frame safety margin so no "
             "ghost text leaks through the end-hold). 0 to play every "
             "baked frame.",
    )
    args = ap.parse_args()

    # Default skip matches srtelnet.server: 11s cut centered in the 15.2s
    # static landscape freeze (~42.76s to 57.97s in the short webm).
    default_skips: list[tuple[int, int]] = [(1346, 1676)]
    if args.skip is None:
        skips = default_skips
    elif len(args.skip) == 1 and args.skip[0].lower() == "none":
        skips = []
    else:
        skips = []
        for s in args.skip:
            try:
                a, b = s.split(":", 1)
                skips.append((int(a), int(b)))
            except ValueError:
                sys.exit(f"[error] bad --skip value: {s!r} (expected START:END)")

    if not args.frame_dir.is_dir():
        sys.exit(f"[error] not a directory: {args.frame_dir}")

    # install safety net: always restore the terminal, even on SIGINT / crash
    def _sig_handler(signum, frame):
        restore_terminal()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        frames = load_frames(
            args.frame_dir,
            args.start,
            args.end,
            skips=skips,
            max_frames=args.max_frames,
        )
        play(frames, args.fps, args.loop)
    finally:
        restore_terminal()
    return 0


if __name__ == "__main__":
    sys.exit(main())
