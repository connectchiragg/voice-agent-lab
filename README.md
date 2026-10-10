# Voice Agent Lab

A local continuous-call learning project. Click Start call, speak freely, then
End call. `/ws/voice` carries 16 kHz microphone PCM upstream and 24 kHz MiniCPM
audio downstream concurrently, including while the assistant speaks. VAD emits
diagnostics only; it never gates microphone input or stops playback.

MiniCPM is now selected by the app, but real inference remains unverified.
The secure remote endpoint is not established yet. See
[the proxy plan](docs/minicpm-integration.md). Do not wake the Studio/GPU without
explicit approval. Without configured connectivity, Start call fails clearly.

The old `/ws/chat`, `/api/transcribe`, `/api/speak`, health endpoint and API docs
are removed. HTTP only serves the browser page at `/` and assets under `/static`.
The recorded-turn interface is replaced, not retained as a fallback.
`/ws/diagnostics` remains an isolated diagnostic test endpoint.
No PostgreSQL history is loaded or written in this phase. Legacy provider/state
modules remain in the repository for learning, but are not used by the call API.

## Run locally

Create `.env` from `.env.example` on a fresh setup, and fill in
`MINICPM_URL` and `MINICPM_TOKEN`. Never put tokens in frontend code or Git.
Use the experimental VAP runtime described in [speech diagnostics](SPEECH_DIAGNOSTICS.md).
PostgreSQL is not required for calls:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[diagnostics,dev]'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The generic install above does not install the patched, pinned MaAI source.
For the active VAP experiment use the runtime command in the VAP section below.

Open http://127.0.0.1:8000. Microphone recording needs localhost/HTTPS and permission.

```sh
python -m pytest
node --test tests/audio-player.test.mjs
node tests/audio-worklet.test.mjs
```

The browser requests echo cancellation (not guaranteed by every device).
AudioWorklet emits 100 ms PCM packets. Its lightweight interpolating resampler
is a learning implementation, not a production band-limited DSP resampler.
Slow microphone upload ends the call rather than silently dropping model input.
Only diagnostic copies may be skipped/reset under overload, with a warning.
Model-native interruption behavior is distinct from our diagnostic-only VAD and
must be checked during real inference. Real mic quality/AEC remain untested.

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

Server events on a successful reply:

- `transcript` with recognized text.
- Gemini sends text deltas immediately to one Cartesia speech context; the
  accumulated reply is checkpointed when generation ends, not on every chunk.
- `audio_start` with `sample_rate: 24000` and `encoding: "pcm_f32le"`.
- Binary messages containing raw mono little-endian Float32 PCM samples.
- `reply_complete` with saved text, shown once, not token by token.
- `audio_end` with `partial: false` (or `true` after a speech-stream failure).
- `done` after both generation and speech have finished.

Text and audio run concurrently: `reply_complete` can arrive before or after the
first audio. There is no promise that audio starts before the last text delta on
every real turn; provider buffering, generation speed and the network affect this.

The browser no longer needs a WAV decoder. It reads raw PCM samples, retains any
fragmented sample bytes, and schedules mono buffers with Web Audio. The format is
explicitly requested from Cartesia and announced with each audio stream. No CDN,
frontend build or audio-decoding dependency is needed.

Playback can begin before Gemini finishes generating or Cartesia finishes sending
audio. `done` does not mean playback has finished; Record remains disabled until
scheduled audio drains. A speech failure does not cancel text generation.
Closing the voice generator cancels and awaits its background jobs.

Errors are visible as `error` or `interrupted` events.
A failed turn finishes with `done` so another recording can be attempted.
Disconnect closes the current generator; detection while awaiting a provider chunk
is still limited. Reload to reconnect. Existing history is never automatically spoken.

## What this phase does and does not do

- Record/Stop is the explicit turn boundary. UI recordings stop at 60 seconds.
- Backend accepts nonempty clips up to 5 MB in WebM, MP4, Ogg, WAV or MP3 format.
- `GROQ_TRANSCRIPTION_MODEL` defaults to `whisper-large-v3-turbo`.
- `CARTESIA_MODEL` defaults to the pinned `sonic-3.6-2026-08-27` snapshot.
  `CARTESIA_VOICE` defaults to Daniel (`47c38ca4-5f35-497b-b1a3-415245fb35e1`).
- Groq transcription uses a 30-second timeout and no automatic retries.
  Cartesia opens within 15 seconds; generation and TTS each have a 90-second deadline.
  A new speech WebSocket/context is opened per turn, with no automatic retries.
- Gemini is still asked to reply in English within 200 characters. This is a prompt
  instruction, not a hard TTS limit: longer responses are saved and spoken intact.
  Speech may already have played before the final response length is known.
- After Stop, transcription, Gemini and TTS happen automatically on the same socket.
  Record is disabled during processing and active playback.
- Clicking Record unlocks Web Audio playback. Chunks play automatically with a small
  initial buffer. This increment does not retain replayable audio or provide the old
  file-based pause/replay controls. Playback completion is local, not server-acknowledged.
- Transcription failure creates no conversation turn. Gemini failure attempts one
  save of its accumulated partial text. TTS failure does not undo completed text.
- Microphone and generated audio are held temporarily in memory, not saved to disk
  or PostgreSQL. Input audio goes to Groq; transcripts and context go to Google;
  reply text goes to Cartesia.
- Input transcription is still complete-clip. Two jobs overlap Gemini generation
  and speech; all text deltas share one continuing TTS context, not one request per
  token or sentence. If Gemini fails, already-spoken words cannot be undone; saved
  partial text remains interrupted and the partial text can finish being spoken.
- Queues are in-memory and unbounded in this learning increment; short model output
  (`max_output_tokens=1024`) and turn deadlines bound ordinary use. Production would
  need explicit backpressure and output budgets.
- No continuous listening, streaming ASR, tools, VAD,
  barge-in, automatic reconnect, authentication, telephony or deployment yet.

This is local-only learning scaffolding, not a production service.
Provider documentation: https://docs.cartesia.ai/api-reference/tts/websocket
and https://console.groq.com/docs/rate-limits. Cartesia free credits are limited
and shared between its speech services; this app keeps transcription on Groq.

## Read these files in order

1. `app/main.py`: the single voice WebSocket, recording validation and socket delivery.
2. `app/voice.py`: one recording through ASR, conversation generation and TTS.
3. `static/voice.js`: browser recording, protocol events and automatic playback.
4. `app/tts.py`: Cartesia WebSocket input/output and cleanup.
5. `app/speech.py`: Groq transcription and safe provider errors.
6. `app/events.py`: small event enums/dataclasses.
7. `static/audio-player.js`: interpret raw PCM samples and schedule playback.
8. `app/llm.py`: Gemini text streaming with short English voice-style replies.
9. `app/database.py`: LangGraph conversation state and PostgreSQL checkpoints.

## Conversation persistence

The graph remains `START -> generate_reply -> END`; this migration changes the
interface, not the storage schema. A `Conversation` contains `messages` and
`updated_at`, keyed by UUID (`thread_id`). The user transcript is checkpointed
at the start of generation. Text deltas stream without database writes. LangGraph
checkpoints the complete reply at the node boundary with asynchronous durability;
a provider failure attempts one save of the accumulated interrupted reply.
A process crash or cancellation can lose in-flight text. Completed text stays
completed even if speech synthesis later fails. Saving after browser audio delivery
is not implemented yet.

Startup creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and
`checkpoint_migrations`. These are the only application tables.
Server restarts preserve saved history. There is one in-memory lock per conversation
around the complete voice turn; use one server process because these locks do not
coordinate multiple workers. History is not trimmed yet. Async checkpoints overlap
graph execution, but pending writes can be lost if the process crashes.

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
# VAP diagnostic increment

The active microphone observer uses dedicated single-channel English VAP instead
of Silero. It processes every 100 ms of 16 kHz PCM, including silence, and reports
current speech probability, `p_now` (0–600 ms), `p_future` (600–2000 ms), and CPU time.
These are observations, not interruption classifications. Playback, generation,
input forwarding and history are unchanged. Model failure disables diagnostics
without terminating a working provider call. Audio gaps reset the temporal state.

Local experimental runtime: Python 3.11 environment at
`/Users/omen/projects/vap-cpu-trial/.venv`; pinned MaAI source
`b61b96d7dd68f4047170d6cefa20ced4e940832e`. Set `VAP_MODEL_PATH` to the dedicated
mono CPC 10 Hz / 20-second checkpoint and `VAP_CPC_PATH` to its CPC encoder.
The trial source makes PyAudio optional for chunk-only input and fixes cache
directory creation. This setup is deliberately experimental, not a deployment package.
English checkpoint revision: `7135e1b30e2e660e8f40076df1872e055eaf8ddc`;
license CC BY-NC-ND 4.0: non-commercial learning only, not commercial deployment.

```sh
PYTHONPATH=/Users/omen/projects/vap-cpu-trial/MaAI-b61b96d7dd68f4047170d6cefa20ced4e940832e/src /Users/omen/projects/vap-cpu-trial/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Recorded-speech WebSocket verification passed; live microphone, echo, Hinglish,
and labeled prediction accuracy remain unverified. The displayed speech threshold
of 0.5 is provisional, not calibrated. Per-connection model instances increase memory.
