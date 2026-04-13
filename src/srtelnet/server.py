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
import re
import socket
import sys
import time
from pathlib import Path

import telnetlib3

# ---------------------------------------------------------------- constants
DEFAULT_PORT = 2323
DEFAULT_FPS = 30.0
DEFAULT_FRAMES_ROOT = Path(os.environ.get("SRTELNET_FRAMES", "frames-30fps"))
BUCKETS_ORDER = (40, 60, 80, 100, 120, 140, 160, 180, 200)
# Trim the trailing "GRAPHICS/MUSIC/CODE" credit card off the short bake.
# Stored in SECONDS (not frames) so the same default works at any fps —
# multiplied by --fps at startup. ffmpeg blackdetect puts the last black
# segment end at 508.274s, but in practice the credit text starts fading
# in a few frames earlier, so back off a hair (~0.13s) to land solidly
# inside the final black stretch.
DEFAULT_MAX_SECONDS = 508.13
# Frame ranges to skip entirely during playback, in SECONDS. Each
# (start, end) cuts that wall-clock range out of the bake. Default cuts
# the 11s of the 15.2s static landscape section at 42.76s-57.97s
# (ffmpeg freezedetect), centered so ~2.1s of stillness remains on each
# side of the cut. The demo plays quiet music-only pauses during this
# stretch which we can't reproduce over telnet, so skipping most of it
# keeps the viewer engaged without an abrupt splice.
#
# These seconds are multiplied by --fps at startup to produce the
# actual frame-index ranges applied during bucket indexing, so the same
# defaults give the correct visual edit at 20 fps (897:1117),
# 30 fps (1346:1676), or any other rate.
DEFAULT_SKIP_SECONDS: list[tuple[float, float]] = [(44.87, 55.87)]
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
#
# These watermarks also double as the input-to-display latency ceiling
# for WAN clients. With a 100–300 KB/s client, a 128 KB SKIP ceiling
# caps keystroke→render lag at ~0.4–1.3s (vs. ~2–5s with the old 512 KB
# ceiling), at the cost of skipping frames slightly earlier when the
# client can't keep up. See docs/performance-tuning.md for rationale.
WRITE_BUF_HIGH = 256 * 1024    # 256 KB: transport pause_writing above this
WRITE_BUF_LOW = 48 * 1024      # 48 KB:  drain returns / seek-drain target
WRITE_BUF_SKIP = 128 * 1024    # 128 KB: skip next frame above this
# If a client is so slow that we skip this many consecutive frames, they
# are effectively dead (or on a connection that can't sustain even a
# trickle). Disconnect gracefully instead of burning server resources
# writing into a void.
MAX_CONSEC_SKIP = 300           # ~10s at 30fps
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


# Maximum number of buckets to keep fully cached in Python memory at once.
# Each cached bucket is ~1-5 GB depending on width (Python string overhead
# makes in-memory size ~1.5-2x the on-disk .ans file size). With 8 GB RAM,
# 4 cached buckets covers the common terminal widths (80, 100, 120, 160)
# with room for the OS, Python interpreter, and Linux page cache.
MAX_CACHED_BUCKETS = int(os.environ.get("SRTELNET_MAX_CACHED", "4"))
# How long a bucket can go unused before its Python cache is evicted.
# The on-disk frames remain; re-caching is transparent (just slower for
# the first client to hit that width again). 600s = 10 minutes.
CACHE_IDLE_SECONDS = float(os.environ.get("SRTELNET_CACHE_IDLE", "600"))


# ---------------------------------------------------------------- bucket data
class Bucket:
    """One width bucket, indexed on startup. Frames are lazily parsed and
    cached on first read — after one play-through, subsequent reads (from
    this or any other connection) return the same in-memory tuple of rows
    without touching the filesystem or re-parsing UTF-8.

    Cache eviction: when MAX_CACHED_BUCKETS is exceeded or a bucket goes
    idle for CACHE_IDLE_SECONDS, its cache is cleared (all entries set back
    to None). The next read re-parses from disk — the Linux page cache
    still holds the hot files, so the I/O cost is just the Python string
    allocation."""
    __slots__ = ("width", "height", "paths", "cache",
                 "access_count", "active_clients", "last_access")

    def __init__(self, width: int, height: int, paths: list[Path]):
        self.width = width
        self.height = height
        self.paths = paths
        self.cache: list[tuple[str, ...] | None] = [None] * len(paths)
        self.access_count: int = 0       # total connections that used this bucket
        self.active_clients: int = 0     # currently-playing connections
        self.last_access: float = 0.0    # monotonic time of last frame read

    def cached_count(self) -> int:
        """How many frames are currently cached (non-None)."""
        return sum(1 for c in self.cache if c is not None)

    def clear_cache(self) -> None:
        """Evict all cached frames, freeing Python string memory. The paths
        list is untouched so re-caching is transparent."""
        for i in range(len(self.cache)):
            self.cache[i] = None
        log.info("bucket %d cache evicted (%d frames freed)",
                 self.width, len(self.cache))


BUCKETS: dict[int, Bucket] = {}
MAX_FRAMES: int | None = None  # set at startup from CLI
SKIPS: list[tuple[int, int]] = []  # set at startup from CLI

# Runtime stats for the welcome screen. Populated at startup from the
# live configuration (fps CLI, indexed buckets) so the welcome display
# always matches what is actually being served — no code edit needed
# when fps or frame counts change. See render_welcome().
_STATS_FPS: float = 30.0
_STATS_FRAMES: int = 0


# Chafa emits DECTCEM cursor-hide at the top of each rendered frame and
# DECTCEM cursor-show at the bottom. When we pump frames at 20-30 fps
# with TCP_NODELAY enabled (no Nagle coalescing) over a WAN link, the
# few-millisecond gap between a frame's trailing "show" and the next
# frame's leading "hide" is long enough for the client terminal to
# render the cursor, producing a visible rapid flash. We strip both
# sequences at parse time so the cached line tuple contains no cursor
# toggles — the shell itself emits HIDE_CURSOR once at connect and that
# stays in effect throughout playback.
_CHAFA_CURSOR_TOGGLE_RE = re.compile(r"\x1b\[\?25[lh]")


def _parse_frame_lines(raw: str) -> list[str]:
    """Split a chafa .ans file into per-row strings, dropping the trailing
    empty line produced by the final newline. Also strips chafa's
    per-frame cursor show/hide toggles (see _CHAFA_CURSOR_TOGGLE_RE)."""
    raw = _CHAFA_CURSOR_TOGGLE_RE.sub("", raw)
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


def smaller_bucket(current: Bucket) -> Bucket | None:
    """Return the next-smaller loaded bucket, or None if `current` is already
    the smallest one available. Used by _play_once to downgrade a client
    whose uplink can't sustain the current bucket's byte rate — a smaller
    bucket means fewer bytes per frame and fewer skips per second."""
    candidates = [b for b in BUCKETS.values() if b.width < current.width]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.width)


def larger_bucket(current: Bucket, cap: Bucket) -> Bucket | None:
    """Return the next-larger loaded bucket, capped at `cap.width` (exclusive
    on the cap side — we don't go PAST the NAWS-determined bucket), or None
    if already at or above the cap. Used by _play_once to upgrade a client
    back toward their natural window-fit bucket after a streak of
    successful frame writes — a momentary network blip that triggered a
    downgrade shouldn't pin them at a smaller bucket forever."""
    candidates = [b for b in BUCKETS.values()
                  if current.width < b.width <= cap.width]
    if not candidates:
        return None
    return min(candidates, key=lambda b: b.width)


def read_frame(bucket: Bucket, index: int) -> tuple[str, ...]:
    """Return the row tuple for frame `index`. Parses and caches on first
    hit, returns the cached tuple on every subsequent call. All concurrent
    connections playing the same bucket share the same immutable tuples,
    so memory cost is per-bucket, not per-connection."""
    bucket.last_access = time.monotonic()
    cached = bucket.cache[index]
    if cached is not None:
        return cached
    raw = bucket.paths[index].read_text(encoding="utf-8", errors="replace")
    lines = tuple(_parse_frame_lines(raw))
    bucket.cache[index] = lines
    return lines


def evict_idle_caches() -> None:
    """Free Python-level frame caches for buckets that have no active clients
    and haven't been touched recently, OR when more than MAX_CACHED_BUCKETS
    are populated. Called on every client disconnect.

    Eviction order: idle buckets first (oldest access), then least-recently-
    used buckets if we're still over the cap. Buckets with active_clients > 0
    are never evicted (that would just force immediate re-parsing)."""
    now = time.monotonic()
    cached_buckets = [b for b in BUCKETS.values() if b.cached_count() > 0]

    # Phase 1: evict any bucket that's idle AND has no active clients.
    for b in cached_buckets:
        if b.active_clients == 0 and (now - b.last_access) > CACHE_IDLE_SECONDS:
            b.clear_cache()

    # Phase 2: if still over the cap, evict LRU (no active clients first).
    cached_buckets = [b for b in BUCKETS.values() if b.cached_count() > 0]
    if len(cached_buckets) > MAX_CACHED_BUCKETS:
        # Sort: idle (no active clients) first, then by oldest last_access.
        evictable = sorted(
            [b for b in cached_buckets if b.active_clients == 0],
            key=lambda b: b.last_access,
        )
        while len(cached_buckets) > MAX_CACHED_BUCKETS and evictable:
            victim = evictable.pop(0)
            victim.clear_cache()
            cached_buckets = [b for b in BUCKETS.values() if b.cached_count() > 0]


def prewarm_bucket(bucket: Bucket) -> None:
    """Eagerly cache every frame in a bucket so the first client gets smooth
    playback with no per-frame disk reads. Called at startup for the most
    common terminal width."""
    log.info("pre-warming bucket %d (%d frames)...", bucket.width, len(bucket.paths))
    t0 = time.monotonic()
    for i in range(len(bucket.paths)):
        read_frame(bucket, i)
    elapsed = time.monotonic() - t0
    log.info("bucket %d pre-warmed in %.1fs", bucket.width, elapsed)


# ---------------------------------------------------------------- rendering
def render_frame_at(lines: tuple[str, ...] | list[str], top: int, left: int) -> str:
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


# Text of the "press any key" prompt on the welcome screen. Defined as
# a module constant so render_welcome() can emit it AND shell() can
# paint over it with a flashing overlay (many modern terminals ignore
# the \x1b[5m blink SGR, so we drive the flash server-side instead).
_JACK_IN_TEXT = ">>>   PRESS ANY KEY TO JACK IN   <<<"


def render_welcome(
    cols: int, rows: int, session: int = 0, lifetime: int = 0
) -> tuple[str, int, int]:
    """Render a BBS-style truecolor ANSI welcome screen. Designed to fit
    exactly on an 80x25 terminal — taller terminals just get extra top/
    bottom margin via the centering math.

    Returns (welcome_str, jack_row, jack_col) — the jack_row and jack_col
    are 1-indexed terminal coordinates of the "PRESS ANY KEY TO JACK IN"
    line so the caller can drive a flash animation over it without
    re-rendering the whole screen.

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

    # --- stats (1 row) with session + lifetime counters when available.
    # Frame count and fps are read from the live runtime config so the
    # welcome always matches what is actually being served — no code edit
    # needed when the fps or bake changes.
    fps_str = f"{_STATS_FPS:g} fps"       # 20 -> "20 fps", 24.0 -> "24 fps"
    frames_str = f"{_STATS_FRAMES:,} frames" if _STATS_FRAMES else "frames"
    parts = [frames_str, fps_str, "truecolor ANSI"]
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

    # --- signoff + source + prompt (3 rows)
    add("streamed by paulie420  \u00b7  20forbeers.com", _C_GREEN)
    add("source:  github.com/Paulie420/second-reality-TELNET", _C_DIM)
    add(_JACK_IN_TEXT, _BOLD + _BLINK + _C_PINK)
    # Total: 8 (banner) + 3 (subtitle) + 1 (stats) + 1 (credits) + 1 (gap)
    # + 5 (controls) + 3 (signoff+source+prompt) = 22 rows. Centered in 25
    # gives 1-row top + 2-row bottom margin.

    n = len(raw)
    top = max(0, (rows - n) // 2)
    out = [HOME, CLEAR]
    jack_row = 0
    jack_col = 0
    for i, (r_line, c_line) in enumerate(zip(raw, col)):
        pad = max(0, (cols - len(r_line)) // 2)
        out.append(f"\x1b[{top + i + 1};1H{' ' * pad}{c_line}")
        if r_line == _JACK_IN_TEXT:
            jack_row = top + i + 1
            jack_col = pad + 1
    out.append(RESET)
    return "".join(out), jack_row, jack_col


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

    # --- BBS ad block (8 rows)
    add("WEBSITE :  20ForBeers.com", _C_WHITE)
    add("GITHUB  :  github.com/Paulie420/second-reality-TELNET", _C_DIM)
    add("An ANSi TELNET BBS:", _C_DIM)
    add("TELNET  :  20ForBeers.com:1337", _C_CYAN)
    add("SSH     :  20ForBeers.com:1338", _C_CYAN)
    add("")
    add("'Dial-in' Today!", _C_GREEN + _BOLD)
    # Classic modem drop — one last BBS callback on the way out
    add("NO CARRIER", _BOLD + _C_PINK)
    # Total: 8 (banner) + 3 (subtitle) + 1 (stats) + 5 (addresses) +
    # 1 (gap) + 1 ('Dial-in') + 1 (NO CARRIER) = 20 rows. Plus top/
    # bottom margin auto-centered by the rows - n math below.

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

# Active (concurrent) connection count — incremented on connect, decremented
# on disconnect. Written to _STATUS_PATH so admins can `cat` it any time.
_ACTIVE_CONNECTIONS = 0
_PEAK_CONNECTIONS = 0       # high-water mark since process start

# Lifetime connection counter — total connections ever served, persisted to
# disk so restarts don't lose the count. Loaded at startup from _COUNTER_PATH
# and rewritten on every new connection. Best-effort: if the file is missing
# or unwritable we log a warning and keep counting in memory.
_LIFETIME_COUNTER = 0
_COUNTER_PATH: Path | None = None

# Path for the live status file, written on every connect/disconnect.
DEFAULT_STATUS_FILE = Path(
    os.environ.get("SRTELNET_STATUS_FILE", "state/status.txt")
)
_STATUS_PATH: Path | None = None

# Connection log: one CSV line per connection for post-hoc analytics.
DEFAULT_CONNLOG_FILE = Path(
    os.environ.get("SRTELNET_CONNLOG_FILE", "state/connections.csv")
)
_CONNLOG_PATH: Path | None = None


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


def _write_status() -> None:
    """Write a human-readable status file with live connection stats. Called
    on every connect and disconnect so `cat state/status.txt` always shows
    current state. Best-effort — never fatal."""
    if _STATUS_PATH is None:
        return
    try:
        now = time.monotonic()
        uptime_s = now - _SERVER_START_TIME
        h, rem = divmod(int(uptime_s), 3600)
        m, s = divmod(rem, 60)
        # Bucket details: cache state, popularity, active clients.
        bucket_lines = []
        for w in BUCKETS_ORDER:
            b = BUCKETS.get(w)
            if b is None:
                continue
            n_cached = b.cached_count()
            total = len(b.cache)
            pct = (n_cached * 100 // total) if total else 0
            idle = ""
            if n_cached > 0 and b.active_clients == 0 and b.last_access > 0:
                idle_s = now - b.last_access
                idle = f"  idle {int(idle_s)}s"
            bucket_lines.append(
                f"  {w:>3}w: {n_cached:>5}/{total} cached ({pct:>3}%)"
                f"  hits={b.access_count:<4} active={b.active_clients}{idle}"
            )
        lines = [
            f"second-reality-TELNET status",
            f"============================",
            f"active connections : {_ACTIVE_CONNECTIONS}",
            f"peak connections   : {_PEAK_CONNECTIONS}",
            f"session total      : {_SESSION_COUNTER}",
            f"lifetime total     : {_LIFETIME_COUNTER}",
            f"uptime             : {h}h {m}m {s}s",
            f"cache policy       : max {MAX_CACHED_BUCKETS} buckets, "
            f"evict after {int(CACHE_IDLE_SECONDS)}s idle",
            f"",
            f"buckets:",
            *(bucket_lines if bucket_lines else ["  (none loaded)"]),
            f"",
            f"updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        _STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS_PATH.with_suffix(_STATUS_PATH.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(_STATUS_PATH)
    except OSError as e:
        log.debug("status file write failed: %s", e)


_SERVER_START_TIME = time.monotonic()


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


def _configure_socket(writer) -> None:
    """Configure TCP socket options on the accepted connection:
      - SO_KEEPALIVE + Linux keepalive knobs so dead peers are detected in
        ~60s instead of the OS default (~2 hours on Linux).
      - TCP_NODELAY (disable Nagle) so per-frame writes hit the wire
        immediately. At 30 fps our writes are frequent enough that Nagle's
        coalescing adds up to ~200 ms of latency for no benefit — we're
        already batching at the application layer.
      - TCP_QUICKACK (Linux) so inbound client bytes (keystrokes) are
        ACK'd immediately instead of waiting up to 40ms for a piggyback.
        Note Linux resets QUICKACK after each idle period; our 30fps
        steady-state keeps the socket busy enough that it's effectively
        always on for the input path that matters (seek / pause / quit).
    Best-effort: silently no-ops on platforms / transports that don't
    expose a socket."""
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
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if hasattr(socket, "TCP_QUICKACK"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
    except OSError as e:
        log.debug("socket setsockopt failed: %s", e)


async def _play_once(writer, key_q, peer, bucket, cols, rows, fps) -> tuple[str, int, int, int, int, int]:
    """Play the demo once through and return
    (status, frames_played, skipped, peak_buffer_kb, seek_count,
     final_bucket_width).

    Status is 'QUIT', 'DISCONNECT', or 'DONE'. The last four fields are
    per-session telemetry written to the CSV in _log_connection() for
    post-hoc analytics and to measure whether WAN-side optimizations
    are actually helping."""
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
        return ("DISCONNECT", 0, 0, 0, 0, bucket.width)

    i = 0
    paused = False
    target = loop.time()
    skipped = 0
    consec_skip = 0
    # After a seek we force the very next frame to be written regardless of
    # the backpressure skip, so the target frame is guaranteed to land even
    # on a slow link. Reset to False after the post-seek frame goes out.
    force_next_frame = False
    # Per-session telemetry. Written to state/connections.csv in
    # _log_connection() on disconnect. peak_buffer_kb tracks the high-water
    # mark of the transport send-buffer occupancy (proxy for "how backed up
    # did this client get"). seek_count counts LEFT/RIGHT keystrokes.
    peak_buffer_kb = 0
    seek_count = 0
    # --- Adaptive bucket selection (per-client downgrade / upgrade) ---
    # `naws_bucket` is the natural bucket size for the client's window,
    # updated on every NAWS resize. It's the CAP — we never serve a
    # larger bucket than this, but we can serve smaller ones if the
    # client's link can't sustain the natural size.
    # After DOWNGRADE_SECONDS of consecutive backpressure-skipped frames
    # we step DOWN to the next-smaller loaded bucket. After
    # UPGRADE_SECONDS of consecutive clean writes we step back UP
    # toward naws_bucket. Asymmetric thresholds (fast-downgrade,
    # slow-upgrade) prevent thrashing: momentary congestion doesn't
    # permanently pin a client at a smaller bucket, but a client who
    # can't sustain the big bucket doesn't get upgraded back into
    # failure every few seconds.
    DOWNGRADE_SECONDS = 2.0
    UPGRADE_SECONDS = 10.0
    downgrade_threshold = max(1, int(DOWNGRADE_SECONDS * fps))
    upgrade_threshold = max(1, int(UPGRADE_SECONDS * fps))
    naws_bucket = bucket
    clean_streak = 0

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
            # NAWS resize re-establishes the adaptive cap. User wants to
            # see what fits their new window, so start fresh at the
            # naws-picked bucket and let the downgrade logic kick back in
            # from scratch if the link still can't sustain it.
            naws_bucket = new_bucket
            clean_streak = 0
            consec_skip = 0
            top = max(0, (rows - bucket.height) // 2)
            left = max(0, (cols - bucket.width) // 2)
            # Old frame leaves stale cells around the edges of the new
            # bucket. Wipe the screen so the next render is clean.
            writer.write(CLEAR + HOME)
            try:
                await _drain_bounded(writer)
            except (ConnectionResetError, BrokenPipeError):
                return ("DISCONNECT", i, skipped, peak_buffer_kb, seek_count, bucket.width)
            target = loop.time()  # resync the frame clock after the wipe

        resync = False
        seeked = False
        while not key_q.empty():
            try:
                key = key_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if key in ("QUIT", "DISCONNECT"):
                return (key, i, skipped, peak_buffer_kb, seek_count, bucket.width)
            if key == "SPACE":
                paused = not paused
                resync = True
            elif key == "LEFT":
                i = max(0, i - seek_frames)
                resync = True
                seeked = True
                seek_count += 1
            elif key == "RIGHT":
                i = min(n_frames - 1, i + seek_frames)
                resync = True
                seeked = True
                seek_count += 1

        # On seek (LEFT/RIGHT): the TCP send buffer may already hold a
        # backlog of pre-seek frames that will paint on the client
        # BEFORE the new target frame arrives. On a WAN link that's
        # seconds of "nothing happened." Three-part fix:
        #   (a) write CLEAR+HOME immediately so the user gets instant
        #       visual acknowledgment that seek registered — the terminal
        #       clears, and whatever was queued paints onto a blank
        #       canvas,
        #   (b) wait (bounded) for the write buffer to drain below
        #       WRITE_BUF_LOW so the backlog flushes before we commit to
        #       the new position,
        #   (c) bypass the backpressure skip for the first post-seek
        #       frame, so even if the buffer is still over the skip
        #       threshold, the target frame is guaranteed to land.
        # Net effect: seeks feel like a real seek (instant clear, brief
        # pause, new position appears) instead of a several-second stall.
        if seeked:
            try:
                writer.write(CLEAR + HOME)
            except (ConnectionResetError, BrokenPipeError):
                return ("DISCONNECT", i, skipped, peak_buffer_kb, seek_count, bucket.width)
            seek_deadline = loop.time() + 1.5
            while (_write_buffer_size(writer) > WRITE_BUF_LOW
                   and loop.time() < seek_deadline):
                await asyncio.sleep(0.05)
            force_next_frame = True

        if paused:
            try:
                key = await asyncio.wait_for(key_q.get(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            if key in ("QUIT", "DISCONNECT"):
                return (key, i, skipped, peak_buffer_kb, seek_count, bucket.width)
            if key == "SPACE":
                paused = False
                target = loop.time()
            elif key == "LEFT":
                i = max(0, i - seek_frames)
                seek_count += 1
            elif key == "RIGHT":
                i = min(n_frames - 1, i + seek_frames)
                seek_count += 1
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
        # Exception: the first frame after a seek bypasses the skip so
        # the target frame is guaranteed to land — otherwise on a slow
        # link the seek would drop its own target frame.
        buf = _write_buffer_size(writer)
        buf_kb = buf // 1024
        if buf_kb > peak_buffer_kb:
            peak_buffer_kb = buf_kb
        if (not force_next_frame and buf > WRITE_BUF_SKIP):
            skipped += 1
            consec_skip += 1
            clean_streak = 0
            if consec_skip > MAX_CONSEC_SKIP:
                log.info("%s %d consecutive skips, giving up", peer, consec_skip)
                return ("DISCONNECT", i, skipped, peak_buffer_kb, seek_count, bucket.width)
            # Adaptive downgrade: sustained backpressure means the client's
            # uplink can't keep up with this bucket's byte rate. Step down
            # to the next-smaller bucket — same frame count, fewer bytes
            # per frame, so the buffer has a chance to drain and the skip
            # cycle breaks. Repeatable; can trigger again on the smaller
            # bucket if STILL too much.
            if consec_skip >= downgrade_threshold:
                new_bucket = smaller_bucket(bucket)
                if new_bucket is not None:
                    log.info(
                        "%s adaptive downgrade: bucket %d -> %d "
                        "(consec_skip=%d, naws_cap=%d)",
                        peer, bucket.width, new_bucket.width,
                        consec_skip, naws_bucket.width,
                    )
                    bucket = new_bucket
                    n_frames = len(bucket.paths)
                    if i >= n_frames:
                        i = n_frames - 1
                    top = max(0, (rows - bucket.height) // 2)
                    left = max(0, (cols - bucket.width) // 2)
                    # Wipe — the smaller bucket leaves stale cells at the
                    # edges. Same rationale as NAWS resize.
                    writer.write(CLEAR + HOME)
                    # Wait briefly for the backlog of the larger bucket
                    # to drain before we start stuffing smaller-bucket
                    # frames behind it — otherwise the new frames stack
                    # on the old ones and we'd just downgrade again at
                    # the next threshold. Same pattern as drain-on-seek.
                    drain_deadline = loop.time() + 1.5
                    while (_write_buffer_size(writer) > WRITE_BUF_LOW
                           and loop.time() < drain_deadline):
                        await asyncio.sleep(0.05)
                    consec_skip = 0
                    clean_streak = 0
                    # Guarantee the first frame from the new bucket lands
                    # even if the drain wait didn't fully empty the buffer.
                    force_next_frame = True
                    target = loop.time()  # resync clock to new bucket
            i += 1
            target += frame_interval
            continue

        consec_skip = 0  # client is keeping up, reset the dead-client counter
        force_next_frame = False  # consumed; revert to normal skip behavior
        clean_streak += 1
        # Adaptive upgrade: after sustained clean playback, try stepping
        # back toward the NAWS-determined natural bucket. A transient
        # congestion burst that triggered a downgrade shouldn't pin the
        # client at a smaller image for the rest of the demo. Upgrade
        # threshold is deliberately higher than downgrade (10s vs 2s) to
        # prevent thrashing.
        if (clean_streak >= upgrade_threshold
                and bucket.width < naws_bucket.width):
            new_bucket = larger_bucket(bucket, naws_bucket)
            if new_bucket is not None:
                log.info(
                    "%s adaptive upgrade: bucket %d -> %d "
                    "(clean_streak=%d, naws_cap=%d)",
                    peer, bucket.width, new_bucket.width,
                    clean_streak, naws_bucket.width,
                )
                bucket = new_bucket
                n_frames = len(bucket.paths)
                if i >= n_frames:
                    i = n_frames - 1
                top = max(0, (rows - bucket.height) // 2)
                left = max(0, (cols - bucket.width) // 2)
                writer.write(CLEAR + HOME)
                clean_streak = 0
                target = loop.time()

        try:
            lines = read_frame(bucket, i)
            writer.write(render_frame_at(lines, top, left))
            try:
                await _drain_bounded(writer)
            except ConnectionResetError:
                # drain timeout — client is very slow but maybe not dead.
                # Don't disconnect; let the buffer-size skip at the top of
                # the loop handle flow control. If the client is truly gone,
                # consecutive skips will eventually trip MAX_CONSEC_SKIP.
                log.debug("%s drain timeout (buf=%d), skipping drain",
                          peer, _write_buffer_size(writer))
        except (ConnectionResetError, BrokenPipeError):
            return ("DISCONNECT", i, skipped, peak_buffer_kb, seek_count, bucket.width)
        except Exception as e:
            log.warning("%s frame %d read/write error: %s", peer, i, e)
            return ("DISCONNECT", i, skipped, peak_buffer_kb, seek_count, bucket.width)

        i += 1
        target += frame_interval

    log.info("%s playback complete (skipped=%d of %d)", peer, skipped, n_frames)
    try:
        await asyncio.sleep(DEFAULT_END_HOLD)
    except asyncio.CancelledError:
        pass
    return ("DONE", n_frames, skipped, peak_buffer_kb, seek_count, bucket.width)


async def _drain_keys(key_q: asyncio.Queue) -> None:
    """Empty any buffered keystrokes so the next prompt starts fresh."""
    while not key_q.empty():
        try:
            key_q.get_nowait()
        except asyncio.QueueEmpty:
            break


def _log_connection(peer, bucket_width: int, frames_played: int,
                    total_frames: int, skipped: int,
                    duration: float, outcome: str,
                    peak_buffer_kb: int = 0, seek_count: int = 0,
                    final_bucket_width: int = 0) -> None:
    """Append one line to the connection log CSV. Best-effort — if the file
    can't be written we just skip it. The CSV is meant for post-hoc analytics:
    which buckets are popular, how many viewers watch the whole thing, where
    in the world are they connecting from, and (via the telemetry fields)
    whether WAN-side optimizations are actually helping.

    Columns:
      timestamp,ip,bucket_width,frames_played,total_frames,skipped,
      duration_s,outcome,peak_buffer_kb,seek_count,final_bucket_width

    peak_buffer_kb   high-water mark of the transport send-buffer
                     occupancy during playback (proxy for "how backed
                     up did this client get"). 0 = fast client /
                     no backpressure observed.
    seek_count       number of LEFT/RIGHT keystrokes during playback
                     (proxy for user engagement / whether they found
                     the seek UX usable).
    final_bucket_width
                     the bucket width the session ENDED on. Equal to
                     bucket_width (the starting bucket) unless the
                     server auto-downgraded mid-stream due to sustained
                     backpressure."""
    if _CONNLOG_PATH is None:
        return
    try:
        _CONNLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ip = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
        line = (
            f"{time.strftime('%Y-%m-%d %H:%M:%S')},"
            f"{ip},{bucket_width},{frames_played},{total_frames},"
            f"{skipped},{duration:.1f},{outcome},"
            f"{peak_buffer_kb},{seek_count},{final_bucket_width}\n"
        )
        with open(_CONNLOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


async def shell(reader, writer) -> None:
    """Per-connection coroutine:
       welcome → play → goodbye (held until keypress or timeout)"""
    global _SESSION_COUNTER, _LIFETIME_COUNTER, _ACTIVE_CONNECTIONS, _PEAK_CONNECTIONS
    _SESSION_COUNTER += 1
    _LIFETIME_COUNTER += 1
    _ACTIVE_CONNECTIONS += 1
    if _ACTIVE_CONNECTIONS > _PEAK_CONNECTIONS:
        _PEAK_CONNECTIONS = _ACTIVE_CONNECTIONS
    session = _SESSION_COUNTER
    lifetime = _LIFETIME_COUNTER
    if _COUNTER_PATH is not None:
        save_counter(_COUNTER_PATH, lifetime)
    _write_status()

    # Configure the TCP socket: keepalive (for dead-peer detection in
    # ~60s) and TCP_NODELAY (to kill Nagle's coalescing delay on the
    # per-frame writes we do at 30 fps).
    _configure_socket(writer)
    # Explicit write-buffer watermarks so the play loop's backpressure
    # check has a predictable threshold to compare against.
    _configure_write_buffer(writer)

    peer = writer.get_extra_info("peername", ("?", 0))
    log.info("connect %s session=%d lifetime=%d active=%d",
             peer, session, lifetime, _ACTIVE_CONNECTIONS)

    fps = float(os.environ.get("SRTELNET_FPS", DEFAULT_FPS))
    t_connect = time.monotonic()
    bucket: Bucket | None = None
    outcome = "disconnect"
    frames_played = 0
    play_skipped = 0
    peak_buffer_kb = 0
    seek_count = 0
    final_bucket_width = 0

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
            outcome = "too_small"
            return

        writer.write(HIDE_CURSOR + CLEAR + HOME)
        await writer.drain()

        # --- welcome screen ---
        welcome_str, jack_row, jack_col = render_welcome(
            cols, rows, session=session, lifetime=lifetime)
        writer.write(welcome_str)
        await writer.drain()

        # Flash the "PRESS ANY KEY TO JACK IN" line server-side by
        # repainting it every 500ms alternating between bright pink and
        # dimmed-out. This survives terminals that ignore \x1b[5m (most
        # modern terminals do — Alacritty, Kitty, Windows Terminal, etc.)
        # while still giving blink-honoring terminals the SGR attribute
        # for good measure. Exits on first keypress or 30s timeout.
        flash_on = True
        welcome_deadline = loop.time() + 30.0
        while loop.time() < welcome_deadline:
            style = (_BOLD + _BLINK + _C_PINK) if flash_on else _C_DIM
            try:
                writer.write(
                    f"\x1b[{jack_row};{jack_col}H{style}{_JACK_IN_TEXT}{RESET}"
                )
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return
            try:
                key = await asyncio.wait_for(key_q.get(), timeout=0.5)
                if key in ("QUIT", "DISCONNECT"):
                    outcome = "quit_welcome"
                    return
                break  # any key -> start playback
            except asyncio.TimeoutError:
                flash_on = not flash_on
        await _drain_keys(key_q)

        bucket = pick_bucket(cols, rows)
        bucket.access_count += 1
        bucket.active_clients += 1

        # --- play once ---
        (result, frames_played, play_skipped, peak_buffer_kb,
         seek_count, final_bucket_width) = await _play_once(
            writer, key_q, peer, bucket, cols, rows, fps)
        outcome = result.lower()

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
        if bucket is not None:
            bucket.active_clients = max(0, bucket.active_clients - 1)
        duration = time.monotonic() - t_connect
        _ACTIVE_CONNECTIONS -= 1
        _write_status()
        evict_idle_caches()
        _log_connection(
            peer,
            bucket_width=bucket.width if bucket else 0,
            frames_played=frames_played,
            total_frames=len(bucket.paths) if bucket else 0,
            skipped=play_skipped,
            duration=duration,
            outcome=outcome,
            peak_buffer_kb=peak_buffer_kb,
            seek_count=seek_count,
            final_bucket_width=(final_bucket_width
                                if final_bucket_width
                                else (bucket.width if bucket else 0)),
        )
        reader_task.cancel()
        try:
            writer.close()
        except Exception:
            pass
        log.info("disconnect %s active=%d duration=%.0fs outcome=%s",
                 peer, _ACTIVE_CONNECTIONS, duration, outcome)


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
    ap.add_argument("--max-frames", type=int, default=None,
                    help="stop playback at frame N (default: auto-derived "
                         f"from --fps — wall-clock "
                         f"{DEFAULT_MAX_SECONDS:.2f}s trims the trailing "
                         "credit card off the short bake. Set 0 to play every "
                         "baked frame.)")
    ap.add_argument("--skip", action="append", default=None,
                    metavar="START:END",
                    help="drop frames [START, END) from playback (can be "
                         "repeated). Default auto-derives from --fps — "
                         "cuts the 11s still landscape section at 44.87s-"
                         "55.87s. Pass '--skip none' to disable all skips.")
    ap.add_argument("--counter-file", type=Path, default=DEFAULT_COUNTER_FILE,
                    help=f"persistent lifetime connection counter file "
                         f"(default {DEFAULT_COUNTER_FILE}, overridable via "
                         f"$SRTELNET_COUNTER_FILE)")
    ap.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE,
                    help=f"live status file path (default {DEFAULT_STATUS_FILE}, "
                         f"overridable via $SRTELNET_STATUS_FILE)")
    ap.add_argument("--connlog-file", type=Path, default=DEFAULT_CONNLOG_FILE,
                    help=f"connection log CSV path (default {DEFAULT_CONNLOG_FILE}, "
                         f"overridable via $SRTELNET_CONNLOG_FILE)")
    ap.add_argument("--prewarm", type=int, default=80, metavar="WIDTH",
                    help="pre-cache this bucket width at startup for instant "
                         "first-client playback (default 80, set 0 to disable)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="debug-level logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    global MAX_FRAMES, SKIPS
    # --max-frames: None (default) => auto-derive from fps. 0 => disable.
    # positive integer => use as-is.
    if args.max_frames is None:
        MAX_FRAMES = round(DEFAULT_MAX_SECONDS * args.fps)
        log.info("max-frames auto-derived from fps: %d (%.2fs @ %g fps)",
                 MAX_FRAMES, DEFAULT_MAX_SECONDS, args.fps)
    elif args.max_frames > 0:
        MAX_FRAMES = args.max_frames
    else:
        MAX_FRAMES = None

    # --skip: None (default) => auto-derive DEFAULT_SKIP_SECONDS from fps.
    # Explicit START:END values are taken as literal frame indices.
    # --skip none => no skips.
    if args.skip is None:
        SKIPS = [(round(s * args.fps), round(e * args.fps))
                 for s, e in DEFAULT_SKIP_SECONDS]
        if SKIPS:
            log.info("skip ranges auto-derived from fps: %s", SKIPS)
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

    # Make the live fps available to the welcome-screen renderer so its
    # stats line always reflects what we are actually serving.
    global _STATS_FPS
    _STATS_FPS = args.fps

    # Load the persistent lifetime counter so the welcome screen can show
    # a real "connection #N ever" tally across restarts.
    global _COUNTER_PATH, _LIFETIME_COUNTER, _STATUS_PATH
    _COUNTER_PATH = args.counter_file
    _LIFETIME_COUNTER = load_counter(_COUNTER_PATH)
    log.info("lifetime counter: %d (from %s)", _LIFETIME_COUNTER, _COUNTER_PATH)

    _STATUS_PATH = args.status_file
    _write_status()  # write initial status on startup
    log.info("status file: %s", _STATUS_PATH)

    global _CONNLOG_PATH
    _CONNLOG_PATH = args.connlog_file
    # Write CSV header if the file doesn't exist yet. Existing CSVs from
    # pre-instrumentation builds use the 8-column schema and are still
    # readable by the stats scripts; new rows add peak_buffer_kb,
    # seek_count, and final_bucket_width as columns 9-11.
    if _CONNLOG_PATH and not _CONNLOG_PATH.exists():
        try:
            _CONNLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CONNLOG_PATH.write_text(
                "timestamp,ip,bucket_width,frames_played,total_frames,"
                "skipped,duration_s,outcome,peak_buffer_kb,seek_count,"
                "final_bucket_width\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    log.info("connection log: %s", _CONNLOG_PATH)

    load_all_buckets(args.frames)

    # After indexing, record the served frame count (all buckets have the
    # same length post MAX_FRAMES + SKIPS trim) for the welcome stats line.
    global _STATS_FRAMES
    if BUCKETS:
        _STATS_FRAMES = len(next(iter(BUCKETS.values())).paths)

    # Pre-warm the most popular bucket so the very first client gets smooth
    # playback without per-frame disk reads.
    if args.prewarm and args.prewarm in BUCKETS:
        prewarm_bucket(BUCKETS[args.prewarm])
        _write_status()  # update status to show the cached bucket

    # Prefer uvloop if it's installed — drop-in replacement that runs the
    # asyncio event loop on libuv and is typically 2-4x faster than the
    # stock loop. Safe to skip if the module isn't there.
    try:
        import uvloop  # type: ignore
        uvloop.install()
        log.info("uvloop active")
    except ImportError:
        log.info("uvloop not available, using stock asyncio loop")

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
