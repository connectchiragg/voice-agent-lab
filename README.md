# Voice Agent Lab

A learning project: Gemini text chat with conversation IDs, then speech.
Each model call receives the successful previous turns and the current message.
History lives in PostgreSQL, keyed by UUID. The browser keeps its ID in
sessionStorage and sends it when connecting. Refreshing restores previous messages;
the New conversation button creates a new ID. Replies arrive whole, not streamed.
Server restarts preserve saved history. Use one server process for now: the
per-conversation locks are in memory and do not coordinate multiple workers.
IDs identify conversations; they are not login credentials.
Create a local `.env` from `.env.example` and fill in your Gemini API key.
Prompts are sent to Google. Never put credentials in frontend code or Git.

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
5. `app/database.py`: direct SQL for loading history and saving exchanges.
6. `tests/test_app.py`: local tests with the provider and storage mocked.

## Completed milestone: conversation history

- Browser connects to `/ws/chat` on the same host.
- Server sends previous successful turns with each new message to Gemini.
- Connections using the same ID share history; new IDs start empty.
- A per-conversation lock serializes requests for the same ID.
- Failed requests leave history unchanged.
- Both messages and conversation updated_at are saved in one transaction.
- History is not trimmed yet; long conversations can hit model limits.
- The API key stays on the backend; the model is configurable via GEMINI_MODEL.
- One request at a time per connection, with a timeout and visible errors.
- Reload to reconnect; no automatic retry or provider cancellation on disconnect.

Explain the path of two messages before adding streaming.

## Later milestones

Streaming LLM chat → push-to-talk ASR/TTS → continuous
audio and endpointing → tools and durable state → barge-in and recovery.

No agent framework, application authentication, telephony, or deployment yet.
This is local-only learning scaffolding, not a production service.

## Local PostgreSQL and pgAdmin

The app connects to PostgreSQL on localhost:5432 using psycopg's async driver.
Copy `infra.env.example` to `.env.docker` and choose two passwords on a fresh setup.
The existing local setup already has generated passwords there; this file is ignored by Git.

```sh
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker stop
```

For a fresh database only, create the tables before starting the app:

```sh
docker compose --env-file .env.docker exec -T postgres psql -v ON_ERROR_STOP=1 -U voice_agent -d voice_agent < infra/schema.sql
```

The current local database already has these tables. Existing in-memory chats
are not migrated; only new successful exchanges are saved to PostgreSQL.

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
