# Voice Agent Lab

A learning project: stateless Gemini text chat over WebSockets, then speech.
Each model call receives only the current message. Earlier turns remain visible
in the browser but are not sent to Gemini. Replies arrive whole, not streamed.
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
4. `app/llm.py`: one independent Gemini request, with no history or tools.
5. `tests/test_app.py`: local tests with the provider mocked.

## Completed milestone: stateless chat

- Browser connects to `/ws/chat` on the same host.
- Server sends each message independently to Gemini.
- The API key stays on the backend; the model is configurable via GEMINI_MODEL.
- One request at a time per connection, with a timeout and visible errors.
- Reload to reconnect; no automatic retry or provider cancellation on disconnect.

Explain the path of one message before adding streaming or conversation history.

## Later milestones

Streaming LLM chat with in-memory history → push-to-talk ASR/TTS → continuous
audio and endpointing → tools and durable state → barge-in and recovery.

No agent framework, database, authentication, telephony, or deployment yet.
This is local-only learning scaffolding, not a production service.
