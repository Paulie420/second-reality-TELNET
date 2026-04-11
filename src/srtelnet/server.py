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
import socket
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
# How long we hold the goodbye/BBS-ad screen after the demo ends before
# dropping the connection. Any keypress short-circuits the wait.
DEFAULT_GOODBYE_HOLD = 30.0
# Smallest terminal we'll serve. Below this we politely disconnect instead
# of squashing chafa output into an unreadable mess.
MIN_COLS = 40
MIN_ROWS = 20
# Upper bound on any single writer.drain() in the playback loop. We only
# call drain when the write buffer is known to be small (see below), so a
# wedged drain means the client really has stopped ACKing. Generous
# timeout with keepalive as the eventual backstop.
DRAIN_TIMEOUT = 30.0
# Write-buffer backpressure knobs. A WAN client with less bandwidth than
# the frame stream produces (up to ~2 MB/s on the 200-wide bucket) will
# fill the kernel send buffer, and writer.drain() will block waiting for
# the client to catch up. Rather than blocking (and then tripping the
# drain timeout and killing the connection), we check the transport's
# queued-byte count BEFORE each frame and skip the frame if we're backed
# up — the same coping strategy the wall-clock skip logic uses, but
# keyed on actual network state instead of elapsed time.
WRITE_BUF_HIGH = 1024 * 1024   # 1 MB: drain blocks above this
WRITE_BUF_LOW = 256 * 1024     # 256 KB: drain returns below this
WRITE_BUF_SKIP = 512 * 1024    # 512 KB: skip next frame above this
# Path to the persistent lifetime connection counter. Relative to the
# server's working directory. Overridable via --counter-file or the
# SRTELNET_COUNTER_FILE env var. Stored as a plain ASCII integer.
DEFAULT_COUNTER_FILE = Path(
    os.environ.get("SRTELNET_COUNTER_FILE", "state/counter.txt")
)

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
# Figlet "smslant" (small slant) font, pre-rendered. Stored as raw strings
# so we don't need figlet on the host. Four rows tall each, same slant
# shape as the full slant font — small enough to stack all three
# (SECOND / REALITY / TELNET) and still leave room for info, controls,
# and a press-any-key prompt on a classic 80x25 terminal.
_FIGLET_SECOND = [
    r"   _________________  _  _____ ",
    r"  / __/ __/ ___/ __ \/ |/ / _ \ ",
    r" _\ \/ _// /__/ /_/ /    / // /",
    r"/___/___/\___/\____/_/|_/____/ ",
]
_FIGLET_REALITY = [
    r"   ___  _______   __   ____________  __",
    r"  / _ \/ __/ _ | / /  /  _/_  __/\ \/ /",
    r" / , _/ _// __ |/ /___/ /  / /    \  / ",
    r"/_/|_/___/_/ |_/____/___/ /_/     /_/  ",
]
_FIGLET_TELNET = [
    r" ____________   _  ____________",
    r"/_  __/ __/ /  / |/ / __/_  __/",
    r" / / / _// /__/    / _/  / /   ",
    r"/_/ /___/____/_/|_/___/ /_/    ",
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


def _build_banner() -> list[tuple[str, str]]:
    """Build the 'SECOND REALITY / TELNET' banner as a list of (line, color)
    tuples, 8 rows tall, up to 72 cols wide.

    Layout:

            SECOND REALITY
                TELNET

    SECOND and REALITY are joined with a single space on the top 4 rows.
    TELNET sits on the bottom 4 rows, horizontally centered under the
    SECOND REALITY block by visual bounding box (not char count) so
    asymmetric trailing whitespace in the figlets doesn't visually pull
    TELNET off-center. The caller centers the entire block inside the
    terminal."""
    s_w = max(len(l) for l in _FIGLET_SECOND)
    r_w = max(len(l) for l in _FIGLET_REALITY)
    t_w = max(len(l) for l in _FIGLET_TELNET)
    second  = [l.ljust(s_w) for l in _FIGLET_SECOND]
    reality = [l.ljust(r_w) for l in _FIGLET_REALITY]
    telnet  = [l.ljust(t_w) for l in _FIGLET_TELNET]

    banner_w = s_w + 1 + r_w                 # top row width (char-level)
    top_rows = [f"{second[i]} {reality[i]}".ljust(banner_w) for i in range(4)]
    bot_rows = telnet

    # Visual bounding box of each block: leftmost col that holds any
    # non-whitespace, and rightmost col of any non-whitespace. This
    # ignores figlet trailing whitespace, which is what the eye does.
    def _vspan(lines):
        left = min(len(l) - len(l.lstrip()) for l in lines)
        right = max(len(l.rstrip()) for l in lines)
        return left, right

    t_left, t_right = _vspan(top_rows)            # top visual box
    b_left, b_right = _vspan(bot_rows)            # telnet visual box
    # pad + b_left .. pad + b_right should share its midpoint with the
    # top block. Solve for pad; round up on .5 to bias against the side
    # with more trailing whitespace (which is always the right side for
    # slant figlets, so biasing right cancels the drift).
    telnet_pad = max(
        0,
        ((t_left + t_right) - (b_left + b_right) + 1) // 2,
    )

    rows_out: list[tuple[str, str]] = []
    # Top half: SECOND + space + REALITY, every row padded to banner_w.
    for i in range(4):
        rows_out.append((top_rows[i], _BOLD + _C_FIRE[i % len(_C_FIRE)]))
    # Bottom half: TELNET, offset so its visual midpoint matches the top.
    for i in range(4):
        line = (" " * telnet_pad + bot_rows[i]).ljust(banner_w)
        rows_out.append((line, _BOLD + _C_FIRE[(i + 2) % len(_C_FIRE)]))
    return rows_out


def render_welcome(
    cols: int, rows: int, session: int = 0, lifetime: int = 0
) -> str:
    """Render a BBS-style truecolor ANSI welcome screen. Designed to fit
    exactly on an 80x25 terminal — taller terminals just get extra top/
    bottom margin via the centering math.

    `session` is the connection counter since this process started.
    `lifetime` is the persistent all-time connection counter. Both slot
    into the stats line when non-zero."""
    raw: list[str] = []     # plaintext, for centering math
    col: list[str] = []     # colored, for output

    def add(line: str, color: str = "") -> None:
        raw.append(line)
        col.append(f"{color}{line}{RESET}" if color else line)

    # --- figlet banner (8 rows): SECOND REALITY / TELNET in fire gradient
    for line, color in _build_banner():
        add(line, color)

    # --- subtitle band (3 rows)
    bar = "\u2593\u2592\u2591" + "\u2550" * 48 + "\u2591\u2592\u2593"
    add(bar, _C_PURPLE)
    add("\u00bb  FUTURE CREW  \u00b7  1993  \u00b7  demoscene immortal  \u00ab",
        _C_GOLD + _BOLD)
    add(bar, _C_PURPLE)

    # --- stats (1 row) with session + lifetime counters when available
    parts = ["15,244 frames", "30 fps", "truecolor ANSI"]
    if session > 0:
        parts.append(f"session #{session}")
    if lifetime > 0:
        parts.append(f"lifetime #{lifetime}")
    stats = "[ " + "  \u00b7  ".join(parts) + " ]"
    add(stats, _C_CYAN)

    # --- one-line credits (1 row)
    add("thanks: Future Crew  \u00b7  Jeff Quast  \u00b7  Hans Petter Jansson",
        _C_DIM)

    add("")  # spacer between info and controls

    # --- controls box (5 rows, 44 cells wide)
    add("\u250c" + "\u2500" * 16 + " CONTROLS " + "\u2500" * 16 + "\u2510", _C_DIM)
    add("\u2502   q  or  Ctrl-C     quit                 \u2502", _C_WHITE)
    add("\u2502   space             pause / resume       \u2502", _C_WHITE)
    add("\u2502   \u2190 / \u2192             seek -/+ 5 seconds   \u2502", _C_WHITE)
    add("\u2514" + "\u2500" * 42 + "\u2518", _C_DIM)

    # --- signoff + prompt (2 rows)
    add("streamed by paulie420  \u00b7  20forbeers.com", _C_GREEN)
    add(">>>   PRESS ANY KEY TO JACK IN   <<<", _BOLD + _BLINK + _C_PINK)
    # Total: 8 (banner) + 3 (subtitle) + 1 (stats) + 1 (credits) + 1 (gap)
    # + 5 (controls) + 2 (signoff) = 21 rows. Centered in 25 gives 2-row
    # top margin + 2-row bottom margin.

    n = len(raw)
    top = max(0, (rows - n) // 2)
    out = [HOME, CLEAR]
    for i, (r_line, c_line) in enumerate(zip(raw, col)):
        pad = max(0, (cols - len(r_line)) // 2)
        out.append(f"\x1b[{top + i + 1};1H{' ' * pad}{c_line}")
    out.append(RESET)
    return "".join(out)


def render_goodbye(
    cols: int, rows: int, session: int = 0, lifetime: int = 0
) -> str:
    """BBS-style exit screen, shares its banner with the welcome so they
    feel like a matched pair. Fire-gradient SECOND REALITY / TELNET logo,
    20forbeers.com BBS ad, classic 'NO CARRIER' dial-up sign-off. Held on
    the client until they press a key or the hold timer expires."""
    raw: list[str] = []
    col: list[str] = []

    def add(line: str, color: str = "") -> None:
        raw.append(line)
        col.append(f"{color}{line}{RESET}" if color else line)

    # --- figlet banner (8 rows): same shape as the welcome
    for line, color in _build_banner():
        add(line, color)

    # --- subtitle band with BBS name (3 rows)
    bar = "\u2593\u2592\u2591" + "\u2550" * 48 + "\u2591\u2592\u2593"
    add(bar, _C_PURPLE)
    add("\u00bb  2o fOr beeRS bbS  \u00b7  dial in today  \u00ab",
        _C_GOLD + _BOLD)
    add(bar, _C_PURPLE)

    # --- stats: close the loop on which connection the user was (1 row)
    if lifetime > 0:
        stats = f"[ you were connection #{lifetime} of all time ]"
    elif session > 0:
        stats = f"[ you were session #{session} this run ]"
    else:
        stats = "[ thanks for jacking in ]"
    add(stats, _C_CYAN)

    # --- BBS ad block (7 rows)
    add("WEBSITE :  20ForBeers.com", _C_WHITE)
    add("An ANSi TELNET BBS:", _C_DIM)
    add("TELNET  :  20ForBeers.com:1337", _C_CYAN)
    add("SSH     :  20ForBeers.com:1338", _C_CYAN)
    add("")
    add("'Dial-in' Today!", _C_GREEN + _BOLD)
    # Classic modem drop — one last BBS callback on the way out
    add("NO CARRIER", _BOLD + _C_PINK)
    # Total: 8 + 3 + 1 + 4 + 1 + 1 + 1 = 19 rows. Plus top/bottom margin
    # auto-centered by the rows - n math below.

    n = len(raw)
    top = max(1, (rows - n) // 2)
    out = [RESET, SHOW_CURSOR, CLEAR, HOME]
    for i, (r_line, c_line) in enumerate(zip(raw, col)):
        pad = max(0, (cols - len(r_line)) // 2)
        out.append(f"\x1b[{top + i};1H{' ' * pad}{c_line}")
    out.append(RESET)
    # Park the cursor a couple rows below NO CARRIER and emit blank lines
    # so the client's "Connection closed by foreign host." message lands
    # with breathing room instead of jammed right under NO CARRIER.
    out.append(f"\x1b[{min(rows, top + n + 1)};1H\r\n\r\n")
    return "".join(out)


def render_too_small(cols: int, rows: int) -> str:
    """Polite rejection for tiny terminals. Fits in ~5 rows at any width."""
    lines = [
        f"Your terminal is {cols}x{rows} — too small for Second Reality.",
        f"Please resize to at least {MIN_COLS}x{MIN_ROWS} and reconnect.",
        "",
        "(the biggest version of the demo wants 200x75 if you've got it)",
    ]
    out = [RESET, SHOW_CURSOR, CLEAR, HOME]
    top = max(1, (rows - len(lines)) // 2)
    for i, line in enumerate(lines):
        pad = max(0, (cols - len(line)) // 2)
        out.append(f"\x1b[{top + i};1H{' ' * pad}{line}")
    out.append(f"\x1b[{rows};1H\r\n")
    return "".join(out)


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

# Per-process session counter — total connections since this process started.
# Resets on server restart.
_SESSION_COUNTER = 0

# Lifetime connection counter — total connections ever served, persisted to
# disk so restarts don't lose the count. Loaded at startup from _COUNTER_PATH
# and rewritten on every new connection. Best-effort: if the file is missing
# or unwritable we log a warning and keep counting in memory.
_LIFETIME_COUNTER = 0
_COUNTER_PATH: Path | None = None


def load_counter(path: Path) -> int:
    """Read the persistent lifetime counter from disk. Returns 0 if the file
    doesn't exist, is empty, or can't be parsed — the counter is best-effort,
    never fatal."""
    try:
        raw = path.read_text(encoding="ascii").strip()
        return int(raw) if raw else 0
    except FileNotFoundError:
        return 0
    except (ValueError, OSError) as e:
        log.warning("counter file %s unreadable (%s); starting at 0", path, e)
        return 0


def save_counter(path: Path, value: int) -> None:
    """Atomically persist the lifetime counter. Writes to a temp file and
    renames so a crash mid-write can't corrupt the file. Never raises —
    write failures log a warning and the in-memory counter keeps ticking."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(f"{value}\n", encoding="ascii")
        tmp.replace(path)
    except OSError as e:
        log.warning("counter file %s unwritable (%s); lifetime count not saved",
                    path, e)


async def _drain_bounded(writer) -> None:
    """Drain the write buffer with a hard timeout. A wedged client (slow
    cellular, misbehaving proxy) can otherwise block the shell forever.
    Raises ConnectionResetError on timeout so the play loop treats it as a
    disconnect — same branch as a real RST."""
    try:
        await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise ConnectionResetError("drain timeout") from e


def _configure_write_buffer(writer) -> None:
    """Set explicit high/low watermarks on the underlying transport so
    backpressure kicks in at predictable points (not whatever the event
    loop's default is). Paired with _write_buffer_size() checks in the
    play loop to skip frames instead of blocking when a WAN client can't
    keep up."""
    try:
        transport = writer.transport
        if transport is None:
            return
        transport.set_write_buffer_limits(
            high=WRITE_BUF_HIGH, low=WRITE_BUF_LOW
        )
    except (AttributeError, NotImplementedError, OSError) as e:
        log.debug("set_write_buffer_limits failed: %s", e)


def _write_buffer_size(writer) -> int:
    """How many bytes are queued in the transport's send buffer right now.
    Used to decide whether the client is keeping up; if this grows beyond
    WRITE_BUF_SKIP we skip the next frame instead of stuffing more data
    into a buffer the client clearly can't drain."""
    try:
        transport = writer.transport
        if transport is None:
            return 0
        return transport.get_write_buffer_size()
    except (AttributeError, NotImplementedError):
        return 0


def _enable_keepalive(writer) -> None:
    """Turn on TCP keepalive for this connection so dead peers are detected
    faster than the OS default (~2 hours on Linux). Best-effort: silently
    no-ops on platforms / transports that don't expose a socket."""
    try:
        sock = writer.get_extra_info("socket")
    except Exception:
        return
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Linux-only knobs: start probing after 30s idle, probe every 10s,
        # give up after 3 failed probes. Total dead-peer detection ~= 60s.
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError as e:
        log.debug("keepalive setsockopt failed: %s", e)


async def _play_once(writer, key_q, peer, bucket, cols, rows, fps) -> str:
    """Play the demo once through and return a status: 'QUIT', 'DISCONNECT',
    or 'DONE' (playback reached the end normally)."""
    loop = asyncio.get_event_loop()
    frame_interval = 1.0 / fps
    seek_frames = int(5 * fps)

    top = max(0, (rows - bucket.height) // 2)
    left = max(0, (cols - bucket.width) // 2)
    n_frames = len(bucket.paths)
    log.info("%s playback bucket=%d (%dx%d) margin=(%d,%d) frames=%d",
             peer, bucket.width, bucket.width, bucket.height, left, top, n_frames)

    writer.write(CLEAR + HOME)
    try:
        await _drain_bounded(writer)
    except (ConnectionResetError, BrokenPipeError):
        return "DISCONNECT"

    i = 0
    paused = False
    target = loop.time()
    skipped = 0

    while i < n_frames:
        # Live NAWS resize handling. telnetlib3 updates the extra_info dict
        # whenever a fresh NAWS subnegotiation arrives, so we just re-read
        # it each iteration. If the client resized their terminal, we may
        # need to switch buckets and/or re-center on the new window.
        new_cols = writer.get_extra_info("cols") or cols
        new_rows = writer.get_extra_info("rows") or rows
        if new_cols != cols or new_rows != rows:
            new_bucket = pick_bucket(new_cols, new_rows)
            log.info(
                "%s resize %dx%d->%dx%d  bucket %d->%d",
                peer, cols, rows, new_cols, new_rows,
                bucket.width, new_bucket.width,
            )
            cols, rows = new_cols, new_rows
            if new_bucket is not bucket:
                bucket = new_bucket
                n_frames = len(bucket.paths)
                if i >= n_frames:
                    i = n_frames - 1
            top = max(0, (rows - bucket.height) // 2)
            left = max(0, (cols - bucket.width) // 2)
            # Old frame leaves stale cells around the edges of the new
            # bucket. Wipe the screen so the next render is clean.
            writer.write(CLEAR + HOME)
            try:
                await _drain_bounded(writer)
            except (ConnectionResetError, BrokenPipeError):
                return "DISCONNECT"
            target = loop.time()  # resync the frame clock after the wipe

        resync = False
        while not key_q.empty():
            try:
                key = key_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if key in ("QUIT", "DISCONNECT"):
                return key
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
            try:
                key = await asyncio.wait_for(key_q.get(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            if key in ("QUIT", "DISCONNECT"):
                return key
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
            skipped += 1
            i += 1
            target += frame_interval
            continue
        if delay > 0:
            await asyncio.sleep(delay)

        # Backpressure skip: if the client hasn't drained the previous
        # frames yet (slow WAN link, congested uplink, etc.) don't stack
        # another 50-80 KB on top — drop this frame instead. Lets the
        # skip-frame rate track real network speed instead of wall clock.
        if _write_buffer_size(writer) > WRITE_BUF_SKIP:
            skipped += 1
            i += 1
            target += frame_interval
            continue

        try:
            lines = read_frame(bucket, i)
            writer.write(render_frame_at(lines, top, left))
            await _drain_bounded(writer)
        except (ConnectionResetError, BrokenPipeError):
            return "DISCONNECT"
        except Exception as e:
            log.warning("%s frame %d read/write error: %s", peer, i, e)
            return "DISCONNECT"

        i += 1
        target += frame_interval

    log.info("%s playback complete (skipped=%d of %d)", peer, skipped, n_frames)
    try:
        await asyncio.sleep(DEFAULT_END_HOLD)
    except asyncio.CancelledError:
        pass
    return "DONE"


async def _drain_keys(key_q: asyncio.Queue) -> None:
    """Empty any buffered keystrokes so the next prompt starts fresh."""
    while not key_q.empty():
        try:
            key_q.get_nowait()
        except asyncio.QueueEmpty:
            break


async def shell(reader, writer) -> None:
    """Per-connection coroutine:
       welcome → play → goodbye (held until keypress or timeout)"""
    global _SESSION_COUNTER, _LIFETIME_COUNTER
    _SESSION_COUNTER += 1
    _LIFETIME_COUNTER += 1
    session = _SESSION_COUNTER
    lifetime = _LIFETIME_COUNTER
    if _COUNTER_PATH is not None:
        save_counter(_COUNTER_PATH, lifetime)

    # Turn on TCP keepalive so dead clients are detected in ~60s instead of
    # the kernel default (~2 hours on Linux).
    _enable_keepalive(writer)
    # Explicit write-buffer watermarks so the play loop's backpressure
    # check has a predictable threshold to compare against.
    _configure_write_buffer(writer)

    peer = writer.get_extra_info("peername", ("?", 0))
    log.info("connect %s session=%d lifetime=%d", peer, session, lifetime)

    fps = float(os.environ.get("SRTELNET_FPS", DEFAULT_FPS))

    key_q: asyncio.Queue = asyncio.Queue()
    reader_task = asyncio.create_task(key_reader(reader, key_q))
    loop = asyncio.get_event_loop()

    try:
        # Wait briefly for NAWS to arrive (up to 1s).
        deadline = loop.time() + 1.0
        while loop.time() < deadline:
            if writer.get_extra_info("cols") and writer.get_extra_info("rows"):
                break
            await asyncio.sleep(0.05)

        cols = writer.get_extra_info("cols") or 80
        rows = writer.get_extra_info("rows") or 25
        log.info("%s NAWS=%dx%d", peer, cols, rows)

        # --- terminal size sanity check (Option C) ---
        if cols < MIN_COLS or rows < MIN_ROWS:
            log.info("%s rejected: %dx%d below minimum %dx%d",
                     peer, cols, rows, MIN_COLS, MIN_ROWS)
            writer.write(render_too_small(cols, rows))
            await writer.drain()
            await asyncio.sleep(2.0)
            return

        writer.write(HIDE_CURSOR + CLEAR + HOME)
        await writer.drain()

        # --- welcome screen ---
        writer.write(render_welcome(cols, rows, session=session, lifetime=lifetime))
        await writer.drain()

        try:
            key = await asyncio.wait_for(key_q.get(), timeout=30.0)
            if key in ("QUIT", "DISCONNECT"):
                return
        except asyncio.TimeoutError:
            pass
        await _drain_keys(key_q)

        bucket = pick_bucket(cols, rows)

        # --- play once ---
        await _play_once(writer, key_q, peer, bucket, cols, rows, fps)

        # --- goodbye / BBS ad: held until keypress or timeout ---
        try:
            writer.write(render_goodbye(cols, rows, session=session, lifetime=lifetime))
            await writer.drain()
        except Exception:
            return
        await _drain_keys(key_q)
        try:
            await asyncio.wait_for(key_q.get(), timeout=DEFAULT_GOODBYE_HOLD)
        except asyncio.TimeoutError:
            pass

    finally:
        reader_task.cancel()
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
    ap.add_argument("--counter-file", type=Path, default=DEFAULT_COUNTER_FILE,
                    help=f"persistent lifetime connection counter file "
                         f"(default {DEFAULT_COUNTER_FILE}, overridable via "
                         f"$SRTELNET_COUNTER_FILE)")
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

    # Load the persistent lifetime counter so the welcome screen can show
    # a real "connection #N ever" tally across restarts.
    global _COUNTER_PATH, _LIFETIME_COUNTER
    _COUNTER_PATH = args.counter_file
    _LIFETIME_COUNTER = load_counter(_COUNTER_PATH)
    log.info("lifetime counter: %d (from %s)", _LIFETIME_COUNTER, _COUNTER_PATH)

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
