# AGENTS.md

Local OpenAI-compatible speech-to-text server wrapping
[mlx-audio](https://github.com/Blaizzy/mlx-audio) on Apple Silicon —
exposes `/v1/audio/transcriptions`, default model Cohere Transcribe.
End-user usage is in the README; this file is only the context worth
reloading every session. (CLAUDE.md is a symlink to this file.)

## Run it

```
stt-server --start | --stop | --restart | --status | --logs
```

Shell alias → `scripts/stt-server`; listens on `127.0.0.1:18765` and
keeps the model resident while up. The alias hard-codes an absolute
path, so re-run `./scripts/install-aliases.sh` if you move the repo.

Also starts at login via LaunchAgent `com.mlx-stt-server`, installed by
`scripts/install-launch-agent.sh` (job output:
`~/.cache/mlx-stt-server/launchd.log`). launchd has no shell
environment, so the installer pins the concrete Python interpreter and
ffmpeg's directory into the plist at install time — re-run it too after
moving the repo, switching Python, or relocating ffmpeg. The plist
needs `AbandonProcessGroup` because the wrapper daemonizes via nohup
and exits; without it launchd kills the server on job exit.

## Memory footprint

Steady state is ~4.1 GB: ~3.9 GB model weights resident in unified
memory plus ~200 MB Python. It stays flat because `server.py` calls
`mx.clear_cache()` after every request; MLX's buffer cache is
otherwise bounded only by a limit near total system RAM, and
variable-length audio grows it by gigabytes, which then swaps and
causes multi-second first requests after idle. Check with
`top -l 1 -pid $(cat ~/.cache/mlx-stt-server/server.pid) -stats mem`
(`ps` rss under-reports Metal memory). A footprint well past ~5 GB
means the clear_cache call has regressed.

## Dependency: mlx-audio

Installed via `pip install mlx-audio[stt,server]`. To hack on it locally,
point pip at the sibling checkout (`~/mlx/mlx-audio/`):
`pip install -e ../mlx-audio[stt,server]`.

## Auth

Optional bearer token. With no key file at
`~/.config/mlx-stt-server/api-key` (or `$MLX_STT_API_KEY_FILE`), all
endpoints are open (local mode); a valid key file turns auth on
automatically. Test suite: `scripts/verify-auth.sh`.

## Dictation client (Spokenly)

Base URL is the host only — `http://127.0.0.1:18765` — **not** the `/v1`
suffix; Spokenly appends `/v1/audio/transcriptions` itself. (Plain
`openai` SDK clients *do* want `/v1`.) Model
`CohereLabs/cohere-transcribe-03-2026`.
