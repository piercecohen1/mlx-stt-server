"""OpenAI-compatible transcription server backed by mlx-audio.

Exposes the subset of OpenAI's /v1/audio/transcriptions API that dictation
clients like Spokenly actually call, so a local MLX model (e.g. Cohere
Transcribe) can be dropped in as a drop-in replacement for the cloud API.

Run:
    python server.py                     # preloads Cohere Transcribe on :8765
    python server.py --port 8123
    python server.py --no-preload        # load on first request instead

Then point Spokenly at http://127.0.0.1:8765/v1 with any placeholder API key.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from mlx_audio.stt import load as load_stt_model

DEFAULT_MODEL = "CohereLabs/cohere-transcribe-03-2026"

_model_cache: dict = {}
_preload_model: Optional[str] = os.environ.get("MLX_STT_MODEL", DEFAULT_MODEL)
if os.environ.get("MLX_STT_NO_PRELOAD", "").lower() in ("1", "true", "yes"):
    _preload_model = None


def get_model(model_id: str):
    if model_id not in _model_cache:
        print(f"[mlx-stt-server] loading model: {model_id}", flush=True)
        t0 = time.time()
        _model_cache[model_id] = load_stt_model(model_id)
        print(
            f"[mlx-stt-server] loaded {model_id} in {time.time() - t0:.1f}s",
            flush=True,
        )
    return _model_cache[model_id]


def _format_timestamp(seconds: float, decimal_sep: str = ",") -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace(".", decimal_sep)


def _segments(result) -> list:
    segs = getattr(result, "segments", None) or []
    return [s for s in segs if isinstance(s, dict) and "start" in s and "end" in s]


def _to_srt(result) -> str:
    lines = []
    for i, seg in enumerate(_segments(result), 1):
        lines.append(str(i))
        lines.append(
            f"{_format_timestamp(seg['start'])} --> {_format_timestamp(seg['end'])}"
        )
        lines.append((seg.get("text") or "").strip())
        lines.append("")
    return "\n".join(lines) if lines else result.text


def _to_vtt(result) -> str:
    lines = ["WEBVTT", ""]
    for seg in _segments(result):
        lines.append(
            f"{_format_timestamp(seg['start'], '.')} --> {_format_timestamp(seg['end'], '.')}"
        )
        lines.append((seg.get("text") or "").strip())
        lines.append("")
    return "\n".join(lines)


def _verbose_json(result, language: str) -> dict:
    segs = _segments(result)
    segments_out = []
    for i, seg in enumerate(segs):
        segments_out.append(
            {
                "id": i,
                "seek": 0,
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": seg.get("text", ""),
                "tokens": [],
                "temperature": 0.0,
                "avg_logprob": 0.0,
                "compression_ratio": 0.0,
                "no_speech_prob": 0.0,
            }
        )
    duration = float(segs[-1]["end"]) if segs else 0.0
    return {
        "task": "transcribe",
        "language": getattr(result, "language", None) or language,
        "duration": duration,
        "text": result.text,
        "segments": segments_out,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _preload_model:
        try:
            get_model(_preload_model)
        except Exception as e:
            print(
                f"[mlx-stt-server] failed to preload {_preload_model}: {e}",
                file=sys.stderr,
                flush=True,
            )
    yield


app = FastAPI(title="mlx-audio OpenAI-compatible STT", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "service": "mlx-audio OpenAI-compatible STT",
        "endpoints": ["/v1/models", "/v1/audio/transcriptions"],
        "default_model": DEFAULT_MODEL,
        "loaded_models": list(_model_cache.keys()),
    }


@app.get("/v1/models")
async def list_models():
    ids = sorted({DEFAULT_MODEL, *_model_cache.keys()})
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mlx-audio",
            }
            for mid in ids
        ],
    }


@app.post("/v1/audio/transcriptions")
async def create_transcription(
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: str = Form("json"),
    temperature: Optional[float] = Form(None),
):
    """OpenAI-compatible transcription endpoint.

    Accepts the same multipart/form-data shape as OpenAI:
        file, model, language, prompt, response_format, temperature
    Supported response_format values: json, text, verbose_json, srt, vtt.
    `prompt` and `temperature` are accepted for client compatibility but
    ignored (the underlying Cohere model doesn't expose them).
    """
    lang = (language or "en").lower()

    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        try:
            stt_model = get_model(model)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to load model '{model}': {e}")

        try:
            result = stt_model.generate(tmp_path, language=lang)
        except TypeError:
            result = stt_model.generate(tmp_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    fmt = (response_format or "json").lower()
    if fmt == "text":
        return PlainTextResponse(result.text)
    if fmt == "srt":
        return PlainTextResponse(_to_srt(result), media_type="application/x-subrip")
    if fmt == "vtt":
        return PlainTextResponse(_to_vtt(result), media_type="text/vtt")
    if fmt == "verbose_json":
        return JSONResponse(_verbose_json(result, lang))
    return JSONResponse({"text": result.text})


@app.post("/v1/audio/translations")
async def create_translation(
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    prompt: Optional[str] = Form(None),
    response_format: str = Form("json"),
    temperature: Optional[float] = Form(None),
):
    """OpenAI translations endpoint — Cohere Transcribe is ASR-only, so this
    forwards to transcription with language='en' so clients that default to
    the translations endpoint still get a usable result."""
    return await create_transcription(
        file=file,
        model=model,
        language="en",
        prompt=prompt,
        response_format=response_format,
        temperature=temperature,
    )


def main():
    global _preload_model
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model to preload at startup (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="Skip preloading; load on first request instead.",
    )
    args = parser.parse_args()

    _preload_model = None if args.no_preload else args.model
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
