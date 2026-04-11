# second-reality-TELNET

Future Crew's *Second Reality* (1993) streamed as pre-baked truecolor ANSI frames over
plain telnet. One command, no client install, no browser, just bytes into your
terminal.

## Try it live

```
telnet 20forbeers.com
```

Works in any modern terminal that handles 24-bit color: iTerm2, Alacritty, Kitty,
Wezterm, Windows Terminal, GNOME Terminal, Konsole, foot, st with truecolor patches,
basically anything from the last decade.

It does **not** work in Syncterm, Netrunner, PuTTY, or Windows `telnet.exe`. None of
those handle 24-bit SGR escapes, so you'll get a wall of garbage. If that happens, the
client is the problem, not the stream — switch to one of the terminals listed above.

A 100x40-ish window is a good middle ground. Bigger windows pull bigger frame buckets
(up to 200x75) and chew more bandwidth; smaller ones use the smallest bucket (40x15)
and barely tickle a dialup line. The server picks the largest bucket that fits your
window, and **re-picks live as you resize**, so you can drag the corner of your
terminal mid-demo and the stream will switch buckets on the next frame.

### Keyboard controls

| key              | action                        |
| ---------------- | ----------------------------- |
| `space`          | pause / resume                |
| `←` / `→`        | seek back / forward 5 seconds |
| `q` or `Ctrl-C`  | quit                          |
| any key          | skip the welcome or goodbye   |

---

This project owes its existence to two things:

- **Future Crew's *Second Reality***, released at Assembly 1993 and still one of the
  most revered productions in demoscene history.
- **Jeff Quast's `network23`**, the telnet streaming backend behind
  [`telnet 1984.ws`](https://1984.ws), released into the public domain in roughly 277
  lines of Python. The whole approach here is lifted from his playbook.

## How it works

1. **Bake.** `tools/bake_frames.py` runs the source video through
   [`ffmpeg`](https://ffmpeg.org) at 30 fps and pipes each frame through
   [`chafa`](https://hpjansson.org/chafa/). Output is one `.ans` file per frame per
   width bucket: 40, 60, 80, 100, 120, 140, 160, 180, 200 cells wide. Nine buckets,
   ~15k frames each, a few gigabytes total.
2. **Serve.** A small [`telnetlib3`](https://github.com/jquast/telnetlib3) server
   accepts connections, reads window size from
   [RFC 1073 NAWS](https://www.rfc-editor.org/rfc/rfc1073), picks the biggest bucket
   that fits, and pumps frames at 30 fps. The play loop watches for fresh NAWS
   updates and switches buckets on the fly when you resize. Slow clients get fewer
   frames — the loop drops them rather than queueing — so playback stays in sync
   instead of wedging.
3. **Deploy.** Unprivileged Debian 12 LXC on Proxmox, bound to TCP 23 via
   `setcap cap_net_bind_service=+ep`. Public traffic goes through an
   nginx-proxy-manager TCP stream.

The frames are not in this repo. They're a few gigabytes of pre-rendered escape codes,
re-baked locally from whatever source video you supply. See
[`docs/bake.md`](docs/bake.md).

## Known limitations

- **Bandwidth.** 30fps truecolor ANSI is heavy. The 80-wide bucket runs around
  300 KB/sec; the 200-wide bucket can hit 2 MB/sec. Cellular, hotel wifi, and packet-
  dropping proxies will degrade gracefully (slow clients see a lower effective frame
  rate), but a really bad link can still drop you mid-stream. Reconnect, or shrink
  your terminal to drop into a smaller bucket.
- **No audio.** Purple Motion and Skaven's soundtrack is half the demo. Play it
  yourself alongside the stream from your favorite tracker/mod source.
- **Minimum size.** Terminals below 40x20 get rejected at connect — anything smaller
  would just be unreadable noise.
- **Resize behavior.** Most terminals send a NAWS update on every resize and the
  server picks it up on the next frame. A few clients only send NAWS once during the
  initial handshake; if you resize one of those mid-stream, things will look wrong
  until you reconnect.
- **Windows `telnet.exe`** doesn't do truecolor. Use Windows Terminal with `telnet`
  from WSL, or any of the other modern terminals listed up top.

## Documentation

- [`docs/bake.md`](docs/bake.md) — re-baking frames from your own source video.
- [`docs/deploy.md`](docs/deploy.md) — Proxmox LXC setup, systemd unit, port 23
  binding, firewall notes.
- [`docs/credits.md`](docs/credits.md) — Future Crew, Jeff Quast, and everything else
  this project stands on.

## Build and run (local development)

```bash
# one-time: create venv, install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# supply a source video (any format ffmpeg can decode).
# this repo assumes second_reality_short.webm in the project root.
# see docs/bake.md for how to prepare one.

# bake frames into ./frames/
python tools/bake_frames.py second_reality_short.webm

# play locally in your own terminal (no networking, sanity check)
python tools/play.py frames/120

# run the telnet server on a non-privileged port for local testing
python -m srtelnet.server --port 2323

# then, from another terminal:
telnet localhost 2323
```

See [`docs/deploy.md`](docs/deploy.md) for the production LXC setup.

## License

Source is released under **The Unlicense** (public domain), matching jquast's stance
on `network23`. Do whatever you want with the code.

The source video of *Second Reality* is **not** included in this repo and **not**
redistributed by this project. Supply your own (DOSBox capture, downloaded archive,
whatever you have). Second Reality is a 1993 Future Crew production — see
[`docs/credits.md`](docs/credits.md) for links to canonical sources.
