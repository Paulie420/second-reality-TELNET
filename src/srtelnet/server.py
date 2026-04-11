#!/usr/bin/env python3
"""
srtelnet.server — telnet server that plays pre-baked Second Reality frames.

Architecture:
    - Frames live on disk under frames/<W>/frame-*.ans, one file per frame
      per width bucket. Baked by tools/bake_frames.py.
    - On startup we *index* every bucket (list files, measure height from the
      first file) but do NOT preload frame bytes. Frames are read from disk
      per playback step; the Linux page cache makes hot frames effectively
      free after the first client touches them.
    - One asyncio shell coroutine per connected client.
    - telnetlib3 already stores NAWS (rows/cols) in extra_info for us.
    - A per-connection background task reads client keystrokes into a queue
      so the playback loop can poll them non-blockingly between frames.

Controls (transmitted to the user in the welcome screen):
    q / Ctrl-C      quit
    space           pause / resume
    left / right    seek -/+ 5 seconds

Usage:
    srtelnet-server --frames ./frames --port 2323
    srtelnet-server --frames ~/second-reality-TELNET/frames --port 23
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import telnetlib3

# ---------------------------------------------------------------- constants
DEFAULT_PORT = 2323
DEFAULT_FPS = 30.0
DEFAULT_FRAMES_ROOT = Path(os.environ.get("SRTELNET_FRAMES", "frames"))
BUCKETS_ORDER = (40, 60, 80, 100, 120, 140, 160, 180, 200)
# Trim the trailing "GRAPHICS/MUSIC/CODE" credit card off the short bake.
# ffmpeg blackdetect puts the last black segment end at 508.274s -> frame 15249,
# but in practice the credit text starts fading in a few frames earlier than
# that, so leaning on 15249 held ~2-3 frames of ghost text on screen during
# the end-hold. Back off by 5 frames (~0.17s at 30 fps) so the last frame
# the end-hold freezes on is solidly inside the final black stretch.
DEFAULT_MAX_FRAMES = 15244
# Frame ranges to skip entirely during playback. Each (start, end) removes
# frames [start, end) from the playback index. Default cuts 11s out of the
# 15.2s static landscape section at 42.76s-57.97s (ffmpeg freezedetect),
# centered so ~2.1s of stillness remains on each side of the cut. The demo
# plays quiet music-only pauses during this stretch which we can't reproduce
# over telnet, so skipping most of it keeps the viewer engaged without an
# abrupt splice.
DEFAULT_SKIPS: list[tuple[int, int]] = [(1346, 1676)]
# After the last frame, hold the final image on screen for this many seconds
# before transitioning to the goodbye message. Compensates for the fact that
# MAX_FRAMES = 15249 stops a hair before the video technically ends.
DEFAULT_END_HOLD = 0.5

# ANSI control strings (telnetlib3 accepts str, emits utf-8 on the wire)
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET = "\x1b[0m"
CLEAR = "\x1b[2J"
HOME = "\x1b[H"

log = logging.getLogger("srtelnet")


# ---------------------------------------------------------------- bucket data
class Bucket:
    """One width bucket, indexed but not preloaded. Frames are read on demand."""
    __slots__ = ("width", "height", "paths")

    def __init__(self, width: int, height: int, paths: list[Path]):
        self.width = width
        self.height = height
        self.paths = paths


BUCKETS: dict[int, Bucket] = {}
MAX_FRAMES: int | None = None  # set at startup from CLI
SKIPS: list[tuple[int, int]] = []  # set at startup from CLI


def _parse_frame_lines(raw: str) -> list[str]:
    """Split a chafa .ans file into per-row strings, dropping the trailing
    empty line produced by the final newline."""
    lines = raw.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def load_bucket_index(root: Path, width: int) -> Bucket | None:
    """Index one bucket: list its frames and measure height from frame 0.
    Returns None if the bucket directory is absent or empty."""
    bdir = root / str(width)
    if not bdir.is_dir():
        return None
    files = sorted(bdir.glob("frame-*.ans"))
    if not files:
        return None
    if MAX_FRAMES is not None and MAX_FRAMES < len(files):
        files = files[:MAX_FRAMES]
    # Apply skip ranges: drop frames that fall inside any [start, end) range.
    # Iterate in reverse so each slice keeps earlier indices valid.
    for start, end in sorted(SKIPS, reverse=True):
        if start < 0 or end <= start:
            continue
        if start >= len(files):
            continue
        end = min(end, len(files))
        del files[start:end]
    first = files[0].read_text(encoding="utf-8", errors="replace")
    height = len(_parse_frame_lines(first))
    log.info("bucket %d indexed: %d frames, %dx%d", width, len(files), width, height)
    return Bucket(width, height, files)


def load_all_buckets(root: Path) -> None:
    """Discover every bucket under `root`. At least one must be present."""
    for w in BUCKETS_ORDER:
        b = load_bucket_index(root, w)
        if b is not None:
            BUCKETS[w] = b
    if not BUCKETS:
        sys.exit(
            f"[error] no frame buckets found under {root}\n"
            f"        expected frames/<W>/frame-*.ans files"
        )
    log.info("server ready with %d buckets: %s",
             len(BUCKETS), sorted(BUCKETS.keys()))


def pick_bucket(cols: int, rows: int) -> Bucket:
    """Pick the largest bucket that fits the client's window. If nothing fits
    (very small terminal), fall back to the smallest we have."""
    fit = [b for b in BUCKETS.values() if b.width <= cols and b.height <= rows]
    if fit:
        return max(fit, key=lambda b: b.width)
    return min(BUCKETS.values(), key=lambda b: b.width)


def read_frame(bucket: Bucket, index: int) -> list[str]:
    """Read one frame from disk and return its row list."""
    raw = bucket.paths[index].read_text(encoding="utf-8", errors="replace")
    return _parse_frame_lines(raw)


# ---------------------------------------------------------------- rendering
def render_frame_at(lines: list[str], top: int, left: int) -> str:
    """Wrap a frame's rows with per-line cursor-move escapes so it lands at
    (top, left) regardless of whatever else is on the client's screen."""
    parts: list[str] = []
    for i, line in enumerate(lines):
        parts.append(f"\x1b[{top + i + 1};{left + 1}H{line}")
    return "".join(parts)


# ---------------------------------------------------------------- welcome
# Figlet "slant" font, pre-rendered. Stored as raw strings so we don't need
# figlet on the host. SECOND is 38 cols wide, REALITY is 45 cols wide — the
# two blocks are centered independently, so the shape looks natural.
_FIGLET_SECOND = [
    r"   _____ ________________  _   ______ ",
    r"  / ___// ____/ ____/ __ \/ | / / __ \ ",
    r"  \__ \/ __/ / /   / / / /  |/ / / / /",
    r" ___/ / /___/ /___/ /_/ / /|  / /_/ / ",
    r"/____/_____/\____/\____/_/ |_/_____/  ",
]
_FIGLET_REALITY = [
    r"    ____  _________    __    ____________  __",
    r"   / __ \/ ____/   |  / /   /  _/_  __/\ \/ /",
    r"  / /_/ / __/ / /| | / /    / /  / /    \  / ",
    r" / _, _/ /___/ ___ |/ /____/ /  / /     / /  ",
    r"/_/ |_/_____/_/  |_/_____/___/ /_/     /_/   ",
]
_FIGLET_TELNET = [
    r"  ______________    _   ______________",
    r" /_  __/ ____/ /   / | / / ____/_  __/",
    r"  / / / __/ / /   /  |/ / __/   / /   ",
    r" / / / /___/ /___/ /|  / /___  / /    ",
    r"/_/ /_____/_____/_/ |_/_____/ /_/     ",
]

# Truecolor helpers. Modern terminals speak 24-bit; 256-color and 16-color
# clients will still map these to their nearest palette entry.
def _fg(r: int, g: int, b: int) -> str:
    return f"\x1b[38;2;{r};{g};{b}m"

_C_FIRE = [
    _fg(255, 40, 40),    # deep red
    _fg(255, 90, 30),    # orange-red
    _fg(255, 140, 20),   # orange
    _fg(255, 180, 30),   # amber
    _fg(255, 220, 60),   # gold
]
_C_CYAN   = _fg(0, 220, 255)
_C_PINK   = _fg(255, 80, 200)
_C_PURPLE = _fg(180, 100, 255)
_C_GOLD   = _fg(255, 200, 80)
_C_GREEN  = _fg(120, 255, 140)
_C_WHITE  = _fg(235, 235, 235)
_C_DIM    = _fg(130, 130, 150)
_BOLD     = "\x1b[1m"
_BLINK    = "\x1b[5m"


def render_welcome(cols: int, rows: int) -> str:
    """Render a BBS-style truecolor ANSI welcome screen with figlet banner,
    fire gradient, box-drawing borders, and the credits/controls block."""
    raw: list[str] = []     # plaintext, for centering math
    col: list[str] = []     # colored, for output

    def add(line: str, color: str = "") -> None:
        raw.append(line)
        col.append(f"{color}{line}{RESET}" if color else line)

    # Pad each figlet block to a uniform width so all its lines center together
    s_w = max(len(l) for l in _FIGLET_SECOND)
    r_w = max(len(l) for l in _FIGLET_REALITY)
    t_w = max(len(l) for l in _FIGLET_TELNET)
    second = [l.ljust(s_w) for l in _FIGLET_SECOND]
    reality = [l.ljust(r_w) for l in _FIGLET_REALITY]
    telnet = [l.ljust(t_w) for l in _FIGLET_TELNET]

    # --- figlet banner with fire gradient (SECOND / REALITY / TELNET stack)
    add("")
    for i, ln in enumerate(second):
        add(ln, _BOLD + _C_FIRE[i % len(_C_FIRE)])
    for i, ln in enumerate(reality):
        add(ln, _BOLD + _C_FIRE[(i + 2) % len(_C_FIRE)])
    for i, ln in enumerate(telnet):
        add(ln, _BOLD + _C_FIRE[(i + 4) % len(_C_FIRE)])
    add("")

    # --- subtitle band
    bar = "\u2593\u2592\u2591" + "\u2550" * 48 + "\u2591\u2592\u2593"
    add(bar, _C_PURPLE)
    add("\u00bb 1993  \u00b7  FUTURE CREW  \u00b7  telnet edition  \u00ab", _C_GOLD + _BOLD)
    add(bar, _C_PURPLE)
    add("")

    # --- stats
    add("[ 15,244 frames  \u00b7  30 fps  \u00b7  truecolor ANSI  \u00b7  9 width buckets ]", _C_CYAN)
    add("")

    # --- controls box — all rows 44 cells wide (2 corners + 42 interior)
    add("\u250c" + "\u2500" * 16 + " CONTROLS " + "\u2500" * 16 + "\u2510", _C_DIM)
    add("\u2502   q  or  Ctrl-C     quit                 \u2502", _C_WHITE)
    add("\u2502   space             pause / resume       \u2502", _C_WHITE)
    add("\u2502   \u2190 / \u2192             seek -/+ 5 seconds   \u2502", _C_WHITE)
    add("\u2514" + "\u2500" * 42 + "\u2518", _C_DIM)
    add("")

    # --- credits / shouts
    add("shouts to:", _C_PINK + _BOLD)
    add("  Future Crew           \u2014 the demo itself (1993)", _C_DIM)
    add("  Jeff Quast            \u2014 telnetlib3 + network23/1984.ws template", _C_DIM)
    add("  Hans Petter Jansson   \u2014 chafa, the image\u2192ANSI renderer", _C_DIM)
    add("")
    add("streamed by paulie420  \u00b7  20forbeers.com", _C_GREEN)
    add("")
    add(">>>   PRESS ANY KEY TO JACK IN   <<<", _BOLD + _BLINK + _C_PINK)
    add("")

    # Note: the controls-box border and subtitle bar contain unicode
    # box-drawing / block chars, which len() counts as single cells — that
    # matches how a monospace terminal renders them, so centering is correct.
    n = len(raw)
    top = max(0, (rows - n) // 2)
    out = [HOME, CLEAR]
    for i, (r_line, c_line) in enumerate(zip(raw, col)):
        pad = max(0, (cols - len(r_line)) // 2)
        out.append(f"\x1b[{top + i + 1};1H{' ' * pad}{c_line}")
    out.append(RESET)
    return "".join(out)


def render_goodbye(cols: int, rows: int) -> str:
    """Shown on clean disconnect."""
    msg = "Thanks for watching. -- second-reality-TELNET"
    row = max(1, rows // 2)
    col = max(1, (cols - len(msg)) // 2 + 1)
    return CLEAR + HOME + f"\x1b[{row};{col}H{msg}\r\n"


# ---------------------------------------------------------------- key reader
async def key_reader(reader, queue: asyncio.Queue) -> None:
    """Read bytes from the client forever, parse them into logical keys,
    push them onto `queue`. Exits on EOF / error by pushing a 'DISCONNECT'
    token so the playback loop can notice."""
    buf = ""
    while True:
        try:
            chunk = await reader.read(1024)
        except Exception:
            await queue.put("DISCONNECT")
            return
        if not chunk:
            await queue.put("DISCONNECT")
            return
        buf += chunk
        i = 0
        while i < len(buf):
            ch = buf[i]
            if ch == "\x1b":
                # CSI sequence: need at least ESC [ X
                if i + 2 < len(buf) and buf[i + 1] == "[":
                    code = buf[i + 2]
                    key = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(code)
                    if key:
                        await queue.put(key)
                    i += 3
                    continue
                if i + 2 >= len(buf):
                    # Incomplete — stop and wait for more bytes
                    break
                # Unknown escape — skip the ESC and keep going
                i += 1
                continue
            if ch in ("q", "Q"):
                await queue.put("QUIT")
            elif ch == "\x03":  # Ctrl-C
                await queue.put("QUIT")
            elif ch == " ":
                await queue.put("SPACE")
            elif ch in ("\r", "\n"):
                await queue.put("ENTER")
            # silently ignore everything else
            i += 1
        buf = buf[i:]


# ---------------------------------------------------------------- shell
async def shell(reader, writer) -> None:
    """Per-connection coroutine: welcome screen, then playback loop."""
    peer = writer.get_extra_info("peername", ("?", 0))
    log.info("connect %s", peer)

    fps = float(os.environ.get("SRTELNET_FPS", DEFAULT_FPS))
    frame_interval = 1.0 / fps
    seek_frames = int(5 * fps)  # 5 seconds of frames

    key_q: asyncio.Queue = asyncio.Queue()
    reader_task = asyncio.create_task(key_reader(reader, key_q))
    loop = asyncio.get_event_loop()

    try:
        # Wait briefly for NAWS to arrive. telnetlib3 negotiates after accept;
        # giving it up to 1s means slow clients still get their real size
        # rather than the 80x25 default.
        deadline = loop.time() + 1.0
        while loop.time() < deadline:
            if writer.get_extra_info("cols") and writer.get_extra_info("rows"):
                break
            await asyncio.sleep(0.05)

        cols = writer.get_extra_info("cols") or 80
        rows = writer.get_extra_info("rows") or 25
        log.info("%s NAWS=%dx%d", peer, cols, rows)

        writer.write(HIDE_CURSOR + CLEAR + HOME)
        await writer.drain()

        # --- welcome screen ---
        writer.write(render_welcome(cols, rows))
        await writer.drain()

        # Wait for any keypress (up to 30s), then drop any extras.
        try:
            key = await asyncio.wait_for(key_q.get(), timeout=30.0)
            if key in ("QUIT", "DISCONNECT"):
                return
        except asyncio.TimeoutError:
            pass
        while not key_q.empty():
            try:
                key_q.get_nowait()
            except asyncio.QueueEmpty:
                break

        # --- playback ---
        bucket = pick_bucket(cols, rows)
        top = max(0, (rows - bucket.height) // 2)
        left = max(0, (cols - bucket.width) // 2)
        n_frames = len(bucket.paths)
        log.info("%s playback bucket=%d (%dx%d) margin=(%d,%d) frames=%d",
                 peer, bucket.width, bucket.width, bucket.height, left, top, n_frames)

        writer.write(CLEAR + HOME)
        await writer.drain()

        i = 0
        paused = False
        target = loop.time()
        skipped = 0

        while i < n_frames:
            # Drain any pending keystrokes
            resync = False
            while not key_q.empty():
                try:
                    key = key_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if key in ("QUIT", "DISCONNECT"):
                    return
                if key == "SPACE":
                    paused = not paused
                    resync = True
                elif key == "LEFT":
                    i = max(0, i - seek_frames)
                    resync = True
                elif key == "RIGHT":
                    i = min(n_frames - 1, i + seek_frames)
                    resync = True

            if paused:
                # Block on the queue until a key arrives (or disconnect).
                try:
                    key = await asyncio.wait_for(key_q.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    continue
                if key in ("QUIT", "DISCONNECT"):
                    return
                if key == "SPACE":
                    paused = False
                    target = loop.time()
                elif key == "LEFT":
                    i = max(0, i - seek_frames)
                elif key == "RIGHT":
                    i = min(n_frames - 1, i + seek_frames)
                continue

            if resync:
                target = loop.time()

            now = loop.time()
            delay = target - now
            if delay < -frame_interval:
                # We're more than a whole frame behind schedule — skip rather
                # than queue work that'll just fall further behind.
                skipped += 1
                i += 1
                target += frame_interval
                continue
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                lines = read_frame(bucket, i)
                writer.write(render_frame_at(lines, top, left))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return
            except Exception as e:
                log.warning("%s frame %d read/write error: %s", peer, i, e)
                return

            i += 1
            target += frame_interval

        log.info("%s playback complete (skipped=%d of %d)", peer, skipped, n_frames)

        # Hold the last frame for a beat before the goodbye transition so the
        # demo doesn't feel like it snaps to black.
        try:
            await asyncio.sleep(DEFAULT_END_HOLD)
        except asyncio.CancelledError:
            pass

    finally:
        reader_task.cancel()
        try:
            writer.write(RESET + SHOW_CURSOR + render_goodbye(
                writer.get_extra_info("cols") or 80,
                writer.get_extra_info("rows") or 25,
            ))
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass
        log.info("disconnect %s", peer)


# ---------------------------------------------------------------- entrypoint
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"TCP port to listen on (default {DEFAULT_PORT})")
    ap.add_argument("--host", default="0.0.0.0",
                    help="interface to bind to (default 0.0.0.0)")
    ap.add_argument("--frames", type=Path, default=DEFAULT_FRAMES_ROOT,
                    help="root directory of baked frames (default ./frames "
                         "or $SRTELNET_FRAMES)")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS,
                    help=f"playback frame rate (default {DEFAULT_FPS})")
    ap.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES,
                    help=f"stop playback at frame N (default {DEFAULT_MAX_FRAMES}, "
                         "trims the trailing credit card off the short bake; "
                         "set 0 to play every baked frame)")
    ap.add_argument("--skip", action="append", default=None,
                    metavar="START:END",
                    help="drop frames [START, END) from playback (can be "
                         "repeated). Default skips the 11s still landscape "
                         "section. Pass '--skip none' to disable all skips.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="debug-level logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    global MAX_FRAMES, SKIPS
    MAX_FRAMES = args.max_frames if args.max_frames > 0 else None

    if args.skip is None:
        SKIPS = list(DEFAULT_SKIPS)
    elif len(args.skip) == 1 and args.skip[0].lower() == "none":
        SKIPS = []
    else:
        SKIPS = []
        for s in args.skip:
            try:
                a, b = s.split(":", 1)
                SKIPS.append((int(a), int(b)))
            except ValueError:
                sys.exit(f"[error] bad --skip value: {s!r} (expected START:END)")
    if SKIPS:
        log.info("playback skips: %s", SKIPS)

    # FPS is read from env var inside the shell coroutine so the admin can
    # override without code changes. Stash the CLI choice there.
    os.environ["SRTELNET_FPS"] = str(args.fps)

    load_all_buckets(args.frames)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    coro = telnetlib3.create_server(
        host=args.host,
        port=args.port,
        shell=shell,
        force_binary=True,
        encoding="utf8",
        timeout=0,  # no idle-disconnect — the video is ~9 minutes
    )
    server = loop.run_until_complete(coro)
    if server.sockets:
        host, port, *_ = server.sockets[0].getsockname()
        log.info("listening on %s:%s", host, port)
    else:
        log.info("listening on %s:%s", args.host, args.port)

    try:
        loop.run_until_complete(server.wait_closed())
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.close()
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
