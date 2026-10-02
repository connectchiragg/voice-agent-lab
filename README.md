# Voice Agent Lab

A learning project: Gemini text chat with conversation IDs, then speech.
Each model call receives previous turns (including marked partial replies) and the current message.
History lives in LangGraph PostgreSQL checkpoints, keyed by UUID (`thread_id`). The browser keeps its ID in
sessionStorage and sends it when connecting. Refreshing restores previous messages;
the New conversation button creates a new ID. Replies stream into one browser message.
Server restarts preserve saved history. Use one server process for now: the
per-conversation locks are in memory and do not coordinate multiple workers.
IDs identify conversations; they are not login credentials.
Create a local `.env` from `.env.example` and fill in your Gemini API key.
Prompts are sent to Google. Never put credentials in frontend code or Git.

## Speech input (first audio increment)

Set `GROQ_API_KEY` in the backend `.env`, then restart the app. Text chat still
works without this key. `GROQ_TRANSCRIPTION_MODEL` defaults to
`whisper-large-v3-turbo`: a speed-oriented multilingual transcription model.
We use the Groq SDK for typed async calls, with a 30-second provider timeout
and no automatic retries.

Click Record, allow microphone access, then Stop. The browser sends one
complete audio clip to `/api/transcribe`. The transcript fills the existing
message field: review/edit it and click Send to use our unchanged Gemini and
LangGraph flow. Transcribing alone does not add a conversation turn.

- Requires localhost or HTTPS and browser microphone permission.
- The UI stops recording after 60 seconds; the backend limits uploads to 5 MB.
- WebM/MP4/Ogg browser recordings and WAV/MP3 uploads are accepted.
- Audio goes to Groq and is held temporarily in memory, not saved to disk or PostgreSQL.
- A failed transcription leaves existing typed text and chat history unchanged.
- No continuous listening, streaming transcription or automatic message submission.
- Browser recordings are uploaded as captured; we do not implement resampling yet.
- Free quotas vary by account; see https://console.groq.com/docs/rate-limits.

## Speech output (second audio increment)

Completed replies up to 200 characters have a Play button. It sends the reply
to `/api/speak`, which calls Groq and returns WAV bytes; the browser plays them.
`GROQ_SPEECH_MODEL` defaults to `canopylabs/orpheus-v1-english` and
`GROQ_SPEECH_VOICE` defaults to `troy`, using the same backend key as transcription.

- English output only. Multilingual transcription does not imply multilingual TTS.
- Before first use, Groq may require your account's org admin to accept Orpheus
  model terms in the Groq console. The app reports this setup requirement explicitly.
- Groq currently limits Orpheus input to 200 characters. Longer text replies stay
  intact but cannot be played yet; we do not truncate, summarize or split them.
- Only completed replies get Play. Interrupted text remains visible as before.
- Click-to-play is deliberate: no autoplay, streaming TTS or barge-in yet.
- Audio controls can pause/replay the current WAV without another provider call.
  Clicking a message's Play button again makes a new request and uses quota.
- Starting a recording pauses existing playback. Generated audio is not persisted.
- Failed speech generation does not change Gemini text or conversation history.
- Model limits and voices: https://console.groq.com/docs/text-to-speech/orpheus.

## Run locally

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. A GEMINI_API_KEY is required for real responses.

```sh
python -m pytest
```

## Read these files in order

1. `app/main.py`: HTTP routes, WebSocket handler and request validation.
2. `static/index.html`: the message form and conversation area.
3. `static/chat.js`: WebSocket connection and safe message rendering.
4. `app/llm.py`: sends history and the new message to Gemini, without tools.
5. `app/database.py`: typed conversation state, Gemini graph node, and PostgreSQL checkpoints.
6. `tests/test_app.py`: local tests with a mocked provider and an in-memory LangGraph checkpointer.
7. `app/speech.py` and `static/speech.js`: speech provider calls and browser recording, separate from chat.

## Completed milestone: conversation history

LangGraph now executes the conversation: `START → generate_reply → END`.
`Conversation` contains `messages` and `updated_at`. `stream_conversation`
loads the previous state and submits a user message and an empty assistant
message to `graph.astream`. Synchronous checkpoint durability saves that input
before the Gemini node runs. The node calls our existing Gemini SDK stream and
emits `start`/`delta` events through LangGraph's custom stream. The WebSocket
handler forwards these events and sends `done` only when the graph finishes.

The node explicitly checkpoints each accumulated partial reply before emitting
its delta. This is an `aupdate_state` attributed to `generate_reply`; it does
not run the node again. A provider failure or cancellation leaves that partial
state marked interrupted. After normal generation, the node returns completed
state for LangGraph to checkpoint. `aclosing` closes the graph and provider
generators when streaming stops. No automatic resume or retry is performed.

Startup runs the PostgreSQL checkpointer's setup, creating `checkpoints`,
`checkpoint_blobs`, `checkpoint_writes`, and `checkpoint_migrations`.
These are the only application tables. Messages and `updated_at` live in graph state.

Preserving every partial reply requires an explicit checkpoint before each
UI chunk. This stores more data than updating one SQL message row because the
messages list is versioned. This is a learning tradeoff, not a throughput
optimization. We retain the single-process conversation lock: persistence
does not itself serialize concurrent requests for the same conversation.

- Browser connects to `/ws/chat` on the same host.
- Server sends previous turns with each new message to Gemini.
- Connections using the same ID share history; new IDs start empty.
- A per-conversation lock serializes requests for the same ID.
- User messages are saved before generation. Assistant messages start as interrupted
  and become completed only when Gemini finishes normally.
- Each text chunk is persisted before sending, updating conversation updated_at.
  This favors simple recovery over throughput; batching writes is a later optimization.
- Interrupted replies stay visible and enter future context with an interruption note.
- No automatic retry: the user can send another message, including "continue".
- On browser disconnect detected during sending, generation stops; saved text remains.
- Storage failures stop delivery; the UI reports interruption, and only committed chunks survive.
- History is not trimmed yet; long conversations can hit model limits.
- The API key stays on the backend; the model is configurable via GEMINI_MODEL.
- One request at a time per connection, with a timeout and visible errors.
- Reload to reconnect. A disconnect detected while sending closes the stream;
  disconnect detection while awaiting a provider chunk is not implemented yet.

The WebSocket sends start, delta, done or interrupted events for each response.

## Later milestones

Full-length/streaming speech → continuous audio and endpointing → tools →
barge-in and recovery. PostgreSQL conversation state is already implemented.

No application authentication, telephony, or deployment yet.
This is local-only learning scaffolding, not a production service.

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
