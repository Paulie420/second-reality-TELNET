# Operating second-reality-TELNET

Day-to-day monitoring, tuning, and capacity planning for the telnet server.
For initial deployment, see `docs/deploy.md`.

## Quick reference

```bash
# Live status (active connections, cache, bucket popularity)
cat ~/second-reality-TELNET/state/status.txt

# Tail server logs
journalctl -u srtelnet.service -f

# Connection history
tail -20 ~/second-reality-TELNET/state/connections.csv

# Restart after a code update
cd ~/second-reality-TELNET && git pull && sudo systemctl restart srtelnet

# Restart after changing the .service file
sudo cp deploy/srtelnet.service /etc/systemd/system/srtelnet.service
sudo systemctl daemon-reload && sudo systemctl restart srtelnet
```

## Status file

The server writes `state/status.txt` on every connect and disconnect. It
shows:

- **active connections**: clients currently streaming
- **peak connections**: highest concurrent count since last restart
- **session / lifetime totals**: connection counts (lifetime persists across
  restarts via `state/counter.txt`)
- **cache policy**: how many buckets are kept cached, idle eviction timeout
- **bucket table**: per-width cache fill %, hit count, active clients, idle
  time

Example:

```
second-reality-TELNET status
============================
active connections : 2
peak connections   : 14
session total      : 47
lifetime total     : 597
uptime             : 6h 12m 03s
cache policy       : max 4 buckets, evict after 600s idle

buckets:
   40w:     0/14914 cached (  0%)  hits=0    active=0
   60w:     0/14914 cached (  0%)  hits=1    active=0  idle 3204s
   80w: 14914/14914 cached (100%)  hits=38   active=2
  100w:     0/14914 cached (  0%)  hits=0    active=0
  120w: 14914/14914 cached (100%)  hits=7    active=0  idle 142s
  140w:     0/14914 cached (  0%)  hits=0    active=0
  160w:  8200/14914 cached ( 54%)  hits=1    active=0  idle 580s
  180w:     0/14914 cached (  0%)  hits=0    active=0
  200w:     0/14914 cached (  0%)  hits=0    active=0

updated: 2026-04-12 20:15:03
```

## Connection log

Every completed connection appends one line to `state/connections.csv`:

```
timestamp,ip,bucket_width,frames_played,total_frames,skipped,duration_s,outcome
2026-04-12 14:30:01,203.0.113.5,80,14914,14914,12,512.3,done
2026-04-12 14:31:45,198.51.100.2,120,4200,14914,0,140.8,quit
```

Fields:
- **bucket_width**: which resolution bucket was served
- **frames_played**: how far the viewer got before disconnecting
- **skipped**: frames dropped due to backpressure (slow client)
- **duration_s**: total connection time in seconds
- **outcome**: `done` (watched it all), `quit` (pressed q), `disconnect`
  (connection dropped), `quit_welcome` (quit on welcome screen),
  `too_small` (terminal too small)

Useful one-liners:

```bash
# How many people watched the whole thing?
grep ',done$' state/connections.csv | wc -l

# Most popular bucket widths
awk -F, 'NR>1{print $3}' state/connections.csv | sort | uniq -c | sort -rn

# Average watch duration
awk -F, 'NR>1{sum+=$7; n++} END{printf "%.0fs avg over %d connections\n", sum/n, n}' state/connections.csv

# Connections by hour
awk -F'[, :]' 'NR>1{print $2" "$3":00"}' state/connections.csv | sort | uniq -c
```

## Memory and cache management

### How memory works

The server indexes frame file paths on startup (~90 MB). When a client
plays through a bucket, each frame is read from disk, parsed into Python
string tuples, and cached in memory. A fully cached bucket uses roughly:

| Bucket | Disk size | Python memory |
|--------|-----------|---------------|
| 40w    | 208 MB    | ~350 MB       |
| 80w    | 644 MB    | ~1.0 GB       |
| 120w   | 1.3 GB    | ~2.0 GB       |
| 160w   | 2.1 GB    | ~3.2 GB       |
| 200w   | 3.2 GB    | ~4.9 GB       |

### Cache eviction

The server keeps at most `MAX_CACHED_BUCKETS` (default 4) buckets cached.
When a client disconnects, the server checks for idle buckets:

1. Any bucket with no active clients and idle > `CACHE_IDLE_SECONDS`
   (default 600s / 10 min) is evicted.
2. If still over the cap, the least-recently-used idle bucket is evicted.
3. Buckets with active clients are **never** evicted mid-stream.

After eviction, the frames remain on disk. The next client to request that
bucket will re-cache from disk (the Linux page cache typically still holds
the files, so re-caching is fast).

### Pre-warming

The `--prewarm 80` flag (default, set in the systemd unit) caches the
entire 80-wide bucket at startup. This means the very first client gets
smooth playback without per-frame disk reads. Startup takes ~10-20s
longer but is worth it.

### Tuning for your RAM

With 8 GB RAM, 4 cached buckets is comfortable:

- 80w (1.0 GB) + 120w (2.0 GB) + 100w (1.5 GB) + 60w (0.6 GB) = 5.1 GB
- Leaves ~2.5 GB for OS, Python interpreter, Linux page cache, swap buffer

To change the max cached buckets:

```bash
# Environment variable (no code change needed)
export SRTELNET_MAX_CACHED=3

# Or via the systemd unit — add to the [Service] section:
Environment=SRTELNET_MAX_CACHED=3
```

To change the idle timeout:

```bash
export SRTELNET_CACHE_IDLE=300   # evict after 5 minutes idle
```

## Bandwidth capacity

Each frame is sent as ANSI escape sequences over TCP. Per-client bandwidth
by bucket width at 30 fps:

| Bucket | Avg frame size | Bandwidth per client |
|--------|---------------|---------------------|
| 40w    | ~14 KB        | ~0.4 MB/s           |
| 80w    | ~42 KB        | ~1.2 MB/s           |
| 120w   | ~87 KB        | ~2.6 MB/s           |
| 160w   | ~137 KB       | ~4.0 MB/s           |
| 200w   | ~210 KB       | ~6.3 MB/s           |

With a **40 Mbps upload** (~5 MB/s), maximum concurrent clients:

- ~4 clients at 80w
- ~2 clients at 120w
- ~1 client at 200w

The backpressure system handles oversubscription gracefully: slow clients
see frame drops (lower effective fps) instead of disconnects. But for the
best experience, keep concurrent streams within your upload budget.

With **1 Gbps fiber** (~125 MB/s), you could serve ~100 clients at 80w
simultaneously.

## systemd service

The unit file is at `deploy/srtelnet.service`. Key settings:

- `--prewarm 80`: pre-cache the 80-wide bucket on startup
- `LimitNOFILE=65536`: allow enough file descriptors for many connections
- `Restart=on-failure`: auto-restart on crash
- `NoNewPrivileges=true`: minimal hardening

### Updating after a code change

```bash
cd ~/second-reality-TELNET
git pull
sudo systemctl restart srtelnet
```

### Updating after a .service file change

```bash
sudo cp deploy/srtelnet.service /etc/systemd/system/srtelnet.service
sudo systemctl daemon-reload
sudo systemctl restart srtelnet
```

## LXC resource recommendations

| Connections | RAM   | CPU cores | Upload bandwidth |
|------------|-------|-----------|-----------------|
| 1-5        | 4 GB  | 2         | 40 Mbps         |
| 5-20       | 8 GB  | 2         | 100+ Mbps       |
| 20-100     | 8 GB  | 4         | 1 Gbps          |

CPU is rarely the bottleneck. Bandwidth and RAM are what matter.

## Troubleshooting

**RAM keeps growing after all clients disconnect**:
Check `state/status.txt` — the "buckets" section shows which caches are
populated. If idle buckets aren't being evicted, verify the eviction
timeout hasn't been set too high. A restart clears all caches.

**First client after restart is slow/choppy**:
Make sure `--prewarm 80` is in the systemd unit. Without it, the first
client triggers per-frame disk reads which can cause jitter.

**Swap usage climbing**:
Reduce `MAX_CACHED_BUCKETS` or `CACHE_IDLE_SECONDS`. With 8 GB RAM you
should rarely need swap if max cached buckets is 4 or fewer.

**Connection log getting large**:
Rotate it periodically:
```bash
mv state/connections.csv state/connections.$(date +%Y%m%d).csv
# The server will create a fresh one on the next connection
```
