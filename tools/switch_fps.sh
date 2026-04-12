#!/usr/bin/env bash
# tools/switch_fps.sh — flip the running srtelnet.service between 20 and
# 30 fps on the LXC, AND archive the non-active frame set to reclaim
# disk automatically after each switch. Runs on the LXC itself (sudo).
#
# The script writes a complete systemd unit for the requested fps
# target and restarts the service. It does NOT modify any source code;
# the welcome-screen reads fps and frame count from live runtime state,
# so the display updates itself on restart.
#
# Layout assumed on disk:
#
#   $ROOT/frames-20fps/                - 20fps truecolor, live when
#                                        serving 20fps
#   $ROOT/frames-30fps/                - 30fps truecolor, live when
#                                        serving 30fps (ephemeral
#                                        otherwise, archived to save disk)
#   $ROOT/archives/frames-20fps.tar.zst
#   $ROOT/archives/frames-30fps.tar.zst
#
# At any given time, one of {frames-20fps/, frames-30fps/} is live on
# disk and the OTHER lives only in its compressed archive. Flipping
# fps = unpack the target's archive (if needed), restart the service,
# then archive + delete the now-inactive uncompressed dir.
#
# One-time migration: if the legacy $ROOT/frames/ dir exists (30fps
# truecolor from before this tooling landed), the first invocation
# renames it to $ROOT/frames-30fps/ before doing anything else.
#
# Usage:
#   sudo tools/switch_fps.sh 20       # flip to 20fps truecolor (default)
#   sudo tools/switch_fps.sh 30       # flip to 30fps truecolor
#   sudo tools/switch_fps.sh status   # show what is currently serving
#
# Disk math (reference):
#   30fps truecolor uncompressed      ~14 GB
#   20fps truecolor uncompressed      ~9.4 GB
#   each compressed (zstd -6)         ~2.5-3.5 GB
#   archive time on 4 threads         ~2-3 min
#   unpack time on 1 thread (zstd)    ~1-2 min
#
set -euo pipefail

# Script lives at $ROOT/tools/, project root is one level up.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT="/etc/systemd/system/srtelnet.service"
VENV_PY="$ROOT/.venv/bin/python"

FRAMES_20="$ROOT/frames-20fps"
FRAMES_30="$ROOT/frames-30fps"
ARCHIVE_DIR="$ROOT/archives"
ARCHIVE_20="$ARCHIVE_DIR/frames-20fps.tar.zst"
ARCHIVE_30="$ARCHIVE_DIR/frames-30fps.tar.zst"
LEGACY_FRAMES="$ROOT/frames"   # pre-tooling 30fps dir name

# Number of threads for zstd when archiving. Capped at 4 by default so
# we don't spike CPU on the host (other workloads may be running).
# Override with SRTELNET_ZSTD_THREADS=N to tune.
ZSTD_THREADS="${SRTELNET_ZSTD_THREADS:-4}"

# User / group for the systemd service. Priority:
#   1. SRTELNET_USER / SRTELNET_GROUP env vars (explicit override)
#   2. $SUDO_USER (the human who invoked `sudo tools/switch_fps.sh ...`)
#   3. the current user (id -un)
# The group defaults to the user name if SRTELNET_GROUP is unset, which
# matches the default behavior of `useradd` on most distros.
SRTELNET_USER="${SRTELNET_USER:-${SUDO_USER:-$(id -un)}}"
SRTELNET_GROUP="${SRTELNET_GROUP:-$SRTELNET_USER}"

usage() {
    cat <<EOF
usage: sudo $(basename "$0") <20|30|status|help>

  20      flip to 20fps truecolor (current project default).
          Unpacks $ARCHIVE_20 if $FRAMES_20/ missing.
          Archives $FRAMES_30/ (if present) to $ARCHIVE_30 and deletes it.

  30      flip to 30fps truecolor.
          Unpacks $ARCHIVE_30 if $FRAMES_30/ missing.
          Archives $FRAMES_20/ (if present) to $ARCHIVE_20 and deletes it.

  status  print the fps, frames path, and active state of the service.

  help    this message.

One-time: if $LEGACY_FRAMES/ exists (pre-tooling 30fps bake), it is
renamed to $FRAMES_30/ on first run.
EOF
    exit 2
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "[error] $(basename "$0") must be run as root (use sudo)"
        exit 1
    fi
}

require_tools() {
    for t in zstd tar systemctl; do
        if ! command -v "$t" >/dev/null 2>&1; then
            echo "[error] required tool missing: $t"
            exit 1
        fi
    done
}

log() { echo "[switch] $*"; }

# Rename legacy $ROOT/frames/ (30fps) to $FRAMES_30/ if needed.
migrate_legacy() {
    if [ -d "$LEGACY_FRAMES" ] && [ ! -d "$FRAMES_30" ]; then
        log "legacy dir $LEGACY_FRAMES detected; renaming to $FRAMES_30"
        mv "$LEGACY_FRAMES" "$FRAMES_30"
    fi
}

# Archive $1 (a bucket-root dir) to $2 (a .tar.zst file). Skip if the
# archive already exists (avoids expensive re-archiving on every flip).
ensure_archived() {
    local src_dir="$1"
    local dest="$2"
    if [ -f "$dest" ]; then
        log "archive already exists: $dest ($(du -h "$dest" | cut -f1))"
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    log "archiving $src_dir -> $dest ($ZSTD_THREADS threads, level 6)"
    log "  (this takes ~2-3 min, service already restarted so users unaffected)"
    local tmp="$dest.tmp"
    if ! tar -C "$src_dir" -cf - . | zstd -T"$ZSTD_THREADS" -6 -o "$tmp"; then
        rm -f "$tmp"
        echo "[error] archive failed; $src_dir left in place"
        return 1
    fi
    mv "$tmp" "$dest"
    log "archive done: $(du -h "$dest" | cut -f1)"
}

# Unpack $1 (a .tar.zst) into $2 (a fresh dir). Dir must not already exist.
unpack_archive() {
    local src="$1"
    local dest_dir="$2"
    if [ ! -f "$src" ]; then
        echo "[error] archive missing: $src"
        return 1
    fi
    log "unpacking $src -> $dest_dir"
    log "  (this takes ~1-2 min)"
    mkdir -p "$dest_dir"
    if ! zstd -dc "$src" | tar -C "$dest_dir" -xf -; then
        rm -rf "$dest_dir"
        echo "[error] unpack failed"
        return 1
    fi
    log "unpack done: $(du -sh "$dest_dir" | cut -f1)"
}

write_unit_20fps() {
    cat >"$UNIT" <<EOF
[Unit]
Description=second-reality-TELNET server (stream Second Reality over telnet)
Documentation=https://github.com/Paulie420/second-reality-TELNET
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SRTELNET_USER
Group=$SRTELNET_GROUP
WorkingDirectory=$ROOT
# 20fps truecolor. Generated by tools/switch_fps.sh — edit via that
# script so fps switches stay atomic and the welcome-screen stats stay
# accurate. Do not hand-edit.
ExecStart=$VENV_PY -m srtelnet.server \\
    --port 2323 \\
    --frames $FRAMES_20 \\
    --fps 20 \\
    --max-frames 10163 \\
    --skip 897:1117
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

write_unit_30fps() {
    cat >"$UNIT" <<EOF
[Unit]
Description=second-reality-TELNET server (stream Second Reality over telnet)
Documentation=https://github.com/Paulie420/second-reality-TELNET
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SRTELNET_USER
Group=$SRTELNET_GROUP
WorkingDirectory=$ROOT
# 30fps truecolor. Generated by tools/switch_fps.sh — edit via that
# script so fps switches stay atomic and the welcome-screen stats stay
# accurate. Do not hand-edit.
ExecStart=$VENV_PY -m srtelnet.server \\
    --port 2323 \\
    --frames $FRAMES_30 \\
    --fps 30
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

reload_and_restart() {
    log "systemctl daemon-reload"
    systemctl daemon-reload
    log "systemctl restart srtelnet.service"
    systemctl restart srtelnet.service
    sleep 2
    journalctl -u srtelnet.service -n 30 --no-pager 2>/dev/null | \
        grep -E 'playback skips|listening on|bucket [0-9]+ indexed' | \
        tail -15 | sed 's/^/  /' || true
    echo
    if systemctl is-active --quiet srtelnet.service; then
        log "srtelnet.service is active ✓"
    else
        echo "[error] srtelnet.service is NOT active after restart — check journalctl"
        exit 1
    fi
}

show_status() {
    if [ ! -f "$UNIT" ]; then
        echo "[status] no unit file at $UNIT"
        return
    fi
    echo "[status] unit file: $UNIT"
    grep -E '^ExecStart=|--fps|--frames' "$UNIT" | sed 's/^/  /'
    echo
    echo "[status] active: $(systemctl is-active srtelnet.service 2>/dev/null || echo unknown)"
    echo
    echo "[status] on-disk state:"
    for d in "$FRAMES_20" "$FRAMES_30" "$LEGACY_FRAMES"; do
        if [ -d "$d" ]; then
            echo "  $(du -sh "$d" 2>/dev/null | cut -f1)  $d/"
        fi
    done
    for a in "$ARCHIVE_20" "$ARCHIVE_30"; do
        if [ -f "$a" ]; then
            echo "  $(du -h "$a" | cut -f1)  $a"
        fi
    done
    echo
    echo "[status] recent buckets indexed:"
    journalctl -u srtelnet.service -n 50 --no-pager 2>/dev/null | \
        grep -E 'bucket [0-9]+ indexed' | tail -9 | sed 's/^/  /' || \
        echo "  (no recent log lines)"
}

flip_to_20() {
    require_root
    require_tools
    migrate_legacy

    # Ensure 20fps frames are present.
    if [ ! -d "$FRAMES_20" ]; then
        unpack_archive "$ARCHIVE_20" "$FRAMES_20"
    fi

    log "writing 20fps unit file"
    write_unit_20fps
    reload_and_restart

    # Archive the now-inactive 30fps set to reclaim disk.
    if [ -d "$FRAMES_30" ]; then
        if ensure_archived "$FRAMES_30" "$ARCHIVE_30"; then
            log "removing uncompressed $FRAMES_30 ($(du -sh "$FRAMES_30" | cut -f1) reclaimed)"
            rm -rf "$FRAMES_30"
        else
            log "WARNING: archive of $FRAMES_30 failed; leaving uncompressed copy in place"
        fi
    fi

    echo
    show_status
}

flip_to_30() {
    require_root
    require_tools
    migrate_legacy

    # Ensure 30fps frames are present.
    if [ ! -d "$FRAMES_30" ]; then
        unpack_archive "$ARCHIVE_30" "$FRAMES_30"
    fi

    log "writing 30fps unit file"
    write_unit_30fps
    reload_and_restart

    # Archive the now-inactive 20fps set to reclaim disk.
    if [ -d "$FRAMES_20" ]; then
        if ensure_archived "$FRAMES_20" "$ARCHIVE_20"; then
            log "removing uncompressed $FRAMES_20 ($(du -sh "$FRAMES_20" | cut -f1) reclaimed)"
            rm -rf "$FRAMES_20"
        else
            log "WARNING: archive of $FRAMES_20 failed; leaving uncompressed copy in place"
        fi
    fi

    echo
    show_status
}

case "${1:-}" in
    20)     flip_to_20 ;;
    30)     flip_to_30 ;;
    status) show_status ;;
    help|-h|--help) usage ;;
    "")     usage ;;
    *)      echo "[error] unknown mode: $1"; usage ;;
esac
