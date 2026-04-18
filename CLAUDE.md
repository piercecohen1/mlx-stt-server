# CLAUDE.md — notes for future Claude sessions

Context that isn't in the public README: machine layout, dictation
client config, upstream model gotchas, design review history, and
other lived-in notes useful for future agents working on this repo.

## Machine layout

- **MacBook Pro (this machine)** — dev machine, runs the server locally
  for dictation. Repo at `/Users/piercecohen/repos/mlx-stt-server/`.
  A sibling `mlx-audio` checkout lives at
  `/Users/piercecohen/mlx/mlx-audio/` (different parent dir — the repo
  was moved out of `~/mlx/` to align with the rest of `~/repos/`).
- **Mac Mini** — runs the deployed server behind a Cloudflare tunnel so
  MBP + phone hit one always-on endpoint instead of starting the server
  locally per machine.

## The rebranding decision

Repo was originally committed as `mlx-cohere-openai-compatible-api` and
renamed to `mlx-stt-server` on GitHub after Pierce pushed back on the
Cohere-specific name. The local directory was subsequently moved to
`~/repos/mlx-stt-server/` on 2026-04-16 to match. If you ever `mv` the
local dir again, re-run `./scripts/install-aliases.sh` afterward so the
`stt-server` alias picks up the new absolute path.

## Auth layer (implemented 2026-04-12)

`server.py` now supports optional bearer-token auth via ASGI middleware.
Key files changed:

- **`server.py`**: `_load_api_key()` (TOCTOU-safe file reader),
  `AuthMiddleware` + `MaxBodySizeMiddleware` (ASGI), `/healthz` endpoint,
  `--require-key` CLI flag, disabled `/docs`/`/redoc`/`/openapi.json`,
  removed CORS and disclosive root route, sanitized error messages.
- **`scripts/stt-server`**: removed legacy `/v1/models` fallback in
  `cmd_status`, now probes `/healthz` only.
- **`.env.example`**: documents non-secret env vars for tunneled hosts.
- **`scripts/verify-auth.sh`**: 24-assertion test suite covering backward
  compat, startup guards, auth matrix, 413 body cap, and wrapper integration.

**Local mode is backward compatible** — without a key file at
`~/.config/mlx-stt-server/api-key` (or `$MLX_STT_API_KEY_FILE`), all
endpoints remain open with no auth. Auth activates automatically when
a valid key file exists.

**Deployed** on the Mac Mini behind a Cloudflare tunnel; the original
runbook at `.claude/plans/remote-access.md` was removed once the rollout
was verified.

## Dictation client: Spokenly

Pierce uses [Spokenly](https://spokenly.app) as the front-end dictation
client. Config that works end-to-end:

| Field                | Value                                   |
| ---                  | ---                                     |
| Base URL             | `http://127.0.0.1:8765`                 |
| API Key              | bearer token from `~/.config/mlx-stt-server/api-key` (or any string in local mode) |
| Model                | `CohereLabs/cohere-transcribe-03-2026`  |

**Spokenly gotcha — do not forget:** Spokenly's "Base URL" field wants
**just the host** (`http://127.0.0.1:8765`), not the `/v1` suffix.
Spokenly appends `/v1/audio/transcriptions` itself. Plain OpenAI SDK
clients *do* want the `/v1` suffix — this quirk is Spokenly-specific.
Pierce hit this during initial setup; came up again when writing the
public README.

Spokenly only exposes ONE credential field (mapped to the
`Authorization: Bearer <key>` header). This is why the server uses a
plain bearer token instead of Cloudflare Access service tokens (which
need two custom headers). Revisit if Spokenly ever grows multi-header
support.

## Cohere Transcribe — what the model does NOT support

Researched against the HF model card and Cohere's blog/launch coverage
(2026-04-10):

- **No keyterms / hotwords / context biasing / custom vocabulary.** The
  upstream processor signature is
  `processor(audio, sampling_rate, language, punctuation)`. That's it.
  Don't waste time trying to wire a `prompt` or `context` field into the
  Cohere path — it has nowhere to go. Cohere's *paid cloud* Transcribe is
  a separate product; don't confuse the two.
- **No streaming.** `cohere_asr.py` raises `NotImplementedError` on
  `stream=True`.
- **14 languages only**: en, fr, de, it, es, pt, el, nl, pl, zh, ja, ko,
  vi, ar.

If Pierce ever wants hotword biasing, the drop-in alternatives are
Whisper (`mlx-community/whisper-large-v3-turbo`) which has
`initial_prompt`, or Qwen3-ASR which has a context field — see
`/Users/piercecohen/mlx/mlx-audio/examples/qwen3_asr_transcription.py`.
Either would require ~10 lines in `create_transcription()` to wire the
`prompt` form field through to the model's prompt/context kwarg.

## Ground truth from initial testing (2026-04-10)

- End-to-end tested against a personal voice note at
  `/Users/piercecohen/mlx/mlx-audio-tests/pierce-voice-note.m4a` on an
  M5 Max (128 GB):
  - Cold load: ~1.1 s from HF cache
  - Per-request: ~0.7 s for a 35 s clip
  - Peak memory: ~4.74 GB (stays resident for process lifetime)
- All four `response_format` values verified returning expected shape.
- Output byte-matches the reference `python -m mlx_audio.stt.generate` CLI.

## Auth layer — review history

The design went through **seven rounds** of Codex review (`-s read-only`,
severity-tagged CRITICAL/HIGH/MEDIUM/LOW) before implementation. Key
decisions locked in during review:

1. **File-based key loading, not env var.** `_load_api_key` uses
   `os.open(path, O_RDONLY | O_NOFOLLOW)` → `os.fstat()` on the fd →
   checks `st_uid == os.getuid()` AND `(st_mode & 0o077) == 0`. TOCTOU-safe.
   Env-var transport was rejected because it lands in `ps -ewww`,
   sysdiagnose, and crash reports.
2. **Auth is ASGI middleware, NOT `Depends(verify_api_key)`.** FastAPI
   0.129.0 parses `await request.form()` BEFORE running route dependencies,
   so a `Depends` approach still lets unauth clients spool the full
   multipart body before 401. Middleware runs before the router.
3. **Pre-parse body size cap via ASGI middleware.** Handler-level byte
   counting is a lie — Starlette has already spooled the body by the
   time the handler runs. Middleware inspects `Content-Length` and
   short-circuits with 413.
4. **Symlink-safe atomic writes** for the key file in ALL the places
   the plan teaches creating/rotating it. Raw
   `> ~/.config/.../api-key` redirects were flagged as CRITICAL because
   a hostile symlink could redirect the write before `chmod 600` took
   effect. The canonical pattern is `mktemp` in the same dir → `chmod
   600` → `mv -f` atomic rename.
5. **`--require-key` guard lives in `lifespan`, not `main()`.** Gated
   by `MLX_STT_REQUIRE_KEY=1` env var, which is read at lifespan startup.
   This is so `uvicorn server:app` direct boot honors the guard — a
   `main()`-only check would be bypassed.
6. **CORSMiddleware removed.** The old server had `allow_origins=["*"]`.
   For an internet-facing bearer-token API that doesn't serve a browser
   UI, CORS is unnecessary attack surface.
7. **FastAPI `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`
   all disabled** via `docs_url=None, redoc_url=None, openapi_url=None`
   in the constructor. They leaked the route schema otherwise.

## Empirical sandbox finding

Ran a controlled test during the review: Codex's `-s read-only` mode
does NOT scope reads to the `-C` working directory — it has fullAccess
reads across the whole filesystem (subject to Unix perms). Confirmed by
placing a sentinel file outside the workspace containing a fresh
`openssl rand -hex 4` token and having Codex return it verbatim. This
matches the docs at
`~/repos/openai-docs/codex/llms-full.txt:2192` which say the
`readOnly` sandbox defaults to `{"type": "fullAccess"}` — restricted
reads are only available via the app-server JSON-RPC API, not via CLI
flags.

For plan reviews this is fine (Codex only reads the plan + repo). Worth
remembering if a future prompt touches sensitive data that we don't
want Codex to see — there's no simple CLI flag to prevent it.

## `scripts/stt-server` gotchas

- **Empty-array expansion under `set -u`**: the script uses
  `${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}` to pass optional extra args.
  Do NOT simplify to `"${EXTRA_ARGS[@]}"` — under `set -euo pipefail`,
  an empty `"${ARR[@]}"` triggers an unbound-variable error that kills
  the backgrounded child before it exec's python. The safe-expansion
  pattern is load-bearing.
- **Orphan PID caveat**: if a previous `python server.py` session is
  still holding port 8765 (e.g. left over from a foreground run), a
  new `stt-server --start` will happily record a PID for the
  backgrounded python, but that child will die seconds later on
  `EADDRINUSE`. Symptom: `stt-server --status` says "not running" but
  `curl http://127.0.0.1:8765/healthz` still works. Fix:
  `lsof -iTCP:8765 -sTCP:LISTEN`, kill the real pid,
  `rm ~/.cache/mlx-stt-server/server.pid`, retry `stt-server --start`.
- **zsh-syntax-highlighting + alias color**: aliases in `~/.zshrc`
  must use **double quotes** so `$HOME` expands at definition time
  (absolute path in the alias body). Single quotes leave `$HOME`
  literal, which paints the alias red because the plugin can't `stat`
  `$HOME/...`. `scripts/install-aliases.sh` already does the right
  thing; just don't hand-edit it back to single quotes.

## Running persistently

Pierce went with the ad-hoc `scripts/stt-server` + shell alias model
rather than launchd. The model stays resident only while actively
dictating — spin up via `stt-server --start`, tear down via
`stt-server --stop`. If he later wants always-on-at-login on the Mini,
a launchd plist is the right upgrade path — but don't proactively add
it without being asked.

## Dependencies on sibling repos

`mlx-audio` is assumed to live at
`/Users/piercecohen/mlx/mlx-audio/` (i.e. `../mlx-audio` relative to
this repo). The server imports it via normal
`pip install mlx-audio[stt,server]`, so a local editable install is
optional — but if Pierce is hacking on mlx-audio itself, point pip at
the local path:

```bash
pip install -e ../mlx-audio[stt,server]
```

When reading mlx-audio internals for this project, the relevant files are:
- `mlx_audio/stt/__init__.py` → `load`, `load_model`
- `mlx_audio/stt/models/cohere_asr/cohere_asr.py` → `generate()` at line 972
- `mlx_audio/stt/models/base.py` → `STTOutput` dataclass
- `mlx_audio/server.py` → upstream's own server (different shape — returns
  ndjson, not OpenAI JSON). We intentionally do NOT use it.

## Sample audio

Repo ships with `samples/test.m4a` (~28 KB, macOS `say -v Samantha`
synthesized "quick brown fox" + "test of the MLX speech to text server").
Generated via:
```bash
say -v Samantha -o /tmp/sample.aiff "<text>"
afconvert -f 'm4af' -d 'aac' -b 32000 /tmp/sample.aiff samples/test.m4a
```
Used in the README smoke test and in `scripts/verify-auth.sh` (via `$AUDIO`).
If you ever regenerate it, keep the file small (<50 KB) so the git
history stays trim, and prefer synthesized over real voice recordings so
the repo has no personal data.
