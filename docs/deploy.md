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

The frames are not in the git repo — they're 14 GB. Bake them on your
workstation (see `docs/bake.md`), then ship them over with zstd
compression:

```bash
# on your workstation, inside the repo:
tar -C frames -cf - . | zstd -T0 -6 -o /tmp/sr-frames.tar.zst
scp /tmp/sr-frames.tar.zst paulie420@<lxc-ip>:/tmp/

# on the LXC:
cd ~/second-reality-TELNET
mkdir -p frames
zstd -dc /tmp/sr-frames.tar.zst | tar -C frames -xf -
du -sh frames   # sanity check: should be ~14G
rm /tmp/sr-frames.tar.zst
```

## 4. First run (manual)

Before wiring systemd, make sure it works by hand:

```bash
source .venv/bin/activate
python -m srtelnet.server --port 2323 --frames ./frames
```

You should see `listening on 0.0.0.0:2323` and the per-bucket load lines.
From another terminal:

```bash
telnet <lxc-ip> 2323
```

You should get the welcome screen. Press any key to start playback. Press
`q` to quit.

Ctrl-C the server to stop.

## 5. Install the systemd unit

The repo ships a unit file at `deploy/srtelnet.service`. Install it:

```bash
sudo cp deploy/srtelnet.service /etc/systemd/system/srtelnet.service
sudo systemctl daemon-reload
sudo systemctl enable --now srtelnet.service
sudo systemctl status srtelnet.service
journalctl -u srtelnet.service -f   # tail logs live
```

If you change `--port` in the unit file, remember to
`sudo systemctl daemon-reload && sudo systemctl restart srtelnet.service`.

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

**Frames dir empty after upgrade**:
- `.gitignore` excludes `frames/` — `git pull` won't touch them.
  Re-ship with the zstd tarball flow from step 3.
