# Voice Agent Lab

A local learning project with one voice API: `/ws/voice`.
Click Record, speak, then Stop. The server transcribes the completed recording,
runs the Gemini/LangGraph conversation, and streams audio for automatic playback.
Visible transcripts are debugging output, not a text-chat input.

The old `/ws/chat`, `/api/transcribe`, `/api/speak`, health endpoint and API docs
are removed. HTTP only serves the browser page at `/` and assets under `/static`.
This is a full interface replacement, not two parallel APIs.

## Run locally

Create `.env` from `.env.example` on a fresh setup, and fill in
`GEMINI_API_KEY` and `GROQ_API_KEY`. Never put keys in frontend code or Git.
Start PostgreSQL as described below, then:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Microphone recording needs localhost/HTTPS and permission.

```sh
python -m pytest
```

## Voice WebSocket protocol

Connect to `/ws/voice?conversation_id=<UUID>`. The server returns a `history`
event with `conversation_id` and `messages`. The browser keeps its UUID in
sessionStorage; refresh restores text history, and New conversation creates a new ID.
IDs are not authentication credentials.

For each turn, send these two messages in order:

1. JSON: `{"type":"recording","media_type":"audio/webm;codecs=opus"}`
2. One binary message containing the complete recording.

After Stop, the browser does this automatically. No manual Send or Play is needed.
We deliberately still send one completed clip, not live microphone frames.

Server events, in order on a successful short reply:

- `transcribing`, then `transcript` with recognized text.
- `generating`: Gemini runs and checkpoints its text internally.
- `reply_complete` with `text`: the saved reply is shown once, not token by token.
- `synthesizing`, then `audio_start` with a `format`, for example:
  `{"sample_rate":24000,"channels":1,"encoding":"pcm_s16le"}`.
- Multiple binary messages containing aligned signed 16-bit little-endian PCM.
  Format is announced once per reply, not before each chunk.
- `audio_end` with `partial: false` (or `true` after a speech-stream failure).
- `done`: the server is ready for another turn.

The browser schedules arriving PCM chunks with Web Audio. It can play before the
provider finishes sending the response. `done` does not mean playback has finished;
Record stays disabled until the scheduled audio drains.

Errors are visible as `error` or `interrupted` events; `notice` explains limitations.
A failed turn finishes with `done` so another recording can be attempted.
Disconnect closes the current generator; detection while awaiting a provider chunk
is still limited. Reload to reconnect. Existing history is never automatically spoken.

## What this phase does and does not do

- Record/Stop is the explicit turn boundary. UI recordings stop at 60 seconds.
- Backend accepts nonempty clips up to 5 MB in WebM, MP4, Ogg, WAV or MP3 format.
- `GROQ_TRANSCRIPTION_MODEL` defaults to `whisper-large-v3-turbo`.
- `GROQ_SPEECH_MODEL` defaults to `canopylabs/orpheus-v1-english`,
  with `GROQ_SPEECH_VOICE=troy`. Both speech stages use the same backend key.
- Groq provider requests use a 30-second timeout and no automatic retries.
  The complete TTS stream also has a 45-second deadline.
- Speech output is English and limited to 200 characters. Gemini is asked to stay
  within that limit, but the server still checks it. Oversized replies stay intact
  as text; no truncation, summarization, splitting or TTS call is performed.
- Groq may require the org admin to accept Orpheus model terms before first use.
- After Stop, transcription, Gemini and TTS happen automatically on the same socket.
  Record is disabled during processing and active playback.
- Clicking Record unlocks Web Audio playback. Chunks play automatically with a small
  initial buffer. This increment does not retain a replayable WAV or provide the old
  file-based pause/replay controls. Playback completion is local, not server-acknowledged.
- Transcription failure creates no conversation turn. Gemini failure preserves
  successfully checkpointed partial text. TTS failure does not undo completed text.
- Microphone and generated audio are held temporarily in memory, not saved to disk
  or PostgreSQL. Audio goes to Groq; transcripts and context go to Google.
- Input transcription is still complete-clip. Gemini finishes before TTS begins;
  there is no LLM/TTS overlap, background producer, or sentence chunking.
- No continuous listening, streaming ASR, tools, VAD,
  barge-in, automatic reconnect, authentication, telephony or deployment yet.

This is local-only learning scaffolding, not a production service.
Provider limits: https://console.groq.com/docs/text-to-speech/orpheus
and https://console.groq.com/docs/rate-limits.

## Read these files in order

1. `app/main.py`: the single voice WebSocket, recording validation and socket delivery.
2. `app/voice.py`: one recording through ASR, conversation generation and TTS.
3. `static/voice.js`: browser recording, protocol events and automatic playback.
4. `app/speech.py`: direct Groq speech calls and safe provider errors.
5. `app/events.py`: small event enums/dataclasses and the PCM audio format.
6. `app/audio.py` and `static/audio-player.js`: strip the streamed WAV header,
   preserve complete sample frames, and schedule browser playback.
7. `app/llm.py`: Gemini text streaming with short English voice-style replies.
8. `app/database.py`: LangGraph conversation state and PostgreSQL checkpoints.

## Conversation persistence

The graph remains `START -> generate_reply -> END`; this migration changes the
interface, not the storage schema. A `Conversation` contains `messages` and
`updated_at`, keyed by UUID (`thread_id`). The user transcript is checkpointed
before generation. Each accumulated text chunk is saved before delivery and
marked interrupted until Gemini finishes normally. Completed text stays completed
even if speech synthesis later fails.

Startup creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and
`checkpoint_migrations`. These are the only application tables.
Server restarts preserve saved history. There is one in-memory lock per conversation
around the complete voice turn; use one server process because these locks do not
coordinate multiple workers. History is not trimmed yet. Frequent checkpoints
favor learning/recovery over storage efficiency and throughput.

## Local PostgreSQL and pgAdmin

The app connects to PostgreSQL on localhost:5432 using psycopg's async driver.
Copy `infra.env.example` to `.env.docker` and choose two passwords on a fresh setup.
The existing local setup already has generated passwords there; this file is ignored by Git.

```sh
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker stop
```

Start the containers before starting the app. LangGraph creates its tables
automatically during app startup; no manual SQL schema setup is needed.

- PostgreSQL: `localhost:5432`, database/user `voice_agent`, password from
  `POSTGRES_PASSWORD` in `.env.docker`.
- pgAdmin: http://localhost:5050, login `admin@example.com`, password from
  `PGADMIN_PASSWORD` in `.env.docker`.
- The registered server "Voice Agent Lab" uses host `postgres` and port `5432`
  inside Docker. Enter the PostgreSQL password when connecting, then open Query Tool.
- Named volumes preserve database and pgAdmin data across container recreation.
  Do not use `down -v` unless you intend to erase those volumes.
- These environment variables initialize credentials on the first launch only;
  changing them later does not change passwords already stored in the volumes.
- Both published ports bind only to this Mac's loopback interface.
