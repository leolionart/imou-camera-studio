#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PY="$SCRIPT_DIR/imou_web_server.py"
PORT="${IMOU_STUDIO_PORT:-8766}"
HOST="127.0.0.1"
URL="http://${HOST}:${PORT}"
PID_FILE="/tmp/imou_camera_studio.pid"
LOG_FILE="/tmp/imou_camera_studio.log"

is_running() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

start_server() {
  if is_running; then
    echo "running ($URL)"
    return 0
  fi
  nohup /usr/bin/python3 "$SERVER_PY" --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in {1..20}; do
    if is_running; then
      echo "started ($URL)"
      return 0
    fi
    sleep 0.15
  done
  echo "failed to start server; check $LOG_FILE" >&2
  return 1
}

stop_server() {
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
  local pids
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill $pids 2>/dev/null || true
  fi
  echo "stopped"
}

ACTION="${1:-open}"
case "$ACTION" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  status)
    if is_running; then
      echo "running ($URL)"
    else
      echo "stopped"
      exit 1
    fi
    ;;
  open)
    start_server
    if [[ -d "/Applications/Imou Camera Studio.app" ]]; then
      open -a "/Applications/Imou Camera Studio.app"
    else
      open "$URL"
    fi
    ;;
  *)
    echo "Usage: $(basename "$0") [start|stop|status|open]" >&2
    exit 2
    ;;
esac
