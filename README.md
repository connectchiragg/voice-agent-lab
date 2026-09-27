# Voice Agent Lab

A learning project: Gemini text chat with conversation IDs, then speech.
Each model call receives the successful previous turns and the current message.
History lives in a server dictionary keyed by UUID. The browser keeps its ID in
sessionStorage and sends it when connecting. Refreshing restores previous messages;
the New conversation button creates a new ID. Replies arrive whole, not streamed.
Server restarts (including development reloads) clear all histories. Use one server
process: this dictionary is not shared between workers. Conversations have no expiry
yet, so memory grows until restart. IDs identify conversations; they are not login credentials.
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
5. `tests/test_app.py`: local tests with the provider mocked.

## Completed milestone: conversation history

- Browser connects to `/ws/chat` on the same host.
- Server sends previous successful turns with each new message to Gemini.
- Connections using the same ID share history; new IDs start empty.
- A per-conversation lock serializes requests for the same ID.
- Failed requests leave history unchanged.
- History is not persisted or trimmed yet; long conversations can hit model limits.
- The API key stays on the backend; the model is configurable via GEMINI_MODEL.
- One request at a time per connection, with a timeout and visible errors.
- Reload to reconnect; no automatic retry or provider cancellation on disconnect.

Explain the path of two messages before adding streaming.

## Later milestones

Streaming LLM chat → push-to-talk ASR/TTS → continuous
audio and endpointing → tools and durable state → barge-in and recovery.

No agent framework, database, authentication, telephony, or deployment yet.
This is local-only learning scaffolding, not a production service.
