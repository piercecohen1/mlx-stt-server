#!/usr/bin/env bash
set -euo pipefail

# Self-locate: resolve REPO to the repo root regardless of where this
# script lives inside the repo or what CWD it was invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

# Use the shipped sample unless overridden. samples/test.m4a is a small
# macOS `say` synthesized clip committed to the repo so the verification
# works anywhere without relying on a user's personal audio.
AUDIO="${AUDIO:-$REPO/samples/test.m4a}"
if [[ ! -f "$AUDIO" ]]; then
    echo "error: sample audio missing at $AUDIO" >&2
    echo "set AUDIO=/path/to/clip.m4a or regenerate samples/test.m4a" >&2
    exit 1
fi

# --- Token hygiene helpers --------------------------------------------------
# All auth-bearing curl calls go through mk_auth_conf + curl -K so the token
# never appears on an exec argv (and thus never in `ps -ef`). The test key
# also lives in a 0600 file, never an env var, mirroring the production
# design.

SRV=""
BIGWAV=""
TEST_KEY_FILE=""
TEST_ENV_FILE=""
CURL_CONFS=()

mk_auth_conf() {  # mk_auth_conf <key-file> → echoes path of a 0600 curl config
    local kf="$1" conf
    conf=$(mktemp -t mlx-stt-curl.XXXXXX)
    chmod 600 "$conf"
    printf 'header = "Authorization: Bearer %s"\n' "$(cat "$kf")" > "$conf"
    CURL_CONFS+=("$conf")
    printf '%s' "$conf"
}

assert_code() {  # assert_code <expected> <url> [curl-args...]
    local expected="$1" url="$2"; shift 2
    local got
    got=$(curl -sS -o /dev/null -w '%{http_code}' "$@" "$url")
    if [[ "$got" != "$expected" ]]; then
        echo "FAIL: $url → expected $expected, got $got" >&2
        return 1
    fi
    echo "OK:   $url → $got"
}

assert_exit_nonzero() {  # assert_exit_nonzero <command...>
    # Don't pin a specific exit code — uvicorn's exit on a lifespan
    # RuntimeError varies across versions (0.27 → 1, 0.35 → 3). We just
    # need "it refused to start."
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [[ "$got" == "0" ]]; then
        echo "FAIL: $* → expected non-zero exit, got 0" >&2
        return 1
    fi
    echo "OK:   $* → exit $got"
}

cleanup() {
    if [[ -n "$SRV" ]]; then
        kill "$SRV" 2>/dev/null || true
        wait "$SRV" 2>/dev/null || true
    fi
    for f in "${CURL_CONFS[@]:-}"; do
        [[ -n "$f" && -f "$f" ]] && { : > "$f"; rm -f "$f"; }
    done
    if [[ -n "$TEST_KEY_FILE" && -f "$TEST_KEY_FILE" ]]; then
        : > "$TEST_KEY_FILE"; rm -f "$TEST_KEY_FILE"
    fi
    if [[ -n "$TEST_ENV_FILE" && -f "$TEST_ENV_FILE" ]]; then
        rm -f "$TEST_ENV_FILE"  # env file has no secret
    fi
    if [[ -n "$BIGWAV" && -f "$BIGWAV" ]]; then
        rm -f "$BIGWAV"
    fi
    ./scripts/stt-server stop 2>/dev/null || true
}
trap cleanup EXIT

./scripts/stt-server stop || true
unset MLX_STT_API_KEY_FILE MLX_STT_REQUIRE_KEY

# ---------------------------------------------------------------------------
# 1. Backward compat: no key file, endpoints work unauth
# ---------------------------------------------------------------------------
# Point the server at a non-existent key file path so it falls through to
# "local mode, no auth" regardless of what's in the default location.
MLX_STT_API_KEY_FILE=/nonexistent/mlx-stt-server-key \
    python server.py --no-preload &
SRV=$!
sleep 1
assert_code 200 http://127.0.0.1:8765/healthz
assert_code 200 http://127.0.0.1:8765/v1/models
# FastAPI docs/openapi must be disabled — check all four auto-routes
assert_code 404 http://127.0.0.1:8765/docs
assert_code 404 http://127.0.0.1:8765/openapi.json
assert_code 404 http://127.0.0.1:8765/redoc
assert_code 404 http://127.0.0.1:8765/docs/oauth2-redirect
# Old disclosive root is gone
assert_code 404 http://127.0.0.1:8765/
kill "$SRV"; wait "$SRV" 2>/dev/null || true
SRV=""

# ---------------------------------------------------------------------------
# 2. --require-key with no key file → non-zero exit
# ---------------------------------------------------------------------------
MLX_STT_API_KEY_FILE=/nonexistent/mlx-stt-server-key \
    assert_exit_nonzero python server.py --require-key --no-preload

# ---------------------------------------------------------------------------
# 3. --require-key with too-short key → non-zero exit
# ---------------------------------------------------------------------------
SHORT_KEY_FILE=$(mktemp -t mlx-stt-test-key.XXXXXX)
chmod 600 "$SHORT_KEY_FILE"
printf 'tooshort' > "$SHORT_KEY_FILE"
MLX_STT_API_KEY_FILE="$SHORT_KEY_FILE" \
    assert_exit_nonzero python server.py --require-key --no-preload
rm -f "$SHORT_KEY_FILE"

# ---------------------------------------------------------------------------
# 3b. Key file with wrong mode (0644) → non-zero exit
#     (Hardening: the server refuses to load a world/group-readable key file.)
# ---------------------------------------------------------------------------
BAD_MODE_FILE=$(mktemp -t mlx-stt-test-key.XXXXXX)
openssl rand -hex 32 > "$BAD_MODE_FILE"
chmod 644 "$BAD_MODE_FILE"
MLX_STT_API_KEY_FILE="$BAD_MODE_FILE" \
    assert_exit_nonzero python server.py --require-key --no-preload
rm -f "$BAD_MODE_FILE"

# ---------------------------------------------------------------------------
# 3c. Entrypoint coverage: the require-key guard must fire regardless of
#     HOW the server is booted. Test all three paths explicitly — CLI flag,
#     env-var-only, and direct `uvicorn server:app` boot. A main()-only
#     guard would pass step 2 but fail one of these.
# ---------------------------------------------------------------------------
# env-var-only (no --require-key CLI flag) → must refuse
MLX_STT_API_KEY_FILE=/nonexistent/mlx-stt-server-key MLX_STT_REQUIRE_KEY=1 \
    assert_exit_nonzero python server.py --no-preload

# uvicorn server:app direct boot → must refuse
# (Use a distinct port so we don't collide with any server from earlier
# steps. uvicorn reads MLX_STT_REQUIRE_KEY from env at lifespan startup,
# raises RuntimeError, uvicorn exits non-zero.)
MLX_STT_API_KEY_FILE=/nonexistent/mlx-stt-server-key MLX_STT_REQUIRE_KEY=1 \
    assert_exit_nonzero uvicorn server:app \
        --host 127.0.0.1 --port 8767 --log-level critical

# ---------------------------------------------------------------------------
# 4. Auth enabled: protected endpoints 401, /healthz stays open
# ---------------------------------------------------------------------------
TEST_KEY_FILE=$(mktemp -t mlx-stt-test-key.XXXXXX)
chmod 600 "$TEST_KEY_FILE"
openssl rand -hex 32 > "$TEST_KEY_FILE"
MLX_STT_API_KEY_FILE="$TEST_KEY_FILE" MLX_STT_REQUIRE_KEY=1 \
    python server.py --no-preload &
SRV=$!
sleep 1

AUTH_CONF=$(mk_auth_conf "$TEST_KEY_FILE")
BAD_CONF=$(mktemp -t mlx-stt-curl.XXXXXX); chmod 600 "$BAD_CONF"
printf 'header = "Authorization: Bearer not-the-real-key"\n' > "$BAD_CONF"
CURL_CONFS+=("$BAD_CONF")

# /healthz is always open
assert_code 200 http://127.0.0.1:8765/healthz
# GET /v1/models — auth required
assert_code 401 http://127.0.0.1:8765/v1/models
assert_code 401 http://127.0.0.1:8765/v1/models -K "$BAD_CONF"
assert_code 200 http://127.0.0.1:8765/v1/models -K "$AUTH_CONF"
# POST /v1/audio/transcriptions — auth required (missing from earlier passes)
assert_code 401 http://127.0.0.1:8765/v1/audio/transcriptions \
    -X POST -F "file=@$AUDIO"
assert_code 401 http://127.0.0.1:8765/v1/audio/transcriptions \
    -X POST -F "file=@$AUDIO" -K "$BAD_CONF"
# POST /v1/audio/translations — auth required
assert_code 401 http://127.0.0.1:8765/v1/audio/translations \
    -X POST -F "file=@$AUDIO"
assert_code 401 http://127.0.0.1:8765/v1/audio/translations \
    -X POST -F "file=@$AUDIO" -K "$BAD_CONF"

# ---------------------------------------------------------------------------
# 5. Real transcription through auth — uses curl -K, token not in argv
# ---------------------------------------------------------------------------
curl -fsS -K "$AUTH_CONF" -X POST \
    http://127.0.0.1:8765/v1/audio/transcriptions \
    -F "file=@$AUDIO" \
    -F "model=CohereLabs/cohere-transcribe-03-2026" \
    -F "language=en" >/dev/null && echo "OK:   real transcription"

# ---------------------------------------------------------------------------
# 6. Upload size cap: 150 MB file → 413 from the ASGI middleware
# ---------------------------------------------------------------------------
BIGWAV=$(mktemp -t big.XXXXXX.wav)
dd if=/dev/zero of="$BIGWAV" bs=1m count=150 >/dev/null 2>&1
assert_code 413 http://127.0.0.1:8765/v1/audio/transcriptions \
    -X POST -K "$AUTH_CONF" -F "file=@$BIGWAV"
rm -f "$BIGWAV"; BIGWAV=""

kill "$SRV"; wait "$SRV" 2>/dev/null || true
SRV=""

# ---------------------------------------------------------------------------
# 7. scripts/stt-server: prove the env-file-first-source fix works AND that
#    the wrapper picks up MLX_STT_API_KEY_FILE from the env file, not the
#    user's shell environment. Use a non-default STT_SERVER_PORT to prove
#    env-file values actually override the wrapper defaults.
# ---------------------------------------------------------------------------
unset MLX_STT_API_KEY_FILE MLX_STT_REQUIRE_KEY STT_SERVER_PORT

TEST_ENV_FILE=$(mktemp -t mlx-stt-test-env.XXXXXX)
chmod 644 "$TEST_ENV_FILE"  # no secret in here
printf 'MLX_STT_API_KEY_FILE=%s\nMLX_STT_REQUIRE_KEY=1\nSTT_SERVER_PORT=8766\n' \
    "$TEST_KEY_FILE" > "$TEST_ENV_FILE"

STT_SERVER_ENV_FILE="$TEST_ENV_FILE" ./scripts/stt-server start
sleep 5
STT_SERVER_ENV_FILE="$TEST_ENV_FILE" ./scripts/stt-server status

# Prove the port override took effect — server is on 8766, not 8765
assert_code 200 http://127.0.0.1:8766/healthz
assert_code 401 http://127.0.0.1:8766/v1/models
assert_code 200 http://127.0.0.1:8766/v1/models -K "$AUTH_CONF"

STT_SERVER_ENV_FILE="$TEST_ENV_FILE" ./scripts/stt-server stop

echo "All checks passed."
