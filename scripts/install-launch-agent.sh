#!/usr/bin/env bash
# install-launch-agent.sh — make the server start automatically at login.
#
# Writes a launchd LaunchAgent that runs `stt-server --start` when you log
# in, then loads it (which also starts the server now if it isn't running).
# Idempotent: re-running replaces the agent in place. Re-run after moving
# the repo or switching Python interpreters — the plist embeds absolute
# paths resolved at install time, because launchd jobs run without your
# shell PATH (so `python` from pyenv/venv would not resolve at login).
#
# Usage:
#   ./scripts/install-launch-agent.sh              # install / update
#   ./scripts/install-launch-agent.sh --uninstall  # remove autostart
#
# The agent only runs the start command; it does not supervise the server.
# `stt-server --stop` keeps the server stopped until the next login.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TARGET_SCRIPT="$SCRIPT_DIR/stt-server"

LABEL="com.mlx-stt-server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG_DIR="$HOME/.cache/mlx-stt-server"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "install-launch-agent: removed $PLIST"
    echo "install-launch-agent: the server will no longer start at login (a running server is left running)"
    exit 0
fi

if [[ ! -x "$TARGET_SCRIPT" ]]; then
    echo "error: $TARGET_SCRIPT is missing or not executable" >&2
    exit 1
fi

# Resolve the Python interpreter the same way scripts/stt-server does, but
# pin the result into the plist so the login-time launch is deterministic.
if [[ -n "${STT_SERVER_PYTHON:-}" ]]; then
    PYTHON_BIN="$STT_SERVER_PYTHON"
elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_DIR/.venv/bin/python"
elif [[ -x "$REPO_DIR/venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_DIR/venv/bin/python"
else
    PYTHON_BIN="$(command -v python || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
    echo "error: could not resolve a Python interpreter for the server" >&2
    echo "set STT_SERVER_PYTHON=/path/to/python and re-run" >&2
    exit 1
fi
# Resolve through pyenv shims / symlinks to the concrete interpreter, so a
# later `pyenv global` change can't silently switch the server to a Python
# that lacks its dependencies.
PYTHON_BIN="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"

# launchd starts jobs with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin).
# The server shells out to ffmpeg for M4A/AAC decoding, so pin its
# directory (resolved from the installing shell) into the job's PATH.
FFMPEG_DIR=""
if command -v ffmpeg >/dev/null 2>&1; then
    FFMPEG_DIR="$(dirname "$(command -v ffmpeg)")"
else
    echo "warning: ffmpeg not found on PATH — the login-started server will fail on M4A/AAC audio" >&2
fi
LAUNCHD_PATH="${FFMPEG_DIR:+$FFMPEG_DIR:}/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$TARGET_SCRIPT</string>
        <string>--start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>STT_SERVER_PYTHON</key>
        <string>$PYTHON_BIN</string>
        <key>PATH</key>
        <string>$LAUNCHD_PATH</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <!-- The wrapper daemonizes the server via nohup and exits. Without
         AbandonProcessGroup, launchd kills the job's leftover process
         group on exit, taking the server down with it. -->
    <key>AbandonProcessGroup</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchd.log</string>
</dict>
</plist>
EOF

# Reload so an updated plist takes effect; bootstrap runs the job once,
# which is a no-op if the server is already up.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "install-launch-agent: installed $PLIST"
echo "install-launch-agent:   runs:   $TARGET_SCRIPT --start"
echo "install-launch-agent:   python: $PYTHON_BIN"
echo "install-launch-agent:   log:    $LOG_DIR/launchd.log"
echo "install-launch-agent: the server now starts automatically at login"
