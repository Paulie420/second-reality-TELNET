# second-reality-TELNET

Stream **Future Crew's *Second Reality* (1993)** as pre-rendered truecolor ANSI frames over
a plain telnet connection. Any modern terminal with 24-bit color support can watch the demo
by typing one line.

```
telnet <host>
```

No client install, no audio, no JavaScript, no webpage. Just bytes into your terminal the
way the demoscene gods intended — well, the way they *would* have intended if 1993 had 16.7
million colors per cell.

This project is a love letter to two things:
- **Future Crew's *Second Reality***, released at Assembly 1993 and still one of the most
  revered productions in demoscene history.
- **Jeff Quast's `network23`** — the telnet-streaming backend behind
  [`telnet 1984.ws`](https://1984.ws) — which showed everyone this was possible in ~277
  lines of Python and generously released its source into the public domain.

---

## How it works (30-second version)

1. **Bake**: a local script (`tools/bake_frames.py`) runs the source video through
   [`ffmpeg`](https://ffmpeg.org) to extract frames at 30 fps, then pipes each frame
   through [`chafa`](https://hpjansson.org/chafa/) to produce one `.ans` file per frame per
   terminal-width bucket (40, 60, 80, …, 200 cells wide). Nine width buckets total so the
   server can pick the best fit for any client terminal.
2. **Serve**: a small [`telnetlib3`](https://github.com/jquast/telnetlib3) server accepts
   incoming telnet connections, negotiates window size via
   [RFC 1073 (NAWS)](https://www.rfc-editor.org/rfc/rfc1073), picks the largest bucket
   that fits, caches the frames in RAM, and pumps them out at 30 fps with
   [`blessed`](https://github.com/jquast/blessed) for cursor positioning and ANSI reset
   handling. If a client can't keep up, frames are skipped rather than queued — playback
   stays in sync, low-end clients just see a lower effective frame rate.
3. **Deploy**: the server runs inside an unprivileged Debian 12 LXC on Proxmox, bound to
   TCP port 23 via `setcap cap_net_bind_service=+ep` so it doesn't need root.

The frames themselves are *not* in this repo — they're a few gigabytes of
pre-rendered terminal escape codes, re-generated locally from whatever source video you
provide. See [`docs/bake.md`](docs/bake.md).

## Documentation

- [**`docs/architecture.md`**](docs/architecture.md) — how the server, bake pipeline, and
  frame format fit together.
- [**`docs/bake.md`**](docs/bake.md) — how to re-bake frames from your own source video.
- [**`docs/deploy.md`**](docs/deploy.md) — Proxmox LXC setup, systemd unit, port 23
  binding, firewall notes.
- [**`docs/credits.md`**](docs/credits.md) — Future Crew, Jeff Quast, and everything else
  this project stands on.

## Build and run (local development)

```bash
# one-time: create venv, install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# provide a source video (any format ffmpeg can decode)
# this repo assumes second_reality_short.webm in the project root
# see docs/bake.md for how to prepare one

# bake frames (runs on your Mac; writes to ./frames/)
python tools/bake_frames.py second_reality_short.webm

# play locally in your own terminal (no networking, sanity check)
python tools/play.py frames/120

# run the telnet server on a non-privileged port for local testing
python -m srtelnet.server --port 2323

# then, in another terminal:
telnet localhost 2323
```

See [`docs/deploy.md`](docs/deploy.md) for the production LXC setup.

## License

This project's own source is released under **The Unlicense** (public domain), matching
jquast's stance on `network23`. You can do whatever you want with the code.

The source video of *Second Reality* is **not** included in this repository and **is not
redistributed by this project**. You must supply your own copy (recording from DOSBox,
downloading a capture, etc.). Second Reality itself is a 1993 Future Crew production —
please respect the creators and their scene. See [`docs/credits.md`](docs/credits.md) for
links to canonical archives.
