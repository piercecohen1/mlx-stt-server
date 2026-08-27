#!/usr/bin/env bash
# Regression test: stale PID cleanup preserves an unrelated process whose
# PID was reused.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WRAPPER="$SCRIPT_DIR/stt-server"
TMP_DIR="$(mktemp -d)"
UNRELATED_PID=""

cleanup() {
    if [[ -n "$UNRELATED_PID" ]] && kill -0 "$UNRELATED_PID" 2>/dev/null; then
        kill "$UNRELATED_PID" 2>/dev/null || true
        wait "$UNRELATED_PID" 2>/dev/null || true
    fi
    rm -f "$TMP_DIR/server.pid" "$TMP_DIR/server.log"
    rmdir "$TMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

sleep 60 &
UNRELATED_PID=$!
printf '%s\n' "$UNRELATED_PID" > "$TMP_DIR/server.pid"

STT_SERVER_ENV_FILE="$TMP_DIR/no-env" \
STT_SERVER_PID_FILE="$TMP_DIR/server.pid" \
STT_SERVER_LOG_FILE="$TMP_DIR/server.log" \
STT_SERVER_PORT=28765 \
    "$WRAPPER" --stop

if ! kill -0 "$UNRELATED_PID" 2>/dev/null; then
    echo "FAIL: wrapper signaled unrelated process $UNRELATED_PID" >&2
    exit 1
fi
if [[ -e "$TMP_DIR/server.pid" ]]; then
    echo "FAIL: wrapper left the stale PID file in place" >&2
    exit 1
fi

echo "PASS: stale PID discarded and unrelated process preserved"
