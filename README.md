# mlx-stt-server

Local OpenAI-compatible speech-to-text server that wraps
[mlx-audio](https://github.com/Blaizzy/mlx-audio) on Apple Silicon.
Exposes the subset of OpenAI's `/v1/audio/transcriptions` API that most
dictation clients, transcription tools, and `openai` SDK wrappers call,
so you can point any OpenAI-compatible client at `http://127.0.0.1:8765`
and run inference locally on an M-series Mac with no cloud round-trip.

Default model is [Cohere Transcribe
(`CohereLabs/cohere-transcribe-03-2026`)](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026),
a 2B-parameter multilingual ASR model, but **any mlx-audio STT model**
works via `--model` — Whisper, Parakeet, Qwen3-ASR, Voxtral, Canary,
Moonshine, etc.

## Requirements

- macOS on Apple Silicon (M1 or newer)
- Python 3.10+
- ~5 GB free disk for the default Cohere model (downloaded on first run)
- A Hugging Face account with access to the model you want to use

## Install

```bash
# 1. Clone
git clone https://github.com/piercecohen1/mlx-stt-server.git
cd mlx-stt-server

# 2. Create a virtualenv (recommended — the start script auto-detects
#    .venv/bin/python and venv/bin/python)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

### Hugging Face authentication

The default model (`CohereLabs/cohere-transcribe-03-2026`) is a **gated**
model on Hugging Face — you need an HF account, you need to accept the
model's terms on its page, and you need a token available to
`huggingface_hub`. Whisper and some other mlx-audio models are public and
don't require this step.

```bash
# Option A: interactive login (browser + paste token)
huggingface-cli login

# Option B: set a token env var (useful for scripts and the Mac Mini)
export HF_TOKEN="hf_xxx..."

# Option C: long-term, add to your shell rc file
echo 'export HF_TOKEN="hf_xxx..."' >> ~/.zshrc
```

If you skip this, the first request to load Cohere Transcribe will fail
with a 401 from Hugging Face. You can swap to a non-gated model with
`--model`, e.g. `--model mlx-community/whisper-large-v3-turbo`.

## Run

### Foreground (dev)

```bash
python server.py                       # preloads default model on :8765
python server.py --port 8123
python server.py --no-preload          # load on first request instead
python server.py --model mlx-community/whisper-large-v3-turbo
```

The first run downloads the model from Hugging Face (~4 GB for Cohere
Transcribe). Subsequent runs load from the local HF cache in ~1 second.

### Background with shell alias (recommended for dictation)

A wrapper script at `scripts/stt-server` backgrounds the server via
`nohup`, tracks the PID in `~/.cache/mlx-stt-server/server.pid`, and
appends logs to `~/.cache/mlx-stt-server/server.log`. One-time setup:

```bash
./scripts/install-aliases.sh
source ~/.zshrc          # or ~/.bashrc; or just open a new terminal tab
```

That adds a single `stt-server` alias pointing at the wrapper script.
Then from any directory:

```bash
stt-server --start       # nohup the server, return immediately
stt-server --status      # shows PID + health check against /healthz
stt-server --stop        # SIGTERM the recorded PID (SIGKILL after 10s)
stt-server --restart     # stop + start
stt-server --logs        # tail -f the log file
```

The wrapper sources `~/.config/mlx-stt-server/env` (if present) at the
top of the script, so you can set `STT_SERVER_PORT`, `STT_SERVER_MODEL`,
`STT_SERVER_PYTHON`, or any `MLX_STT_*` server-side variable in one
place.

## Point an OpenAI-compatible client at it

Any tool that speaks the OpenAI audio API works. Base URL is
`http://127.0.0.1:8765` — most clients append `/v1/audio/transcriptions`
themselves, so paste just the host. A few examples:

### `openai` Python SDK

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="local",   # anything works — not validated in local mode
)
with open("audio.wav", "rb") as f:
    result = client.audio.transcriptions.create(
        model="CohereLabs/cohere-transcribe-03-2026",
        file=f,
    )
print(result.text)
```

### curl

```bash
curl -X POST http://127.0.0.1:8765/v1/audio/transcriptions \
  -F "file=@samples/test.m4a" \
  -F "model=CohereLabs/cohere-transcribe-03-2026" \
  -F "language=en" \
  -F "response_format=json"
```

### Dictation apps with "Custom / OpenAI-Compatible API" settings

| Field                | Value                                       |
| ---                  | ---                                         |
| Base URL / Endpoint  | `http://127.0.0.1:8765`                     |
| API Key              | any non-empty string (e.g. `local`)         |
| Model                | `CohereLabs/cohere-transcribe-03-2026`      |

Some clients require the full `/v1` suffix (e.g. `http://127.0.0.1:8765/v1`),
some append it automatically — if one form 404s, try the other.

## Supported API subset

- `POST /v1/audio/transcriptions` — multipart form-data
  - `file` — audio file; any format mlx-audio / librosa can read
    (wav, mp3, m4a, flac, ogg, ...)
  - `model` — any mlx-audio STT model ID
  - `language` — ISO-639-1 code (default `en`)
  - `response_format` — `json` (default), `text`, `verbose_json`, `srt`, `vtt`
  - `prompt`, `temperature` — accepted for client compatibility but
    ignored by the default Cohere model (it has no prompt slot). Whisper
    *does* consume `prompt` if you use `--model ...whisper...`; wiring
    that through is a ~10-line change to `server.py`.
- `POST /v1/audio/translations` — forwards to transcription with
  `language=en`. Cohere Transcribe is ASR-only, not a translator; this
  endpoint exists so clients that default to `/translations` still get
  something usable.
- `GET /v1/models` — lists loaded + default models in OpenAI format.
- `GET /healthz` — plaintext `ok` (used by the wrapper's health probe).

## Smoke test

The repo ships with a tiny synthesized test clip at `samples/test.m4a`
(generated via macOS `say`, ~28 KB) so the curl example above works
out of the box without you supplying your own audio.

```bash
curl http://127.0.0.1:8765/v1/models

curl -X POST http://127.0.0.1:8765/v1/audio/transcriptions \
  -F "file=@samples/test.m4a" \
  -F "model=CohereLabs/cohere-transcribe-03-2026" \
  -F "language=en"
```

## Remote access (optional auth)

For local-only use, skip this section — the server listens on
`127.0.0.1` with no auth by default, which is the right setup for
dictation on your own machine.

To expose the server beyond loopback (LAN, Tailscale, Cloudflare
Tunnel, etc.), enable bearer-token auth by creating a key file:

```bash
umask 077
mkdir -p ~/.config/mlx-stt-server
openssl rand -hex 32 > ~/.config/mlx-stt-server/api-key
chmod 600 ~/.config/mlx-stt-server/api-key
```

The server auto-detects the key file at startup. To refuse startup
when the file is missing or empty (recommended on any tunneled host):

```bash
python server.py --require-key
# or, equivalently
MLX_STT_REQUIRE_KEY=1 python server.py
```

Clients send the token in the `Authorization` header:

```bash
curl -X POST https://your-host/v1/audio/transcriptions \
  -H "Authorization: Bearer $(cat ~/.config/mlx-stt-server/api-key)" \
  -F "file=@samples/test.m4a" \
  -F "model=CohereLabs/cohere-transcribe-03-2026"
```

`/healthz` is always open (so tunnel health probes don't need
credentials). Every other endpoint returns 401 without a valid
bearer token when a key is loaded.

Environment knobs:

| Variable                 | Default                             | Purpose                                                             |
| ---                      | ---                                 | ---                                                                 |
| `MLX_STT_API_KEY_FILE`   | `~/.config/mlx-stt-server/api-key`  | Override the key-file path.                                         |
| `MLX_STT_REQUIRE_KEY`    | `0`                                 | Set to `1` to refuse startup without a valid key file.              |
| `MLX_STT_MAX_UPLOAD_MB`  | `500`                               | Body-size cap enforced before form parsing (returns 413 over cap).  |
| `MLX_STT_LOG_TRANSCRIPTS`| `0`                                 | Set to `1` to log each request's transcribed text (also `--log-transcripts`). Off by default so routine dictation isn't written to the log. |

Rotate the token by atomically overwriting the key file (`mktemp` in
the same dir → `chmod 600` → `mv -f`); the server re-reads it at
startup only, so you need to restart after rotation.

## Architecture

Thin FastAPI wrapper around `mlx_audio.stt.load(...).generate(path, language=...)`:

1. On startup the model is loaded eagerly via FastAPI `lifespan` and
   cached in a module-level dict keyed by model ID.
2. On each request the uploaded audio is written to a `tempfile` in
   `$TMPDIR`, passed as a path to `stt_model.generate(path, language=lang)`,
   and deleted in a `finally` block.
3. The returned `STTOutput` is shaped into whichever `response_format`
   the client asked for. `json` → `{"text": ...}`, `verbose_json` mirrors
   OpenAI's segment shape, `srt`/`vtt` are generated from
   `result.segments`.

The model only loads once per process, so first-request latency is
just inference (~0.7 s for a 35 s clip on an M-series Mac with the
default Cohere model).

## Known limitations

- Cohere Transcribe is ASR only — `/v1/audio/translations` is a
  pass-through that forces `language=en`.
- `prompt` and `temperature` are accepted but ignored for the default
  Cohere model. Other mlx-audio models may consume them (Whisper's
  `initial_prompt`, Qwen3-ASR's context field); wire-through requires
  a small patch to `server.py`.
- No streaming; Cohere's `generate()` explicitly raises on `stream=True`.
- 14 languages for the default Cohere model: en, fr, de, it, es, pt,
  el, nl, pl, zh, ja, ko, vi, ar. Switch models for other languages.
- `scripts/install-aliases.sh` writes to `~/.zshrc` or `~/.bashrc`.
  If you use fish, nushell, or any other shell, you'll need to install
  the alias by hand — the script handles bash and zsh only.

## License

MIT — see [LICENSE](LICENSE). mlx-audio, which this server wraps, is
also MIT-licensed.
