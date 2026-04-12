#!/usr/bin/env bash
# stats.sh — quick diagnostic snapshot for second-reality-TELNET LXC
# Paste this onto the LXC and run: bash stats.sh
# Or from the repo: bash tools/stats.sh

SR_DIR="${SR_DIR:-$HOME/second-reality-TELNET}"

echo "============================================"
echo " second-reality-TELNET diagnostics"
echo " $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"

echo ""
echo "=== SERVICE STATUS ==="
systemctl is-active srtelnet.service 2>/dev/null || echo "(service not found)"
systemctl show srtelnet.service --property=ActiveState,SubState,MainPID 2>/dev/null

echo ""
echo "=== RECENT LOGS (last 20 lines) ==="
journalctl -u srtelnet.service --no-pager -n 20 2>/dev/null || echo "(no journal access)"

echo ""
echo "=== STATUS FILE ==="
if [ -f "$SR_DIR/state/status.txt" ]; then
    cat "$SR_DIR/state/status.txt"
else
    echo "(not found at $SR_DIR/state/status.txt)"
fi

echo ""
echo "=== LIFETIME COUNTER ==="
if [ -f "$SR_DIR/state/counter.txt" ]; then
    echo "connections ever: $(cat "$SR_DIR/state/counter.txt")"
else
    echo "(not found)"
fi

echo ""
echo "=== CONNECTION LOG (last 10) ==="
if [ -f "$SR_DIR/state/connections.csv" ]; then
    head -1 "$SR_DIR/state/connections.csv"
    tail -10 "$SR_DIR/state/connections.csv"
else
    echo "(not found)"
fi

echo ""
echo "=== MEMORY ==="
free -h

echo ""
echo "=== PYTHON PROCESS ==="
ps aux --sort=-%mem | grep '[p]ython.*srtelnet' || echo "(not running)"

echo ""
echo "=== TCP CONNECTIONS ==="
ss -s

echo ""
echo "=== TELNET CONNECTIONS (established) ==="
ss -tn state established '( sport = :2323 or sport = :23 )' 2>/dev/null | head -20
echo "count: $(ss -tn state established '( sport = :2323 or sport = :23 )' 2>/dev/null | tail -n +2 | wc -l)"

echo ""
echo "=== DISK ==="
df -h / 2>/dev/null
du -sh "$SR_DIR/frames" 2>/dev/null || echo "(frames dir not found)"
du -sh "$SR_DIR/state" 2>/dev/null || echo "(state dir not found)"

echo ""
echo "=== SWAP ==="
swapon --show 2>/dev/null || cat /proc/swaps

echo ""
echo "=== LOAD ==="
uptime
