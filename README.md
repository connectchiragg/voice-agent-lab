# Voice Agent Lab

A local, full-duplex voice agent built on a cascaded, open-weight stack:

```
mic (16 kHz PCM) -> VAD + turn detection -> STT -> LLM -> TTS -> speaker (24 kHz PCM)
```

| Stage | Choice | Runs |
|---|---|---|
| VAD | Silero VAD | local CPU |
| Turn detection | Smart Turn v3 | local CPU |
| STT | Whisper large-v3-turbo | Groq free tier |
| LLM | open-weight model (gpt-oss / Llama) | Groq free tier |
| TTS | Kokoro-82M | local |

No frontier or paid APIs. Earlier experiments (Gemini, Cartesia, MiniCPM-o,
VAP diagnostics) are retired and live only in Git history.

## Status

The pipeline is being rebuilt. What exists today:

- Browser call UI: AudioWorklet microphone capture in 100 ms, 16 kHz float32 packets
  (`static/microphone-worklet.js`) and a raw-PCM Web Audio player
  (`static/audio-player.js`).
- `/ws/voice` validates the call handshake, then reports
  `Voice pipeline not built yet.`
- `app/speech.py`: Groq transcription of complete clips.
- `app/database.py`: LangGraph conversation state with Postgres checkpoints.
  Its `stream_reply` LLM seam is a stub until the Groq LLM is wired in.

Next: the streaming cascaded loop, then barge-in (`player.interrupt()` tests are
already written as todo), tool calling, per-stage latency metrics, then post-call
Temporal and CrewAI workflows.

## Run locally

Create `.env` from `.env.example` and set `GROQ_API_KEY`. Never commit keys.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Microphone capture needs localhost or HTTPS.

```sh
python -m pytest
node --test tests/audio-player.test.mjs
node tests/audio-worklet.test.mjs
```

## Local PostgreSQL and pgAdmin

Copy `infra.env.example` to `.env.docker` and choose two passwords on a fresh setup.

```sh
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker stop
```

- PostgreSQL: `localhost:5432`, database and user `voice_agent`, password from
  `POSTGRES_PASSWORD` in `.env.docker`.
- pgAdmin: http://localhost:5050, login `admin@example.com`, password from
  `PGADMIN_PASSWORD`. The registered server uses host `postgres` inside Docker.
- Both ports bind to loopback only. Named volumes keep data; avoid `down -v`
  unless you mean to erase it.
