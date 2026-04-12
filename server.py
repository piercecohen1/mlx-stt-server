"""OpenAI-compatible transcription server backed by mlx-audio.

Exposes /v1/audio/transcriptions with optional bearer-token auth so it can
serve as a local or tunnel-exposed STT endpoint for any OpenAI-compatible
client.

Local mode (no key file):
    python server.py

With auth (requires ~/.config/mlx-stt-server/api-key):
    python server.py --require-key
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import secrets
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mlx_audio.stt import load as load_stt_model

DEFAULT_MODEL = "CohereLabs/cohere-transcribe-03-2026"

# Serialize model.generate() calls. The model holds GPU state and MLX is
# not designed for concurrent invocations; without this, a burst of authed
# requests can wedge the daemon. Requests queue in Starlette's event loop
# and each gets its turn at the semaphore.
_generate_semaphore = asyncio.Semaphore(1)

_model_cache: dict = {}
_model_accepts_language: dict = {}
_preload_model: Optional[str] = os.environ.get("MLX_STT_MODEL", DEFAULT_MODEL)
if os.environ.get("MLX_STT_NO_PRELOAD", "").lower() in ("1", "true", "yes"):
    _preload_model = None


def get_model(model_id: str):
    if model_id not in _model_cache:
        print(f"[mlx-stt-server] loading model: {model_id!r}", flush=True)
        t0 = time.time()
        model = load_stt_model(model_id)
        try:
            sig = inspect.signature(model.generate)
            _model_accepts_language[model_id] = "language" in sig.parameters
        except (ValueError, TypeError):
            _model_accepts_language[model_id] = False
        _model_cache[model_id] = model
        print(
            f"[mlx-stt-server] loaded {model_id} in {time.time() - t0:.1f}s",
            flush=True,
        )
    return _model_cache[model_id]


# ── File-based key loading (TOCTOU-safe) ─────────────────────────────

DEFAULT_KEY_FILE = os.path.expanduser("~/.config/mlx-stt-server/api-key")


def _load_api_key() -> Optional[str]:
    path = os.environ.get("MLX_STT_API_KEY_FILE") or DEFAULT_KEY_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
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


# ── ASGI middlewares ─────────────────────────────────────────────────

MAX_UPLOAD_BYTES = int(os.environ.get("MLX_STT_MAX_UPLOAD_MB", "25")) * 1024 * 1024
UNAUTH_PATHS: frozenset[str] = frozenset({"/healthz"})


class AuthMiddleware:
    """Reject requests without a valid Bearer token before routing."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
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


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Fast path: reject up front if Content-Length exceeds the cap.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    break
                if length > self.max_bytes:
                    await self._send_413(send)
                    return
                # Content-Length is within limit; skip streaming counter
                # since we already know the total size.
                return await self.app(scope, receive, send)

        # Streaming path: count bytes for chunked transfers that lack
        # Content-Length.  Wraps receive to count body bytes and send to
        # intercept the response if the cap is exceeded.
        received = 0
        exceeded = False
        max_bytes = self.max_bytes

        async def capped_receive():
            nonlocal received, exceeded
            msg = await receive()
            if msg.get("type") == "http.request" and not exceeded:
                received += len(msg.get("body", b""))
                if received > max_bytes:
                    exceeded = True
                    # Return end-of-body to stop further parsing.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                if exceeded:
                    # Replace whatever status the app chose with 413.
                    message = {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                        ],
                    }
            elif message["type"] == "http.response.body" and exceeded:
                message = {
                    "type": "http.response.body",
                    "body": b"Payload too large\n",
                }
            await send(message)

        await self.app(scope, capped_receive, guarded_send)

    @staticmethod
    async def _send_413(send: Send):
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({
            "type": "http.response.body",
            "body": b"Payload too large\n",
        })


# ── Formatting helpers ───────────────────────────────────────────────

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


# ── App setup ────────────────────────────────────────────────────────

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


app = FastAPI(
    title="mlx-audio OpenAI-compatible STT",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Middleware runs in reverse-registration order (LIFO). Auth outer
# (runs first), MaxBodySize inner. Unauth requests rejected before
# any body parsing; oversized authed requests 413'd before form parser.
app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_UPLOAD_BYTES)
if _API_KEY is not None:
    app.add_middleware(AuthMiddleware, api_key=_API_KEY)


# ── Routes ───────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


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

    The `model` form field is accepted for client compatibility but IGNORED:
    the server only ever runs DEFAULT_MODEL. Accepting arbitrary model IDs
    would let an authenticated client trigger code execution via mlx-audio
    backends that pass trust_remote_code=True to transformers.from_pretrained.
    `prompt` and `temperature` are similarly ignored (the Cohere model
    doesn't expose them).
    """
    lang = (language or "en").lower()

    # Bind tmp_path before writing so the outer finally can always clean up,
    # even if the client disconnects mid-upload or file.read() raises.
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp_path = tmp.name
    try:
        try:
            # Stream in chunks instead of slurping the whole body into RAM.
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        finally:
            tmp.close()

        try:
            stt_model = get_model(DEFAULT_MODEL)
        except Exception as e:
            print(
                f"[mlx-stt-server] load failed: {type(e).__name__}: {str(e)[:200]!r}",
                file=sys.stderr,
                flush=True,
            )
            raise HTTPException(status_code=400, detail="Model unavailable")
        accepts_language = _model_accepts_language.get(DEFAULT_MODEL, False)

        try:
            # Serialize model.generate calls (MLX + GPU state is not
            # designed for concurrent invocation) and offload to a thread
            # so the event loop stays responsive for /healthz.
            async with _generate_semaphore:
                if accepts_language:
                    result = await asyncio.to_thread(
                        stt_model.generate, tmp_path, language=lang
                    )
                else:
                    result = await asyncio.to_thread(stt_model.generate, tmp_path)
        except Exception as e:
            print(
                f"[mlx-stt-server] transcribe failed: {type(e).__name__}: {str(e)[:200]!r}",
                file=sys.stderr,
                flush=True,
            )
            raise HTTPException(status_code=500, detail="Transcription failed")
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
    parser.add_argument(
        "--require-key",
        action="store_true",
        help="Refuse to start if no valid API key file is found.",
    )
    args = parser.parse_args()

    _preload_model = None if args.no_preload else args.model
    if args.require_key:
        os.environ["MLX_STT_REQUIRE_KEY"] = "1"
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
