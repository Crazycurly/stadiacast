#!/usr/bin/env bash
# Stop what tools/start_receiver.sh started: the receiver, its DNS/SNTP helpers and the packet
# capture. It stops the processes the run directories' pid files name, after checking that each
# one still is what its file says, and removes the files, stale ones included. Only when there is
# no pid file at all does it fall back to matching the process names, which stops every receiver
# and helper on this machine.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
RECEIVER='-m stadiacast( |$)|bin/stadiacast( |$)'
HELPERS='stadiacast[.]helpers[.](dns_sink|sntp_server)'
TCPDUMP='(^|/)tcpdump( |$)'
stopped=0
stale=0

# is_a PID PATTERN: PID is a running process whose command line matches PATTERN
is_a() {
  case $1 in '' | *[!0-9]*) return 1 ;; esac
  ps -p "$1" -o command= 2>/dev/null | grep -Eq -- "$2"
}

# stop_pid PID: SIGTERM, then SIGKILL when it is still there after 15 s
stop_pid() {
  kill "$1" 2>/dev/null || return 0
  stopped=$((stopped + 1))
  for _ in $(seq 1 75); do
    kill -0 "$1" 2>/dev/null || return 0
    sleep 0.2
  done
  echo "process $1 ignored SIGTERM for 15 s; killing it" >&2
  kill -9 "$1" 2>/dev/null
}

# stop_matching PATTERN: every process whose command line matches PATTERN, one after the other
stop_matching() {
  local pid
  for pid in $(pgrep -f -- "$1"); do stop_pid "$pid"; done
}

found=0
for pidfile in captures/*/server.pid captures/*/tcpdump.pid; do
  [ -f "$pidfile" ] || continue
  found=1
  case $pidfile in */server.pid) pattern=$RECEIVER ;; *) pattern=$TCPDUMP ;; esac
  pid=$(cat "$pidfile")
  if is_a "$pid" "$pattern"; then stop_pid "$pid"; else stale=$((stale + 1)); fi
  rm -f "$pidfile"
  # The receiver stops its own helpers; one of its run still there after it is gone is a leftover.
  if [ "$pattern" = "$RECEIVER" ]; then stop_matching "$HELPERS .*--log $(dirname "$pidfile")/"; fi
done

if [ "$found" = 0 ]; then
  stop_matching "$RECEIVER"
  stop_matching "$HELPERS"
fi

message="stopped $stopped process(es)"
if [ "$stale" -gt 0 ]; then message="$message, removed $stale stale pid file(s)"; fi
echo "$message"
