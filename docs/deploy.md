# Deploying second-reality-TELNET to an LXC

This doc covers deploying the telnet server on an unprivileged Proxmox LXC
so that clients can `telnet <host> <port>` to watch Second Reality.

## 0. Prereqs on the LXC

Debian 12 unprivileged container, networked, with SSH access. Install the
packages you need:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git libcap2-bin sudo
```

## 1. Clone and install the server

```bash
cd ~
git clone https://github.com/Paulie420/second-reality-TELNET.git
cd second-reality-TELNET
python3 -m venv --copies .venv    # --copies is important for setcap
source .venv/bin/activate
pip install -U pip
pip install -e .
python -c "import telnetlib3, srtelnet.server; print('ok')"
```

The `--copies` flag on `venv` creates a real binary rather than a symlink,
so `setcap` below can apply to the venv python rather than the system one.

## 2. Let the server bind low ports without root

If you want to run on port 23 (or any port below 1024), grant the venv
python `cap_net_bind_service`. This is per-binary and survives reboots;
it does NOT give python any other elevated rights.

```bash
sudo setcap cap_net_bind_service=+ep .venv/bin/python3
getcap .venv/bin/python3
# expected: .venv/bin/python3 cap_net_bind_service=ep
```

If you're running on a high port (e.g. 2323), skip this step entirely.

## 3. Ship the baked frames

The frames are not in the git repo — they're 9–14 GB depending on fps.
Bake them on your workstation (see `docs/bake.md`), then ship them
over with zstd compression.

If you baked **30fps only** (classic default):

```bash
# on your workstation, inside the repo:
tar -C frames -cf - . | zstd -T4 -6 -o /tmp/sr-frames-30fps.tar.zst
scp /tmp/sr-frames-30fps.tar.zst paulie420@<lxc-ip>:/tmp/

# on the LXC:
cd ~/second-reality-TELNET
mkdir -p frames-30fps
zstd -dc /tmp/sr-frames-30fps.tar.zst | tar -C frames-30fps -xf -
du -sh frames-30fps   # sanity check: should be ~14G
rm /tmp/sr-frames-30fps.tar.zst
```

If you baked **20fps only** (low-bandwidth variant):

```bash
# on your workstation:
tar -C frames-20fps -cf - . | zstd -T4 -6 -o /tmp/sr-frames-20fps.tar.zst
scp /tmp/sr-frames-20fps.tar.zst paulie420@<lxc-ip>:/tmp/

# on the LXC:
cd ~/second-reality-TELNET
mkdir -p frames-20fps
zstd -dc /tmp/sr-frames-20fps.tar.zst | tar -C frames-20fps -xf -
du -sh frames-20fps   # ~9.4G
rm /tmp/sr-frames-20fps.tar.zst
```

If you baked **both**, repeat the flow for each. You'll end up with
`frames-20fps/` and `frames-30fps/` side by side; `switch_fps.sh`
(step 5) expects exactly this layout.

## 4. First run (manual)

Before wiring systemd, make sure it works by hand. Pick whichever
bake you want to test first:

```bash
source .venv/bin/activate

# 30fps run:
python -m srtelnet.server --port 2323 --frames ./frames-30fps

# OR 20fps run (scale the end-trim and skip to 20fps):
python -m srtelnet.server --port 2323 --frames ./frames-20fps \
    --fps 20 --max-frames 10163 --skip 897:1117
```

You should see `listening on 0.0.0.0:2323` and the per-bucket load lines.
From another terminal:

```bash
telnet <lxc-ip> 2323
```

You should get the welcome screen — the stats line reflects whichever
fps / frame count you launched with (no code edit needed to keep the
display accurate). Press any key to start playback. Press `q` to quit.

Ctrl-C the server to stop.

## 5. Install the systemd unit via switch_fps.sh

Instead of hand-editing `/etc/systemd/system/srtelnet.service`, use the
`tools/switch_fps.sh` script — it generates a canonical unit for the
requested fps, reloads systemd, restarts the service, and verifies the
restart. Works for both initial install and subsequent fps flips.

```bash
cd ~/second-reality-TELNET
sudo tools/switch_fps.sh 20    # or 30, whichever you want to run
```

After it completes:

```bash
sudo systemctl status srtelnet.service
sudo systemctl enable srtelnet.service    # so it starts on boot
journalctl -u srtelnet.service -f         # tail logs live
```

You should see 9 bucket-indexed lines, a `listening on 0.0.0.0:2323`,
and (for 20fps) a `playback skips: [(897, 1117)]` confirmation in the
logs.

### Manual unit install (alternative)

The repo also ships a template at `deploy/srtelnet.service`. If you
prefer managing the unit by hand:

```bash
sudo cp deploy/srtelnet.service /etc/systemd/system/srtelnet.service
sudo systemctl daemon-reload
sudo systemctl enable --now srtelnet.service
```

Note that `switch_fps.sh` **overwrites** any manually-installed unit
with its generated version. If you've customized the unit (extra
env vars, resource limits, etc.), either put those customizations
into `switch_fps.sh`'s unit-writing functions or use a drop-in at
`/etc/systemd/system/srtelnet.service.d/override.conf` which systemd
layers on top and `switch_fps.sh` does not touch.

## 5a. Switching fps on a live server

Operators can flip between 20fps and 30fps on the running service
without a redeploy or a rebake — provided both frame sets have been
shipped to the LXC at least once. `switch_fps.sh` handles:

- Writing a fresh systemd unit for the target fps (with the correct
  `--fps`, `--max-frames`, `--skip`, and `--frames` path).
- `daemon-reload` and `restart`.
- **Auto-archiving the inactive fps** to `archives/frames-NNfps.tar.zst`
  and removing the uncompressed copy, reclaiming disk.
- **Auto-unpacking** the target fps from its archive if only the
  compressed version is on disk.

Typical flow:

```bash
# currently serving 30fps, want to switch to 20fps:
cd ~/second-reality-TELNET
sudo tools/switch_fps.sh 20
# ... restart ...
# ... archives frames-30fps/ -> archives/frames-30fps.tar.zst ...
# ... reclaims ~14 GB ...

# later, back to 30fps:
sudo tools/switch_fps.sh 30
# ... unpacks archives/frames-30fps.tar.zst -> frames-30fps/ ...
# ... restart ...
# ... archives frames-20fps/ -> archives/frames-20fps.tar.zst ...
# ... reclaims ~9.4 GB ...
```

`switch_fps.sh status` shows which fps is currently serving, what
frame sets are on disk (uncompressed vs archived), and the recent
bucket-index log lines.

### One-time migration note

If you upgraded from an older deploy that stored 30fps frames at
`frames/` (no fps suffix), the first `switch_fps.sh` invocation will
automatically rename `frames/` → `frames-30fps/` before doing anything
else. Safe to run repeatedly — the rename only happens if needed.

## 6. Publishing via Nginx Proxy Manager (TCP stream)

NPM supports raw TCP forwarding via the **Streams** tab. Telnet is a plain
TCP protocol, so this works — NPM is not inspecting the bytes, just
proxying them.

1. In NPM: **Streams → Add Stream**.
2. **Incoming Port**: the port you want publicly exposed (e.g. 1337).
3. **Forwarding Host**: your LXC's IP (e.g. `10.0.0.196`).
4. **Forwarding Port**: the port the server is listening on (e.g. 2323).
5. Check **TCP Forwarding**. Leave UDP off.
6. Save.

Make sure your router/firewall forwards the chosen public port to the NPM
host.

Test from outside:

```bash
telnet 20forbeers.com 1337
```

### About port 23

If you want to use the standard telnet port 23 publicly, you'll need:
- Port 23 open inbound on your firewall
- NPM listening on 23 (NPM's own host must not have anything else on 23)
- The LXC server either listening on 23 directly (with setcap) or on a
  high port with NPM forwarding 23 → high port

NPM itself is usually running inside a container with its own host. The
stream only needs its incoming port to be free on the NPM host.

## 7. Troubleshooting

**Server starts but no one can connect**:
- `ss -tlnp | grep 2323` to confirm it's bound and on which interface
- Check the LXC's firewall (`iptables -L -n` or `nft list ruleset`)
- Check NPM's stream is up and pointing at the right host:port

**"Connection refused" via NPM**:
- Usually NPM can't reach the LXC. From the NPM host, try
  `nc -v <lxc-ip> <server-port>` to isolate the hop.

**Disconnect partway through**:
- Check `journalctl -u srtelnet.service` for peer disconnect reason. The
  most common cause is the client pressing q (which is correct).
- At very high fps / large buckets you may hit upstream bandwidth limits.
  The server has frame-skip pacing so it won't stall, but viewers may see
  skipped frames.
- The server enables TCP keepalive on every accepted socket (30s idle,
  10s probes, 3 retries) so genuinely dead clients are detected within
  ~60s instead of hanging forever on a half-open flow. Slow-but-alive
  clients are handled by per-frame backpressure: the play loop checks
  the transport write-buffer size before each frame and skips the frame
  if the buffer is over 512 KB. Result: WAN clients see a lower
  effective frame rate instead of getting kicked.

**Resizing the terminal mid-stream**:
- The server reads NAWS (RFC 1073) on every play-loop tick, so when a
  client resizes their terminal window, the next frame is rendered into
  a freshly chosen bucket and re-centered on the new size. No reconnect
  needed.
- This works in any terminal that sends a NAWS subnegotiation on resize:
  iTerm2, Alacritty, Kitty, Wezterm, Windows Terminal, GNOME Terminal,
  Konsole, foot, etc. A small set of older clients only send NAWS once
  during the initial handshake — for those, the bucket is locked to the
  initial size and a mid-stream resize will look broken until reconnect.
  If a user reports "the demo got mangled when I made my window bigger,"
  ask which terminal they're using.

**Frames dir empty after upgrade**:
- `.gitignore` excludes `frames/` — `git pull` won't touch them.
  Re-ship with the zstd tarball flow from step 3.
