#!/usr/bin/env bash
# install-aliases.sh — add the `stt-server` shell alias to your ~/.zshrc
# (or ~/.bashrc) so you can run `stt-server --start`, `stt-server --stop`,
# etc. from any terminal without cd'ing into this repo first.
#
# Idempotent: re-running this script replaces the existing block in place
# instead of duplicating it. Safe to run multiple times, safe to run on
# a machine that's never had it.
#
# Usage:
#   ./scripts/install-aliases.sh          # detect shell rc, install
#   ./scripts/install-aliases.sh --bash   # force ~/.bashrc
#   ./scripts/install-aliases.sh --zsh    # force ~/.zshrc
#   STT_SERVER_RC=/path/to/rc ./scripts/install-aliases.sh   # explicit path

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TARGET_SCRIPT="$REPO_DIR/scripts/stt-server"

if [[ ! -x "$TARGET_SCRIPT" ]]; then
    echo "error: $TARGET_SCRIPT is missing or not executable" >&2
    echo "run:   chmod +x $TARGET_SCRIPT" >&2
    exit 1
fi

# Determine target rc file.
if [[ -n "${STT_SERVER_RC:-}" ]]; then
    RC_FILE="$STT_SERVER_RC"
elif [[ "${1:-}" == "--zsh" ]]; then
    RC_FILE="$HOME/.zshrc"
elif [[ "${1:-}" == "--bash" ]]; then
    RC_FILE="$HOME/.bashrc"
elif [[ -n "${ZSH_VERSION:-}" || "${SHELL##*/}" == "zsh" ]]; then
    RC_FILE="$HOME/.zshrc"
elif [[ -n "${BASH_VERSION:-}" || "${SHELL##*/}" == "bash" ]]; then
    RC_FILE="$HOME/.bashrc"
else
    echo "error: could not detect shell rc file." >&2
    echo "re-run with --zsh, --bash, or STT_SERVER_RC=<path>" >&2
    exit 1
fi

# Marker comments let us find and replace the block on re-run.
BEGIN_MARKER="# >>> mlx-stt-server aliases >>>"
END_MARKER="# <<< mlx-stt-server aliases <<<"

# Build the block as a single string. Double-quote the alias target
# (not single-quote) so variable interpolation happens at install time,
# which gives the alias body an absolute path. Single quotes + $HOME
# would work at runtime but would render red under
# zsh-syntax-highlighting because it can't stat an unexpanded path.
BLOCK="$BEGIN_MARKER
# Alias for $TARGET_SCRIPT
# Installed by scripts/install-aliases.sh — re-run after moving the repo.
alias stt-server=\"$TARGET_SCRIPT\"
$END_MARKER"

touch "$RC_FILE"

if grep -qF "$BEGIN_MARKER" "$RC_FILE"; then
    echo "install-aliases: updating existing block in $RC_FILE"
    # Delete the old block (inclusive of markers) via awk — portable
    # across macOS BSD sed and GNU sed, which disagree on -i syntax.
    TMP=$(mktemp)
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        $0 == begin { skip = 1; next }
        $0 == end   { skip = 0; next }
        !skip
    ' "$RC_FILE" > "$TMP"
    mv -f "$TMP" "$RC_FILE"
else
    echo "install-aliases: adding new block to $RC_FILE"
fi

# Append the new block, preceded by a blank line if the file doesn't
# already end in one (cosmetic).
if [[ -s "$RC_FILE" ]] && [[ "$(tail -c1 "$RC_FILE" | od -An -c | tr -d ' ')" != "\\n" ]]; then
    printf '\n' >> "$RC_FILE"
fi
printf '\n%s\n' "$BLOCK" >> "$RC_FILE"

echo
echo "install-aliases: done. Installed alias:"
echo "    stt-server → $TARGET_SCRIPT"
echo
echo "To use it in your current shell:"
echo "    source $RC_FILE"
echo
echo "Then:"
echo "    stt-server --start"
echo "    stt-server --status"
echo "    stt-server --stop"
