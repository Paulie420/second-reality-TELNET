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
    grep -E "active conn|peak conn|session|lifetime|cache policy|[0-9]+w:|buckets:" "$SR/state/status.txt"
fi
echo "----------------------------------------"
echo " last 5 connections (  date   start/end    dur  pk  sk  out  )"
if [ -f "$SR/state/connections.csv" ]; then
    # Columns: 1=ts 3=start_bucket 7=duration 8=outcome
    # 9=peak_buf_kb 10=seek_count 11=final_bucket (added with the
    # per-session telemetry work; see docs/performance-tuning.md).
    # Rows written by pre-instrumentation builds lack cols 9-11; awk
    # prints a "-" placeholder so the table stays aligned either way.
    tail -5 "$SR/state/connections.csv" | grep -v '^timestamp' | awk -F, '
        {
            pk  = ($9  == "" ? "-" : $9"k");
            sk  = ($10 == "" ? "-" : $10);
            fin = ($11 == "" ? $3 : $11);
            # Show start->end bucket, compact if they match.
            if (fin == $3) bucket = $3"w"; else bucket = $3">"fin"w";
            printf " %-11s %-8s %4ss %5s %2s %s\n", $1, bucket, $7, pk, sk, $8;
        }'
else
    echo " (no log yet)"
fi
echo "========================================"
