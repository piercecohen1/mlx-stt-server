# CLAUDE.md — mlx-cohere-openai-compatible-api

Context for future Claude sessions working on this repo.

## What this is

A single-file FastAPI server (`server.py`) that wraps `mlx-audio`'s STT models
behind the OpenAI `/v1/audio/transcriptions` API shape, so local dictation
clients (primary target: **Spokenly**) can use Apple Silicon MLX inference
instead of the OpenAI cloud. Default model is **Cohere Transcribe
(`CohereLabs/cohere-transcribe-03-2026`)**, a 2B-param Conformer ASR — but any
mlx-audio STT model works via `--model`.

## Files

- `server.py` — the entire server. ~240 lines. No package structure; run directly.
- `scripts/cohere` — bash wrapper with `start|stop|restart|status|logs`
  subcommands. Uses `nohup` + PID file so the server persists across terminal
  close. Invoked via the `cohere-start` / `cohere-stop` / etc. aliases in
  `~/.zshrc`. PID + log live under `~/.cache/cohere-stt/`.
- `requirements.txt` — `mlx-audio[stt,server]` + FastAPI stack.
- `README.md` — user-facing docs (setup, Spokenly config, curl smoke test).
- `CLAUDE.md` — this file.

## Key implementation facts

- **Model cache**: module-level `_model_cache` dict in `server.py`. Populated
  by `get_model(model_id)` on first use, reused forever. No eviction. Preload
  happens in the FastAPI `lifespan` handler at startup.
- **Default port**: `8765` (chose this over 8000 to avoid conflicts with
  typical dev servers).
- **Audio handling**: the upload is written to `tempfile.NamedTemporaryFile`
  in `$TMPDIR`, passed as a path to `stt_model.generate(path, language=lang)`,
  and `os.remove()`'d in a `finally`. The Cohere model's internal
  `_to_mono` → `load_audio` path handles decoding for any format librosa
  supports (wav, mp3, m4a, flac, ogg, …).
- **Response formats**: `json` (default, `{"text": ...}`), `text`,
  `verbose_json` (OpenAI segment shape), `srt`, `vtt`. The last two are built
  from `STTOutput.segments` via `_to_srt` / `_to_vtt`.
- **`/v1/audio/translations`**: forwards to transcription with `language=en`.
  Cohere Transcribe is ASR-only, not a translator — this exists purely so
  clients that default to the translations endpoint still get something
  usable.
- **`prompt` and `temperature`**: accepted in the form for OpenAI client
  compatibility, silently ignored. Cohere Transcribe exposes neither.

## Ground truth from testing (2026-04-10)

- End-to-end tested against
  `/Users/piercecohen/mlx/mlx-audio-tests/pierce-voice-note.m4a` on an M5 Max:
  - Cold load: ~1.1 s from HF cache
  - Per-request: ~0.7 s for a 35 s clip
  - Peak memory: ~4.74 GB (stays resident for process lifetime)
- All four `response_format` values verified returning the expected shape.
- Output byte-matches the reference `python -m mlx_audio.stt.generate` CLI.

## Spokenly gotcha — DO NOT FORGET

Spokenly's "Base URL" field wants **just the host** (`http://127.0.0.1:8765`),
not `http://127.0.0.1:8765/v1`. Spokenly appends `/v1/audio/transcriptions`
itself. Pierce hit this during initial setup. Plain OpenAI SDK clients
(`openai` Python lib, etc.) *do* want the `/v1` suffix — this quirk is
Spokenly-specific.

## What Cohere Transcribe does NOT support

Researched and confirmed against the HF model card and Cohere's blog/launch
coverage (2026-04-10):

- **No keyterms / hotwords / context biasing / custom vocabulary.** The
  upstream processor signature is
  `processor(audio, sampling_rate, language, punctuation)`. That's it.
  Don't waste time trying to wire a `prompt` or `context` field into the
  Cohere path — it has nowhere to go. Cohere's *paid cloud* Transcribe is a
  separate product; don't confuse the two.
- **No streaming.** `cohere_asr.py` raises `NotImplementedError` on
  `stream=True`.
- **14 languages only**: en, fr, de, it, es, pt, el, nl, pl, zh, ja, ko, vi, ar.

If Pierce ever wants hotword biasing, the drop-in alternatives are:
- **Whisper** (`mlx-community/whisper-large-v3-turbo`) — has `initial_prompt`.
- **Qwen3-ASR** — has a context field; see
  `../mlx-audio/examples/qwen3_asr_transcription.py`.

Either would require ~10 lines in `create_transcription()` to wire the
`prompt` form field through to the model's prompt/context kwarg.

## Logs and privacy

- No persistent logs. Uvicorn's access log (URL + status code, no body) goes
  to stdout/stderr. My code only prints model-load events.
- No audio retention. Temp files are deleted immediately after each request
  in a `finally` block. Only orphan risk is a hard SIGKILL between write and
  delete; macOS cleans `$TMPDIR` periodically regardless.
- No network calls outside localhost after initial model download.

## Dependencies on sibling repos

`mlx-audio` is assumed to live at `../mlx-audio` (i.e.
`/Users/piercecohen/mlx/mlx-audio`). The server imports it via normal
`pip install mlx-audio[stt,server]`, so a local editable install is
optional — but if Pierce is hacking on mlx-audio itself, point pip at the
local path:

```bash
pip install -e ../mlx-audio[stt,server]
```

When reading mlx-audio internals for this project, the relevant files are:
- `mlx_audio/stt/__init__.py` → `load`, `load_model`
- `mlx_audio/stt/models/cohere_asr/cohere_asr.py` → `generate()` at line 972
- `mlx_audio/stt/models/base.py` → `STTOutput` dataclass
- `mlx_audio/server.py` → upstream's own server (different shape — returns
  ndjson, not OpenAI JSON). We intentionally do NOT use it.

## Running persistently

Pierce went with the ad-hoc wrapper (`scripts/cohere` + shell aliases) rather
than launchd. The model only sits resident while actively dictating — he
spins it up via `cohere-start` and tears it down via `cohere-stop`. If he
later wants always-on-at-login, a launchd plist is still the right upgrade
path — but don't proactively add it without being asked.

### `scripts/cohere` gotcha

The wrapper uses `set -euo pipefail` + `nohup ... &` + `disown`. The empty
extra-args array has to use the `${ARR[@]+"${ARR[@]}"}` safe-expansion
pattern, because `set -u` on an empty `"${ARR[@]}"` will kill the
backgrounded child before it exec's python. Don't "simplify" that line.

### Orphan PID caveat

If a previous `python server.py` session is still holding port 8765, a new
`cohere-start` will succeed (nohup doesn't error on EADDRINUSE — uvicorn
does), the backgrounded python will die seconds later, and the PID file
will be stale. Symptom: `cohere-status` says "not running" but `curl
http://127.0.0.1:8765/v1/models` still works. Fix: `lsof -iTCP:8765
-sTCP:LISTEN`, kill the real pid, `rm ~/.cache/cohere-stt/server.pid`,
retry `cohere-start`.
