#!/usr/bin/env bash
cd "$(dirname "$0")/.."
PIDFILE="data/logs/pondr.pid"
if [ ! -f "$PIDFILE" ]; then echo "no pidfile"; exit 0; fi
PID=$(cat "$PIDFILE")
if kill -0 "$PID" 2>/dev/null; then
  echo "stopping pid $PID"; kill "$PID"
  for i in 1 2 3 4 5; do sleep 1; kill -0 "$PID" 2>/dev/null || break; done
  kill -0 "$PID" 2>/dev/null && kill -9 "$PID" || true
fi
rm -f "$PIDFILE"
