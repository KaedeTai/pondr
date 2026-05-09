#!/usr/bin/env bash
cd "$(dirname "$0")/.."
PIDFILE="data/logs/pondr.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "pondr running pid $(cat "$PIDFILE")"
  curl -s http://127.0.0.1:8090/api/state | python3 -m json.tool 2>/dev/null | head -30 || echo "(dashboard not reachable)"
else
  echo "pondr not running"
fi
