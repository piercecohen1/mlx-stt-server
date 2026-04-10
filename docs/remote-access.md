# Plan: Bearer-token auth + Cloudflare Tunnel for remote access

## Context

`server.py` currently has no authentication — the "API key" field in any
OpenAI-compatible client is cosmetic, and the server only binds to
`127.0.0.1`, which is fine for localhost dictation but a non-starter the
moment it's reachable from the internet. This plan adds bearer-token auth
and the hardening needed to safely expose the server through a Cloudflare
Tunnel, so you can run it on one always-on host (e.g. a Mac Mini) and hit
it from your laptop and phone without duplicating the setup per machine.

Goal: a remote-reachable endpoint that is (a) not publicly open, (b)
requires a strong secret to transcribe, (c) leaves the local-only flow
unchanged when no key is configured, and (d) involves zero router /
firewall configuration on the host.

**Non-goals:** per-user auth, Cloudflare Access SSO (most dictation clients
only send one credential header, so Access service tokens which need two
custom headers would lock them out), rate limiting (deferred), live key
rotation (restart is fine), and always-on via launchd on the host
(`scripts/stt-server` is sufficient).

This plan went through seven rounds of Codex review (`-s read-only`,
severity-tagged CRITICAL/HIGH/MEDIUM/LOW). The substantive design
decisions — file-based key loading with `os.open(O_NOFOLLOW)` + fstat +
uid/mode check, auth as an ASGI middleware that runs before Starlette's
form parser, a pre-parse Content-Length body-size cap, FastAPI
`/docs`/`/redoc`/`/openapi.json` disablement, symlink-safe atomic writes
for the key file — all came out of those iterations. Don't "simplify"
them back without understanding why they're that shape.

## Architecture

```
Client (laptop/phone)
        │   https://cohere.<domain>
        │   Authorization: Bearer <token>
        ▼
Cloudflare edge ── TLS terminates, DDoS, edge logs ──
        │
        │   encrypted QUIC to origin connector
        ▼
cloudflared (tunneled host, runs as launchd service)
        │
        │   http://127.0.0.1:8765     (loopback only)
        ▼
server.py  ── validates Authorization: Bearer (constant-time) ──
```

**Why bearer token in the origin, not Cloudflare Access:** Access service
tokens require two custom headers (`CF-Access-Client-Id` + `CF-Access-Client-
Secret`). Most dictation clients' OpenAI-compatible config only exposes one credential
slot, mapped to `Authorization`, so Access would lock out the primary
client. Tunnel alone still gives us: no open ports, TLS at edge, origin on
loopback, automatic HTTPS cert.

**Layers of defense:**
1. No inbound ports on the host (cloudflared makes an outbound connection)
2. Server binds `127.0.0.1` only → even on the host's LAN, nothing but
   cloudflared on the same host can reach it
3. Server rejects any request without a valid `Authorization: Bearer`
   when a key file is present → even if someone got on the host, they
   still need the contents of `~/.config/mlx-stt-server/api-key`
4. Footgun guard: `--require-key` refuses to start with a blank/unset key,
   independent of bind address. The deployment always sets it.

## Changes

### 1. `server.py` — auth + hardening (single cohesive edit)

**Imports** — add `secrets`, `Depends`, `Header`. Also
`starlette.types.{ASGIApp, Receive, Scope, Send}` for the body-size
middleware.

**App construction** — disable docs/openapi entirely (they leak schema, and
we don't need them for a single programmatic client):
```python
app = FastAPI(
    title="mlx-audio OpenAI-compatible STT",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
```

**Remove `CORSMiddleware` entirely.** Dictation clients are not browsers; dropping
CORS eliminates the "phishing page in the user's browser tries to use the
tunnel endpoint" class of attack. If we ever need browser access, re-add
with an explicit origin list and a reason comment.

**Key loading is FILE-BASED, not env-var.** Storing the live token in an
environment variable means it lands in the process environment of the
uvicorn worker (and any child it spawns), where it's visible to `ps -E` /
`ps -ewww` for same-uid processes, and can get captured by macOS
diagnostics (`sysdiagnose`, crash reports, spindump). Reading it directly
from a 0600 file bypasses all of those exposure paths.

The load path is **TOCTOU-safe**: we `os.open()` once with `O_NOFOLLOW`
(fails on symlinks), then `fstat()` the resulting fd to check mode and
owner, then read from the same fd. No second `open()` on the pathname.

```python
DEFAULT_KEY_FILE = os.path.expanduser("~/.config/mlx-stt-server/api-key")

def _load_api_key() -> Optional[str]:
    path = os.environ.get("MLX_STT_API_KEY_FILE") or DEFAULT_KEY_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        # Only FileNotFoundError → local mode. Every other OSError
        # (PermissionError, ELOOP from O_NOFOLLOW, ENOTDIR, EISDIR, etc.)
        # is fatal: the user intended a key file to be there, so
        # silently disabling auth would be a footgun.
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if st.st_uid != os.getuid():
            raise RuntimeError(
                f"key file {path} owned by uid {st.st_uid}, "
                f"expected {os.getuid()} (refuse to load foreign file)"
            )
        if st.st_mode & 0o077:
            raise RuntimeError(
                f"key file {path} is mode {oct(st.st_mode & 0o777)}; "
                f"expected 0600 (owner read/write only)"
            )
        raw = f.read().strip()
    return raw.decode("utf-8") if raw else None

_API_KEY: Optional[str] = _load_api_key()
```

Any non-`FileNotFoundError` propagates as an unhandled exception at
module import time — uvicorn will fail to start, which is the correct
behavior for "the user has a key file but something's wrong with it."
The `O_NOFOLLOW` flag defends against symlink swaps in the config dir,
the `st_uid` check catches a foreign-owned file being placed at a path
that happens to match, and the combined `fstat`-on-open-fd eliminates
the TOCTOU race between stat and open.

The path is configurable via `MLX_STT_API_KEY_FILE` (non-secret — just
a path). See the runbook below for the exact symlink-safe creation
command (same-directory `mktemp` → `chmod 600` → atomic `mv -f`). Blank
files are treated as unset.

**Validation happens in `lifespan`, not `main()`**, so `uvicorn server:app`
direct invocation still honors the guard via `MLX_STT_REQUIRE_KEY=1` in
the environment:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    require_key = os.environ.get("MLX_STT_REQUIRE_KEY", "").lower() in (
        "1", "true", "yes",
    )
    if require_key and not _API_KEY:
        raise RuntimeError(
            "MLX_STT_REQUIRE_KEY=1 but no key loaded from "
            f"{os.environ.get('MLX_STT_API_KEY_FILE') or DEFAULT_KEY_FILE}"
        )
    if _API_KEY is not None and len(_API_KEY) < 32:
        raise RuntimeError("API key must be at least 32 characters")
    print(f"auth: {'enabled' if _API_KEY else 'disabled'}", flush=True)
    # ... existing preload logic
    yield
```

Print exactly one line: `auth: enabled` or `auth: disabled`. Never log the
key itself, not even a prefix, not even the file contents.

**Auth via ASGI middleware, NOT FastAPI dependency.** This is the v5
security fix. FastAPI resolves body parameters (like
`file: UploadFile = File(...)`) by calling `await request.form()`, which
runs Starlette's form parser, before the route's `dependencies=[...]`
list is resolved in the current FastAPI stack (verified against
fastapi 0.129.0 `routing.py:361`). That means a `Depends(verify_api_key)`
approach would still let an unauth attacker cause the server to spool
the full multipart body (up to the 100 MB middleware cap) before
returning 401 — wasted disk, wasted time, wasted bandwidth, and a 401
that comes AFTER a significant compute hit. A pure-ASGI middleware runs
strictly before the router, before any body parameter resolution.

```python
UNAUTH_PATHS: frozenset[str] = frozenset({"/healthz"})

class AuthMiddleware:
    """Reject requests without a valid Bearer token before routing.

    Runs before the router, so unauthorized requests do not reach
    Starlette's form parser (which would otherwise spool multipart
    bodies to disk for `POST /v1/audio/transcriptions`).
    """

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        # Cache the bytes form of the key for constant-time compare.
        self._key_bytes = api_key.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") in UNAUTH_PATHS:
            return await self.app(scope, receive, send)

        supplied: Optional[bytes] = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                if value[:7].lower() == b"bearer ":
                    supplied = value[7:].strip()
                break

        if supplied is None or not secrets.compare_digest(
            supplied, self._key_bytes
        ):
            # One generic 401 for both missing and invalid — don't leak
            # state. WWW-Authenticate per RFC 6750.
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"Unauthorized\n",
            })
            return

        await self.app(scope, receive, send)
```

Registration (module scope, after `app = FastAPI(...)`):
```python
# Middleware runs in reverse-registration order (LIFO). We want
# auth to run first for an incoming request, so register it LAST.
# MaxBodySize inner → Auth outer → request flows Auth → Size → router.
app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_UPLOAD_BYTES)
if _API_KEY is not None:
    app.add_middleware(AuthMiddleware, api_key=_API_KEY)
```
When `_API_KEY is None` (local mode), `AuthMiddleware` is NEVER
registered — the middleware stack is just `MaxBodySize → router`, which
preserves backward compatibility for users without auth configured.

There is NO `Depends(verify_api_key)` anywhere. No per-route dependency
list. The middleware owns auth entirely.

**Routes** — replace the disclosive `/` with `/healthz`, delete the old
root handler, keep the route handlers otherwise unchanged:
```python
@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")

# delete the existing @app.get("/") and its handler

@app.get("/v1/models")
async def list_models(): ...

@app.post("/v1/audio/transcriptions")
async def create_transcription(...): ...

@app.post("/v1/audio/translations")
async def create_translation(...): ...
```
`/healthz` stays unauth via the `UNAUTH_PATHS` allowlist so
`stt-server --status` and cloudflared can probe it without the key. It
returns nothing but `ok` — no service name, no loaded models, no
endpoint list.

**Upload size cap via ASGI middleware** — Starlette parses the multipart
body BEFORE the route handler runs, so a byte counter inside
`create_transcription` is too late: the full file is already spooled to
disk by the form parser. The only place early enough is an ASGI middleware
that inspects `Content-Length` on the raw scope headers and short-circuits
with a 413 before Starlette sees the body.

```python
MAX_UPLOAD_BYTES = int(os.environ.get("MLX_STT_MAX_UPLOAD_MB", "100")) * 1024 * 1024

class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        break
                    if length > self.max_bytes:
                        await send({
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [
                                (b"content-type", b"text/plain; charset=utf-8"),
                            ],
                        })
                        await send({
                            "type": "http.response.body",
                            "body": b"Payload too large\n",
                        })
                        return
                    break
        await self.app(scope, receive, send)

app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_UPLOAD_BYTES)
```

This catches any client that honestly declares `Content-Length` (curl,
requests, the OpenAI SDK, every dictation client I'm aware of — all
do). Chunked transfer without `Content-Length` is a theoretical bypass
but out of scope for a single-user dictation endpoint; Cloudflare's
edge enforces its own 100 MB upload limit on the free tier anyway,
which is the outer guardrail.

Default cap is 100 MB (well above any dictation snippet — the shipped
`samples/test.m4a` is ~28 KB). Override with `MLX_STT_MAX_UPLOAD_MB`.
The in-handler `await file.read()` path is otherwise unchanged; no more
cosmetic byte counting inside the route.

**Sanitize client-facing error strings** — current code leaks exception text
via `HTTPException(status_code=500, detail=f"Transcription failed: {e}")`.
Change to generic messages client-side, log details to stderr server-side:
```python
except Exception as e:
    print(f"[mlx-stt-server] load failed: {e}", file=sys.stderr, flush=True)
    raise HTTPException(status_code=400, detail="Model unavailable")

except Exception as e:
    print(f"[mlx-stt-server] transcribe failed: {e}", file=sys.stderr, flush=True)
    raise HTTPException(status_code=500, detail="Transcription failed")
```

**`main()` flags** — add `--require-key` (bool). Keep `--host`, `--port`,
`--model`, `--no-preload`. When `--require-key` is passed, set
`os.environ["MLX_STT_REQUIRE_KEY"] = "1"` *before* calling `uvicorn.run`,
so the lifespan check reads it at startup. This is also why the guard
lives in lifespan, not main(): `uvicorn server:app` direct invocation
still honors `MLX_STT_REQUIRE_KEY=1` set in the environment (e.g. via the
`~/.config/mlx-stt-server/env` file sourced by `scripts/stt-server`).

### 2. `scripts/stt-server` — source env file FIRST, switch health probe

Move env-file sourcing to the very top, right after `set -euo pipefail` and
the `SCRIPT_DIR`/`REPO_DIR` derivation, so any var in the file — including
`STT_SERVER_PORT`, `STT_SERVER_PYTHON`, `MLX_STT_API_KEY_FILE` (path only,
not secret), `MLX_STT_REQUIRE_KEY` — can override the subsequent defaults.
**The actual bearer token never enters the shell environment**; it stays in
the 0600 file pointed to by `MLX_STT_API_KEY_FILE` (default
`~/.config/mlx-stt-server/api-key`) and is read directly by `server.py`:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Source env file BEFORE deriving PORT/PYTHON_BIN/etc. so overrides win.
ENV_FILE="${STT_SERVER_ENV_FILE:-$HOME/.config/mlx-stt-server/env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

PORT="${STT_SERVER_PORT:-8765}"
# ... rest of existing derivations
```

In `EXTRA_ARGS` setup, append `--require-key` when the env file asks for
it. Note the var is `MLX_STT_REQUIRE_KEY` (read by both the wrapper and
the server's lifespan — single source of truth):
```bash
if [[ "${MLX_STT_REQUIRE_KEY:-}" == "1" ]]; then
    EXTRA_ARGS+=(--require-key)
fi
```

Change `cmd_status`'s curl probe from `/v1/models` (now protected) to the
new unauth endpoint:
```bash
if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "mlx-stt-server: health OK"
else
    echo "mlx-stt-server: health FAIL (model may still be loading — retry in a few seconds)"
    return 1
fi
```

### 3. `.env.example` (new file)

```bash
# ── Non-secrets only. The actual bearer token lives in a 0600 file at
# ── the path below (default ~/.config/mlx-stt-server/api-key), NOT in this
# ── sourced env file, because sourcing would put the secret in the
# ── process environment of every child.

# Override the default key-file path if you want. Leave commented to use
# ~/.config/mlx-stt-server/api-key.
# MLX_STT_API_KEY_FILE=

# Set to 1 to refuse startup when no key file is found or it's empty.
# ALWAYS set this on the tunneled host. It's the footgun
# guard against accidentally booting an unauth server that the tunnel
# would cheerfully expose. Read by the FastAPI lifespan handler, so it
# applies to both `python server.py --require-key` and direct
# `uvicorn server:app` invocations.
MLX_STT_REQUIRE_KEY=1

# Optional overrides (all have sensible defaults):
# STT_SERVER_PORT=8765
# STT_SERVER_MODEL=CohereLabs/cohere-transcribe-03-2026
# STT_SERVER_PYTHON=
# MLX_STT_MAX_UPLOAD_MB=100
```

Because this file holds no secrets, it can be `chmod 644` and, if the
user wants, checked into a private dotfiles repo. The actual key file at
`~/.config/mlx-stt-server/api-key` must always be `chmod 600` — the server
refuses to start otherwise.

### 4. `README.md` — new "Remote access via Cloudflare Tunnel" section

Add after the existing "Background via shell aliases" section:
- API key setup: a symlink-safe same-directory `mktemp` → `chmod 600`
  → atomic `mv -f` that writes the bearer token to
  `~/.config/mlx-stt-server/api-key`, plus `MLX_STT_REQUIRE_KEY=1` in
  `~/.config/mlx-stt-server/env` (chmod 644, no secret in here). The
  exact one-liner lives in the runbook below; the README just
  references the runbook rather than duplicating the command (so a
  copy-paste from the README can never regress the atomic pattern).
- Cloudflared runbook (see "Runbook" below)
- Client config update: whatever OpenAI-compatible client you use,
  set Base URL to `https://<your-hostname>` and paste the bearer token
  as the API key.

### 5. `README.md` — document the auth model (public)

Append a "Remote access" subsection under the existing "Run" section
pointing at this plan doc. Keep the README version short — one
paragraph + a link here. All the security detail lives in this file.

(This repo does not ship a `CLAUDE.md` — it's in `.gitignore` because
per-operator context varies. If you keep a local `CLAUDE.md` for your
own notes, that's fine, it just won't be pushed.)

## Prerequisites

Before running the runbook, confirm on the host machine (the one that
will run the server and the tunnel):

1. **Python + venv**:
   ```bash
   cd /path/to/mlx-stt-server
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Hugging Face token** (only needed if you want the default gated
   Cohere model). Either `huggingface-cli login` or `export HF_TOKEN=...`.
   Without this the first model load will 401.
3. **cloudflared**: `brew install cloudflared`
4. **A domain already on Cloudflare.** The runbook assumes you have a
   zone in your Cloudflare account that you want to use for the tunnel
   hostname. `cloudflared tunnel login` will show you a picker for your
   zones.
5. **Hostname decision**: pick the subdomain you want the tunnel to
   respond at — e.g. `stt.example.com`, `dictation.example.com`,
   `mlx.example.com`. You'll need this at runbook step 5.
6. **Audio sample for the verification step**: the shipped
   `samples/test.m4a` (generated by macOS `say`) is used by the local
   auth matrix. If you remove it, edit the `$AUDIO` variable in the
   verification script to point at your own clip.

## Runbook (run these on the host machine)

1. `brew install cloudflared` (if not done in prerequisites above)
2. Generate the key FILE and the non-secret env file via a
   **symlink-safe atomic write** — never redirect into the final path
   directly. A hostile symlink already at `~/.config/mlx-stt-server/api-key`
   would otherwise receive the token before any `chmod` could take
   effect. The pattern is: create a 0600 tmpfile in the same directory
   with `mktemp`, write into it, then `mv -f` atomically into place
   (`rename(2)` does not follow destination symlinks on macOS when the
   source and destination are regular files in the same filesystem,
   and atomically replaces any existing file at the destination).
   ```bash
   mkdir -p ~/.config/mlx-stt-server

   # --- The bearer token (0600, symlink-safe atomic write) ---
   TMPKEY=$(mktemp "$HOME/.config/mlx-stt-server/.api-key.XXXXXX")
   chmod 600 "$TMPKEY"
   openssl rand -hex 32 > "$TMPKEY"
   mv -f "$TMPKEY" ~/.config/mlx-stt-server/api-key

   # --- Non-secret toggles (no key in here) ---
   # Env file has no secret, so a plain redirect is fine. Still use
   # mktemp+mv for consistency and so that re-running the step
   # doesn't momentarily expose an empty file during write.
   TMPENV=$(mktemp "$HOME/.config/mlx-stt-server/.env.XXXXXX")
   printf 'MLX_STT_REQUIRE_KEY=1\n' > "$TMPENV"
   chmod 644 "$TMPENV"
   mv -f "$TMPENV" ~/.config/mlx-stt-server/env
   ```
   After this, `scripts/stt-server start` sources the env file → sets
   `MLX_STT_REQUIRE_KEY=1` → `server.py`'s lifespan reads the token
   directly from `~/.config/mlx-stt-server/api-key` at startup. The token
   is never present in any shell or process environment variable.
3. `cloudflared tunnel login` → pick the Cloudflare zone
4. `cloudflared tunnel create mlx-stt-server` → writes
   `~/.cloudflared/<uuid>.json`
5. Create `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <uuid>
   credentials-file: /Users/YOUR_USER/.cloudflared/<uuid>.json
   ingress:
     - hostname: <your-hostname>
       service: http://127.0.0.1:8765
     - service: http_status:404
   ```
6. `cloudflared tunnel route dns mlx-stt-server <your-hostname>`
7. `sudo cloudflared service install` → launchd agent starts tunnel at boot
8. `stt-server --start` on the host — picks up the env file, loads the
   key, refuses to start if the key is missing
9. **(From any client machine now, not the tunneled host.)** Fetch the
   token to a local 0600
   file via a symlink-safe atomic write, then hit the tunnel with
   curl -K so the token never lands on any argv:
   ```bash
   mkdir -p ~/.config/mlx-stt-server
   # Create the destination 0600 in the same directory BEFORE writing,
   # using mktemp + atomic mv. This defeats symlink-swap attacks on the
   # final path: a hostile symlink at ~/.config/mlx-stt-server/api-key
   # cannot redirect the write, because we write to a fresh tmpfile and
   # the rename operation does not follow symlinks on the destination.
   TMPKEY=$(mktemp "$HOME/.config/mlx-stt-server/.api-key.XXXXXX")
   chmod 600 "$TMPKEY"
   ssh <tunneled-host> 'cat ~/.config/mlx-stt-server/api-key' > "$TMPKEY"
   mv -f "$TMPKEY" "$HOME/.config/mlx-stt-server/api-key"

   # Use curl -K with a temp 0600 config file. The $(cat ...) expansion
   # happens in the shell process; it never becomes an argv to any
   # exec'd binary. The config file is scrubbed and removed immediately
   # after the request.
   CURL_CONF=$(mktemp); chmod 600 "$CURL_CONF"
   printf 'header = "Authorization: Bearer %s"\n' \
       "$(cat ~/.config/mlx-stt-server/api-key)" > "$CURL_CONF"
   curl -fsS -K "$CURL_CONF" https://<your-hostname>/v1/models
   : > "$CURL_CONF"   # scrub
   rm -f "$CURL_CONF"
   ```
   The key file on the client (`~/.config/mlx-stt-server/api-key`)
   exists so that your dictation client can be configured manually
   (step 10) and so subsequent test curls can reuse the same pattern
   without re-fetching.
10. **(Still on the client machine.)** Configure your OpenAI-compatible
    client: Base URL `https://<your-hostname>` (or `.../v1` for SDK
    clients that don't append it), and paste the API Key field by
    `cat`-ing the local key file in a terminal and copy-pasting the
    contents. Most dictation clients store the key in their own
    Keychain entry, not in shell history, so paste-and-forget is safe.

## Critical files

- `server.py` — all code changes are in this one file
  - Module-level: define `DEFAULT_KEY_FILE`, `_load_api_key()` (opens
    file with `O_RDONLY | O_NOFOLLOW`, `fstat()`'s the fd, verifies
    `st_uid == os.getuid()` AND `(st_mode & 0o077) == 0`, returns
    `Optional[str]`). Only `FileNotFoundError` maps to `None`; every
    other `OSError` / `RuntimeError` propagates. Set
    `_API_KEY = _load_api_key()`. NO `MLX_STT_API_KEY` env var reading
    anywhere.
  - Module-level: define `UNAUTH_PATHS = frozenset({"/healthz"})`,
    `AuthMiddleware` (ASGI3, rejects non-Bearer before routing),
    `MaxBodySizeMiddleware` (ASGI3, Content-Length gate),
    `MAX_UPLOAD_BYTES`. **No `Depends(verify_api_key)` dependency —
    auth is middleware-only.**
  - `lifespan` (line 116) → read `MLX_STT_REQUIRE_KEY` env var, raise
    `RuntimeError` if set and `_API_KEY is None`, or if `_API_KEY` is
    not None and length < 32; print `auth: enabled/disabled` (never
    the key or its length prefix).
  - Constructor `app = FastAPI(...)` at line 130 → add
    `docs_url=None`, `redoc_url=None`, `openapi_url=None`; remove
    `CORSMiddleware`.
  - After construction:
    `app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_UPLOAD_BYTES)`,
    then `if _API_KEY is not None: app.add_middleware(AuthMiddleware,
    api_key=_API_KEY)`. Order matters — auth is outer (runs first).
  - Delete `@app.get("/")` at line 141 + its handler.
  - New `@app.get("/healthz")` returning `PlainTextResponse("ok")`.
    In `UNAUTH_PATHS` so the middleware lets it through.
  - `/v1/models` at 151, `/v1/audio/transcriptions` at 168,
    `/v1/audio/translations` below 209 → **NO `dependencies=[...]`**.
    The middleware owns auth; the route decorators stay plain.
  - `create_transcription` body (currently line 168–208) → generic
    client-facing error strings; NO in-handler size counting
    (middleware owns that); detailed exception text to stderr only.
  - `main()` at bottom → `--require-key` flag that sets
    `os.environ["MLX_STT_REQUIRE_KEY"] = "1"` *before* `uvicorn.run()`.
    No `--api-key` CLI flag exists — the key is always file-based.
- `scripts/stt-server` — env sourcing at the very top (before `PORT=`, line ~22),
  `--require-key` appending in EXTRA_ARGS (line ~36), health probe change in
  `cmd_status` (line ~94)
- New: `.env.example` at repo root
- New: sections in `README.md` and `CLAUDE.md`

## Verification

### Local auth matrix (run from the repo root before deploying)

Save as `scripts/verify-auth.sh` (or any path inside the repo) and run.
Every `assert_code` / `assert_exit` must pass or the script aborts.
Aliases are not available in a non-interactive shell, so we call
`./scripts/stt-server` directly. The script is self-locating — it
resolves the repo root from its own location, so `REPO` is correct no
matter where you clone the repo or what CWD you run it from.

```bash
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
    conf=$(mktemp -t cohere-curl.XXXXXX)
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
SHORT_KEY_FILE=$(mktemp -t cohere-test-key.XXXXXX)
chmod 600 "$SHORT_KEY_FILE"
printf 'tooshort' > "$SHORT_KEY_FILE"
MLX_STT_API_KEY_FILE="$SHORT_KEY_FILE" \
    assert_exit_nonzero python server.py --require-key --no-preload
rm -f "$SHORT_KEY_FILE"

# ---------------------------------------------------------------------------
# 3b. Key file with wrong mode (0644) → non-zero exit
#     (Hardening: the server refuses to load a world/group-readable key file.)
# ---------------------------------------------------------------------------
BAD_MODE_FILE=$(mktemp -t cohere-test-key.XXXXXX)
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
TEST_KEY_FILE=$(mktemp -t cohere-test-key.XXXXXX)
chmod 600 "$TEST_KEY_FILE"
openssl rand -hex 32 > "$TEST_KEY_FILE"
MLX_STT_API_KEY_FILE="$TEST_KEY_FILE" MLX_STT_REQUIRE_KEY=1 \
    python server.py --no-preload &
SRV=$!
sleep 1

AUTH_CONF=$(mk_auth_conf "$TEST_KEY_FILE")
BAD_CONF=$(mktemp -t cohere-curl.XXXXXX); chmod 600 "$BAD_CONF"
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

TEST_ENV_FILE=$(mktemp -t cohere-test-env.XXXXXX)
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
```

### End-to-end via the tunnel (run on the tunneled host + a client)

Same token hygiene as the local verification — no shell variables, no
curl argv containing the bearer.

```bash
# --- On the tunneled host ------------------------------------------------
stt-server --start
stt-server --status   # health OK via /healthz
# launchd-managed cloudflared should already be up; confirm:
cloudflared tunnel info mlx-stt-server

# --- On a client machine -------------------------------------------------
# Assumes ~/.config/mlx-stt-server/api-key was already populated via the
# atomic mktemp+mv pattern in runbook step 9. If not, run that first.

AUTH_CONF=$(mktemp); chmod 600 "$AUTH_CONF"
printf 'header = "Authorization: Bearer %s"\n' \
    "$(cat ~/.config/mlx-stt-server/api-key)" > "$AUTH_CONF"
trap '{ : > "$AUTH_CONF"; rm -f "$AUTH_CONF"; }' EXIT

curl -fsS -K "$AUTH_CONF" https://<your-hostname>/v1/models

# Use the shipped sample clip (or your own) — it's portable across
# machines because it lives in the repo.
curl -fsS -K "$AUTH_CONF" -X POST \
    https://<your-hostname>/v1/audio/transcriptions \
    -F "file=@samples/test.m4a"

# Client setup: open a terminal, `cat ~/.config/mlx-stt-server/api-key`,
# copy-paste the output into the API Key field of your dictation client,
# set Base URL = https://<your-hostname>. Most clients store the key in
# their own Keychain entry — not in any shell history.
```

### Codex review of the implementation (final gate before committing)

After all local tests pass, run Codex read-only over the working tree to
audit the actual code — not the plan this time — for the same class of
issues Codex flagged in this plan.

```bash
cat /tmp/codex-mlx-stt-impl-prompt.txt | codex -a never exec -s read-only \
  -C "$(git rev-parse --show-toplevel)" \
  -o /tmp/codex-mlx-stt-impl-out.md
```

Prompt asks Codex to do an **open-ended, security-focused** review of
the implementation and report findings tagged **CRITICAL / HIGH /
MEDIUM / LOW**, mirroring the framework used on the plan itself. Only
block on CRITICAL, HIGH, and security-relevant MEDIUM findings. The
prompt explicitly flags these areas of concern (but does not constrain
Codex to only these):

- **Token never lives in a process environment.** Grep the diff + the
  final `server.py` for `os.environ.get("MLX_STT_API_KEY"` or any other
  env-var read of the key. The only legal path is the file reader; no
  code path should accept the key as an env var or CLI arg.
- **Token never lands on an argv.** Grep for any subprocess call or
  logged command line that includes the key. Spot-check the scripts
  and any helper shells for the same.
- **Key file loading is TOCTOU-safe.** Verify `_load_api_key` uses
  `os.open(..., O_RDONLY | O_NOFOLLOW)` + `os.fstat()` on the fd (not
  `os.stat(path)` followed by `open(path)`), checks `st_uid ==
  os.getuid()` AND `(st_mode & 0o077) == 0`, and that only
  `FileNotFoundError` maps to "no auth." Other OSErrors
  (PermissionError, ELOOP, ENOTDIR) must be fatal.
- **`secrets.compare_digest`** is used correctly on the submitted
  token bytes with no short-circuit (length check, early return) that
  leaks data via timing.
- **Log lines, print statements, and error responses do not echo the
  key or any prefix of it.** Include traceback formatting —
  `RuntimeError("...")` raised in lifespan is printed by uvicorn;
  make sure the message itself does not include the loaded key value.
  Check that the middleware's 401 response body is a constant
  (`"Unauthorized\n"`), not a formatted message that could include
  the submitted token.
- **Auth is middleware-based, not dependency-based.** Grep the diff
  for any lingering `Depends(verify_api_key)` or
  `dependencies=[Depends(...)]` and fail the review if any remain.
  Confirm `AuthMiddleware` is registered LAST (outer) so it runs
  BEFORE Starlette's form parser, not after.
- **Route coverage is complete.** Iterate `app.routes` programmatically
  with a one-liner, list all paths, and confirm only `/healthz` is in
  `UNAUTH_PATHS`. Confirm `/docs`, `/redoc`, `/openapi.json`, and
  `/docs/oauth2-redirect` all return 404.
- **`MaxBodySizeMiddleware` runs early enough** to bound ingestion.
  Run a concrete test: start the server, send a large-Content-Length
  POST without auth, confirm it gets 401 BEFORE any multipart parsing
  happens (the form parser should never be entered). Then send a
  large-Content-Length POST WITH auth, confirm it gets 413 from the
  middleware before the route handler runs.
- **Lifespan guards hold across all entrypoints**: `python server.py
  --require-key`, direct `uvicorn server:app` with
  `MLX_STT_REQUIRE_KEY=1`, and `./scripts/stt-server start` with an env
  file that sets `MLX_STT_REQUIRE_KEY=1`. Test each.
- **Env-file sourcing in `scripts/stt-server`** happens BEFORE `PORT=`,
  `PYTHON_BIN=`, `EXTRA_ARGS` derivation. No key ends up in
  `server.log`, `server.pid`, or the shell history. The env file
  itself contains NO secrets (only the path to the key file, and
  toggles).
- **`stt-server --status`** probes `/healthz` (unauth) and returns correct
  OK/FAIL.

Codex has full filesystem read in `-s read-only` (we verified this
empirically), so it can read installed Starlette/uvicorn/FastAPI
sources to validate middleware ordering and dependency resolution
claims. It cannot write files, so it can start short-lived local
servers for dynamic testing using `python server.py --no-preload` but
cannot modify the source tree.

**Iterate until Codex returns no CRITICAL, HIGH, or security-relevant
MEDIUM findings.** LOW findings and non-security MEDIUM findings can
be left open. Then commit.

Iterate with Codex until there are no high/medium findings. Then commit.
