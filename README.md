# mlx-cohere-openai-compatible-api

Local OpenAI-compatible transcription server backed by [mlx-audio] and
[Cohere Transcribe] (2B ASR model). Exposes the exact subset of OpenAI's
`/v1/audio/transcriptions` API that dictation clients like [Spokenly] call, so
your dictation runs locally on Apple Silicon with no cloud round-trip.

[mlx-audio]: https://github.com/Blaizzy/mlx-audio
[Cohere Transcribe]: https://huggingface.co/CohereLabs/cohere-transcribe-03-2026
[Spokenly]: https://spokenly.app

## Setup

```bash
# 1. Make sure you're logged into Hugging Face (the Cohere model is gated)
huggingface-cli login

# 2. Install deps (a venv is recommended)
pip install -r requirements.txt
```

## Run

```bash
# Preloads Cohere Transcribe at startup on http://127.0.0.1:8765
python server.py

# Different port
python server.py --port 8123

# Skip preload (model loads on first request)
python server.py --no-preload

# Use a different STT model (any mlx-audio STT model ID)
python server.py --model mlx-community/whisper-large-v3-turbo
```

The first run downloads the model from Hugging Face (~4 GB for Cohere
Transcribe). Subsequent runs load from the local HF cache.

### Background via shell aliases

`scripts/cohere` wraps the server in a `nohup` + PID-file harness so it
survives terminal close. The repo installs these aliases in `~/.zshrc`:

```bash
cohere-start     # nohup the server, record PID, return immediately
cohere-stop      # SIGTERM the recorded PID (SIGKILL after 10s)
cohere-restart   # stop + start
cohere-status    # shows PID + health check against /v1/models
cohere-logs      # tail -f the server log
```

State lives in `~/.cache/cohere-stt/`:
- `server.pid` — current PID (removed on stop)
- `server.log` — appended stdout/stderr

Overrides via env vars: `COHERE_STT_PORT`, `COHERE_STT_PYTHON`,
`COHERE_STT_MODEL`, `COHERE_STT_PID_FILE`, `COHERE_STT_LOG_FILE`.

## Point Spokenly at it

In Spokenly's Custom / OpenAI-Compatible API settings:

| Field | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:8765` |
| API Key | anything (e.g. `local`) — not validated |
| Model | `CohereLabs/cohere-transcribe-03-2026` |

> **Note:** Spokenly appends `/v1/audio/transcriptions` to the base URL itself,
> so paste **just the host**, not `http://127.0.0.1:8765/v1`. For plain OpenAI
> SDK clients you *do* want the `/v1` suffix.

## Smoke test

```bash
curl http://127.0.0.1:8765/v1/models

curl -X POST http://127.0.0.1:8765/v1/audio/transcriptions \
  -F "file=@pierce-voice-note.m4a" \
  -F "model=CohereLabs/cohere-transcribe-03-2026" \
  -F "language=en" \
  -F "response_format=json"
```

## Supported OpenAI features

- `POST /v1/audio/transcriptions` (multipart/form-data)
  - `file` — audio file; any format `mlx-audio` / `librosa` can read
    (wav, mp3, m4a, flac, ogg, …)
  - `model` — any mlx-audio STT model ID; defaults to Cohere Transcribe
  - `language` — ISO-639-1 code; defaults to `en`
  - `response_format` — `json` (default), `text`, `verbose_json`, `srt`, `vtt`
  - `prompt`, `temperature` — accepted for client compat, ignored by Cohere
- `POST /v1/audio/translations` — forwards to transcription with `language=en`
- `GET /v1/models` — lists loaded + default model in OpenAI format

## How it works

Thin FastAPI wrapper around `mlx_audio.stt.load(...).generate(path, language=...)`:

1. On startup the model is loaded eagerly via `lifespan` and cached in a
   module-level dict keyed by model ID.
2. On each request the uploaded audio is written to a temp file (so the
   Cohere model's own `_to_mono` → `load_audio` path handles decoding) and
   passed to `model.generate(path, language=lang)`.
3. The resulting `STTOutput` is shaped into whichever `response_format` the
   client asked for — `json` maps to `{"text": ...}`, `verbose_json` mirrors
   OpenAI's segment shape, `srt`/`vtt` are generated from `result.segments`.

The model only loads once per process, so first-request latency is just
inference (~0.7 s for a 35 s clip on an M5 Max per the reference session).

## Known limitations

- Cohere Transcribe is ASR only — no true translation, so
  `/v1/audio/translations` is a pass-through that forces `language=en`.
- `prompt` and `temperature` are accepted but ignored.
- No streaming; Cohere's `generate()` explicitly raises on `stream=True`.
- 14 supported languages (see Cohere model card).
