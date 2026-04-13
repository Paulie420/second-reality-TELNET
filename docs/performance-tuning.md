# Performance tuning for remote/WAN clients

This document explains the WAN-client optimizations in this project and
what each one does. It's a retrospective — everything described here
has shipped and is live on the reference deployment at
`telnet 20forbeers.com`.

## The problem we solved

The 30 fps truecolor ANSI stream can burst up to ~2 MB/s on the
200-wide bucket. On a WAN client with less upstream than that, the TCP
send buffer fills and frames queue server-side. The symptoms:

1. **Input latency floor.** Keystrokes (space, seek, quit) only render
   after the already-written backlog drains to the client. With the
   original 512 KB skip threshold and 100–300 KB/s WAN bandwidth,
   that was 2–5 seconds of "nothing happened" after every press.
2. **Seek lands in the wrong place.** The backpressure skip logic
   (that drops frames when `WRITE_BUF_SKIP` is exceeded) was seek-
   agnostic. On a slow link the seek target frame itself got dropped
   and `i` advanced past the intended landing point before anything
   hit the wire.
3. **Big terminal + bad link = unwatchable.** A viewer maximizing
   their terminal (180 cols) got the 180-wide bucket regardless of
   whether their pipe could sustain it. Heavy frame-dropping with no
   self-correction.
4. **Visible cursor flashing.** Chafa emits per-frame cursor
   show/hide escapes; the per-frame hide/show cycle is invisible on
   LAN (sub-millisecond gaps) but becomes a visibly flashing cursor
   once WAN latency and `TCP_NODELAY` widen the gap.
5. **Operator fps lock-in.** Operators had to pick 20 fps or 30 fps
   at deploy time, with no good way to switch between them on a
   running server or measure which actually worked better.

## What shipped

### Tier 1 — bounded keystroke latency and snappy seek

| Change | Effect |
|---|---|
| **Drain-then-render on seek.** After `←`/`→`, write `CLEAR+HOME` for instant visual ack, wait up to 1.5 s for the write buffer to drop below `WRITE_BUF_LOW`, then bypass the backpressure skip for the first post-seek frame so the target is guaranteed to land. | Seeks feel snappy instead of laggy; target frame never dropped. |
| **Lower write-buffer watermarks** from 1 MB / 512 KB / 256 KB to 256 KB / 128 KB / 48 KB for `HIGH` / `SKIP` / `LOW`. | Caps worst-case keystroke-to-render latency on a 100–300 KB/s WAN client at ~0.4–1.3 s. |
| **`TCP_NODELAY`** on the accepted socket. | Kills Nagle coalescing (up to ~200 ms) which was pointless given we already batch at the application layer. |
| **`TCP_QUICKACK`** on the accepted socket (Linux). | ACKs inbound keystrokes immediately instead of waiting up to 40 ms for a piggyback. Shaves RTT off seek/pause/quit responses. |

### Tier 2 — bandwidth reduction + adaptive bucket selection

| Change | Effect |
|---|---|
| **20 fps truecolor bake option** (alongside the 30 fps bake). 28 % bandwidth reduction per second at the same visual fidelity. Operators choose via `tools/switch_fps.sh`. | Consistently playable on cellular / hotel wifi / constrained uplinks. |
| **`tools/switch_fps.sh`** flip tool. Rewrites the systemd unit, restarts the service, and auto-archives the inactive fps frame set to `archives/*.tar.zst`. | Operators switch fps without a rebake, preserving the inactive set compressed for fast future flips (~2–3 min round-trip once both archives exist). |
| **Per-client adaptive bucket downgrade.** After 2 s of consecutive backpressure-skipped frames the play loop steps the client down to the next-smaller loaded bucket; after 10 s of clean writes it steps back up, capped at the NAWS-picked natural bucket. Asymmetric thresholds prevent thrashing. | Big-terminal-on-bad-link viewers self-adapt to a bucket their pipe can sustain instead of sitting through frame-drop cycles. |
| **Wall-clock `MAX_FRAMES` / `SKIPS`.** `DEFAULT_MAX_SECONDS = 508.13` and `DEFAULT_SKIP_SECONDS = [(44.87, 55.87)]` auto-convert to frame indices at startup based on `--fps`. | 20 fps and 30 fps units are identical except for `--fps`; no per-rate override math. |

### Cosmetic / observability

| Change | Effect |
|---|---|
| **Chafa cursor-flash strip.** `_parse_frame_lines` regex-strips the per-frame `\x1b[?25l` / `\x1b[?25h` toggles that chafa emits. | No visible flashing cursor during playback, especially noticeable on WAN with `TCP_NODELAY`. |
| **Dynamic welcome stats.** Welcome screen's "N frames · M fps · truecolor ANSI" line reads live runtime state instead of hardcoded values. | Display always matches what's actually being served; no code edit on fps switch. |
| **Per-session CSV telemetry**: `peak_buffer_kb`, `seek_count`, `final_bucket_width` added to `state/connections.csv`. | WAN quality measurable post-hoc: peak buffer = "how backed up did this client get", seek count = engagement, final bucket = how often we downgrade. `tools/stats25.sh` displays them in the 5-connection tail. |

## What this means numerically

Before Tier 1, from a 200 KB/s WAN client:

- Worst-case keystroke → render latency: ~2.5 s
- Seek: stalled 2–5 s, often landed past the target frame
- Frame drops during busy scenes: frequent

After all of the above:

- Worst-case keystroke → render latency: ~0.65 s
- Seek: clears screen instantly, resumes playback ~0.5–1 s later at
  the correct position
- Frame drops: either rare (client keeps up) or self-corrected within
  2 s by an adaptive downgrade to a bucket that fits the pipe

On a LAN / fiber client, nothing should look different from the
pre-optimization baseline — the adaptive logic only engages when the
buffer actually fills, which for fast clients basically never happens.

## Verification

Exercise on a WAN client (phone hotspot, coffee-shop wifi, whatever's
not the same LAN as the server):

1. **Seek feel.** Press `→` during playback. Screen clears within a
   frame, ~0.5–1 s pause while the backlog flushes, playback resumes
   5 s ahead at the right frame.
2. **Seek mashing.** Press `→` four or five times quickly. Deltas
   accumulate into one ~20–25 s jump, not four stacked stalls.
3. **Pause / quit.** `space` and `q` halt or disconnect within a
   fraction of a second.
4. **Adaptive downgrade.** Connect with a large terminal on a
   deliberately bad link. After ~2 s of stuttering, the server
   should log `adaptive downgrade: bucket N -> M` and playback should
   smooth out at the smaller image size. Check `state/connections.csv`
   after disconnect — the `final_bucket_width` column should differ
   from `bucket_width` (the starting bucket).
5. **Welcome stats accuracy.** Welcome should read
   `9,943 frames · 20 fps · truecolor ANSI` at 20 fps, or
   `14,914 frames · 30 fps · truecolor ANSI` at 30 fps, matching
   whatever the systemd unit is currently configured for.
6. **No cursor flash.** Terminal cursor should be invisible for the
   entire playback duration.

## File map

The code touched by this work lives in:

- `src/srtelnet/server.py` — all Tier 1 and Tier 2 runtime logic.
  Key helpers: `_configure_socket` (NODELAY + QUICKACK + keepalive),
  `smaller_bucket` / `larger_bucket` (adaptive step helpers),
  `_play_once` (orchestrates everything on the playback hot path),
  `_log_connection` (CSV writer).
- `tools/switch_fps.sh` — operator-facing fps flip tool.
- `tools/bake_frames.py` — bake pipeline, supports `--fps N` for
  either variant.
- `tools/stats25.sh` — compact one-screen operator readout including
  the new telemetry columns.
- `state/connections.csv` — per-session telemetry. See schema in
  `_log_connection`'s docstring.

See [`bake.md`](bake.md) and [`deploy.md`](deploy.md) for the
operational workflows (baking, shipping frames, installing systemd,
flipping fps).

## Rollback

Every optimization above landed as its own commit, so rolling any
individual change back is a targeted `git revert <sha>` rather than a
sweeping reset. The reference deployment has confirmed all of them
working together in production; partial reverts have not been needed.
