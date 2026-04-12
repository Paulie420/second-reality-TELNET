#!/usr/bin/env bash
# stats25.sh — fits on one 80x25 screen
SR="$HOME/second-reality-TELNET"
PY=$(ps aux | grep '[p]ython.*srtelnet' | awk '{printf "%s RSS=%.0fMB CPU=%s", $11, $6/1024, $3}')
MEM=$(free -m | awk '/Mem:/{printf "%dMB / %dMB (%.0f%%)", $3, $2, $3/$2*100}')
SWAP=$(free -m | awk '/Swap:/{printf "%dMB / %dMB", $3, $2}')
TELNET=$(ss -tn state established '( sport = :2323 or sport = :23 )' 2>/dev/null | tail -n +2 | wc -l)
SVC=$(systemctl is-active srtelnet.service 2>/dev/null)
LOAD=$(awk '{print $1"/"$2"/"$3}' /proc/loadavg)
UP=$(uptime -p 2>/dev/null | sed 's/up //')

echo "======== second-reality-TELNET ========"
echo " service: $SVC    load: $LOAD"
echo " uptime:  $UP"
echo " memory:  $MEM"
echo " swap:    $SWAP"
echo " telnet:  $TELNET active TCP connections"
echo "========================================"
if [ -f "$SR/state/status.txt" ]; then
    # print status.txt but skip the header lines we already covered
    grep -E "active conn|peak conn|session|lifetime|cache policy|^\s+\d+w:|buckets:" "$SR/state/status.txt"
fi
echo "----------------------------------------"
echo " last 5 connections:"
if [ -f "$SR/state/connections.csv" ]; then
    tail -5 "$SR/state/connections.csv" | awk -F, '{printf " %-11s %3sw %5ss %s\n", $1, $3, $7, $8}'
else
    echo " (no log yet)"
fi
echo "========================================"
