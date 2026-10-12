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
| LLM | Qwen 3.8 27B (open weights) | Groq free tier |
| TTS | Kokoro-82M | local |

No frontier or paid APIs. Earlier experiments (Gemini, Cartesia, MiniCPM-o,
VAP diagnostics) are retired and live only in Git history.

## Status

Working end to end: speak, pause, and the assistant answers aloud. Measured on an
M2 / 8 GB with recorded speech over the real WebSocket (3 turns):

| Stage | Time |
|---|---|
| End of turn (Silero pause + Smart Turn) | 192 ms |
| STT (Groq Whisper) | 224–341 ms |
| LLM first token (Groq Qwen, reasoning off) | 161–317 ms |
| TTS first piece (Kokoro, local) | 515–1320 ms |
| **User stops speaking to first audio sent** | **1.1–2.2 s** |

How a call flows (`app/pipeline.py`):

1. The browser streams 100 ms packets of 16 kHz PCM over `/ws/voice`.
2. `TurnDetector`: Silero scores every 32 ms. After 200 ms of silence, Smart Turn
   scores the whole utterance; "complete" ends the turn, otherwise it waits for
   more speech or a 1.5 s timeout.
3. `Call.respond`: Groq Whisper transcribes the turn; the LangGraph agent
   (`app/database.py`) streams the reply from Groq and checkpoints history.
4. `SentenceBuffer` cuts streamed text into speakable pieces (the first may end
   at a comma) and Kokoro synthesizes them while the LLM keeps generating.
5. Audio goes back in 200 ms packets; the browser plays them gap-free.

Events sent to the browser: `ready`, `transcript`, `text` (reply deltas),
`metrics`, `notice`, `reply_end`, `interrupt`, `error`. Postgres is used when running,
otherwise history is in memory (logged at startup).

### Barge-in

Talking over the assistant stops it, the way OpenAI Realtime, LiveKit and
Pipecat handle interruptions:

1. While a reply is in progress, 250 ms of confident speech (Silero >= 0.8)
   counts as an interruption. Shorter sounds (a cough, "mm-hmm") are ignored.
2. The server cancels the LLM and TTS and sends `interrupt`. The browser flushes
   queued audio and answers `{"type": "interrupted", "played_s": ...}`.
3. History keeps only what was heard: whole sentences that played, plus the
   playing sentence cut by word proportion, stored with status `interrupted`.
4. The interrupting speech continues as the user's next turn.

Measured: audio stops about 300 ms after the user starts talking. Barge-in is
disabled when the browser reports no echo cancellation, because the assistant's
own voice would otherwise interrupt it. Headphones are the most reliable setup.

### Groq reliability

Free-tier limits for the LLM: 30 requests per minute (only stated in 429
bodies), 1,000 per day and 8,000 tokens per minute; Whisper allows 2,000 per day.
Each user turn makes two requests (STT and LLM).

- One shared Groq client for the process (connection reuse; first token about
  110-170 ms instead of opening a TLS connection per request).
- A 429 asking for a wait of 3 s or less is waited out and retried once.
- Groq occasionally sends no first token. After 3.5 s the request is abandoned
  and retried once. Root cause is not confirmed: not rate limiting (that returns
  an immediate 429) and no event-loop blocking was seen. A watchdog logs any
  event-loop block over 250 ms, and slow first tokens are logged per attempt.

Next: cut first-sentence TTS time, tool calling, then post-call Temporal and
CrewAI workflows.

## Run locally

Create `.env` from `.env.example` and set `GROQ_API_KEY`. Never commit keys.
Download the local models once (~365 MB) with `scripts/download-models.sh`.

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
