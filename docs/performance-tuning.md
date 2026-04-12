# Performance tuning for remote/WAN clients

Problem: seek (`←` / `→`) and general responsiveness feel slow or unusable
when connecting from across a WAN vs. from the same LAN as the server.

## Root cause

The 30 fps truecolor ANSI stream can burst up to ~2 MB/s on the 200-wide
bucket. On a WAN client with less upstream than that, the TCP send buffer
fills up and frames queue on the server side. Two consequences:

1. **Input latency floor.** Any keystroke (space, seek, quit) is rendered
   only after the backlog of already-written frames has drained to the
   client. With the original 512 KB skip threshold and 100–300 KB/s WAN
   bandwidth, that's 2–5 seconds of "nothing happened" after every press.
2. **Seek lands in the wrong place.** The backpressure skip logic (the
   one that drops frames when the buffer is over `WRITE_BUF_SKIP`) doesn't
   know about seek. On a slow link, the seek target frame itself gets
   dropped and `i` advances past the intended landing point before a
   frame actually hits the wire.

## Tiered options

Options ranked by ROI — highest first. Tier-1 is a single PR worth of
work, no rebake, no visual change, and fixes the user-visible complaint.

### Tier 1 — do these first (highest ROI, smallest change)

- [x] **1. Drain-then-render on seek.** After `←`/`→`, write
      `CLEAR + HOME` immediately for instant visual acknowledgment, wait
      up to 1.5s for the write buffer to drain below `WRITE_BUF_LOW`, and
      bypass the backpressure skip for the first frame post-seek so the
      target frame is guaranteed to land. Effect: seeks feel snappy, not
      laggy. *(shipped — see commit below)*
- [x] **2. Lower write-buffer watermarks.** Drop the buffer ceiling from
      1 MB / 512 KB / 256 KB down to 256 KB / 128 KB / 48 KB for
      `HIGH` / `SKIP` / `LOW`. Bounds the worst-case input-to-display
      latency on any slow link to ~0.6–1s instead of ~2.5–5s. Slow
      clients drop frames a bit earlier but the stream stays responsive.
      *(shipped — see commit below)*
- [x] **3. TCP_NODELAY.** Disable Nagle on the accepted socket.
      Eliminates up to ~200 ms of coalescing delay. Free win. *(shipped —
      see commit below)*

### Tier 2 — bigger surgery, bigger wins

- [ ] **4. 256-color rebake at 30 fps.** Truecolor SGR (`\x1b[38;2;R;G;Bm`,
      up to 19 bytes per color change) shrinks to 256-color
      (`\x1b[38;5;Nm`, up to 11 bytes). 30–40% fewer bytes on frames
      dominated by color changes. Modern terminals still look great in
      256-color. *A previous 256-color experiment (f62d241) was reverted
      because it was paired with 20 fps, which hurt motion. 256-color at
      the original 30 fps has not yet been tested in isolation.*
- [ ] **5. Per-client bucket downgrade on sustained backpressure.** If
      `consec_skip` stays above a threshold for a few seconds, drop the
      client to the next smaller bucket (e.g. 120 → 100 → 80). Keeps WAN
      clients in a bucket their pipe can actually sustain. Moderate
      implementation cost; stacks cleanly on the existing NAWS-driven
      bucket switch.
- [ ] **6. On-connect bandwidth probe.** Write a ~200 KB pad at connect
      time, measure drain rate, bias the initial bucket pick accordingly.
      Rougher than #5 but simpler. Best combined with #5.

### Tier 3 — good ideas, questionable ROI

- [ ] **7. Delta frames.** Bake-time pass producing keyframes +
      per-cell diffs. Huge wins on long static holds, weak wins on motion
      sections. Substantial complexity; seek has to snap to nearest
      keyframe and replay intervening deltas. Storage roughly doubles.
- [ ] **8. MCCP2 (zlib telnet compression).** ANSI compresses 4–5× with
      gzip, but almost no modern terminal's built-in telnet client
      negotiates MCCP2 — only BBS clients do, and those already don't
      handle truecolor. Near-empty client intersection. Skip.
- [ ] **9. Lower FPS globally.** 20 fps saves 33% bandwidth, stutters
      visibly on motion-heavy sections. Already tried and reverted.
      24 fps as a compromise is marginal.
- [ ] **10. 16-color fallback.** Tiny bytes per frame but looks rough.
      Would work as an opt-in keybind ("press `c` to drop to 16-color")
      for really bad links, not as default.

## Revert points

- Pre-Tier-1 baseline: `34168fb` (`fix: make drain timeout non-fatal...`).
  To roll everything back:
  ```
  git reset --hard 34168fb
  git push --force-with-lease origin main
  ```
- Per-commit reverts: each tier change should land as its own commit so
  individual ideas can be reverted without pulling down the others.

## Verification

When a Tier-1 change lands, exercise the following from a WAN client
(not the LAN):

1. **Seek feel.** During playback, hit `→` once. Expect the screen to
   clear for ~0.5–1s, then the stream resumes ~5s ahead. Before Tier-1,
   seeking from WAN was laggy or effectively dropped.
2. **Seek mashing.** Press `→` four or five times quickly. The deltas
   should accumulate into one ~20–25s jump, not queue up as independent
   stalls.
3. **Pause responsiveness.** Space-bar should halt playback within a
   fraction of a second, not after the in-flight backlog drains.
4. **Quit.** `q` should terminate the connection promptly.
5. **Playback quality on fast links.** From a LAN / fiber client, the
   stream should look identical to before. Any visible regression means
   the new buffer watermarks are too tight.
