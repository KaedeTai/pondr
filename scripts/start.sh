#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
LOGDIR="data/logs"
mkdir -p "$LOGDIR"
PIDFILE="$LOGDIR/pondr.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "pondr already running (pid $(cat "$PIDFILE"))"; exit 0
fi
if [ -d ".venv" ]; then source .venv/bin/activate; fi
nohup python -m pondr > "$LOGDIR/stdout.log" 2> "$LOGDIR/stderr.log" &
echo $! > "$PIDFILE"
sleep 1
echo "started pid $(cat "$PIDFILE") — logs: $LOGDIR/{stdout,stderr}.log"
