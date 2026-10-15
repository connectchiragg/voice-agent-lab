# Voice Agent Lab

A local, full-duplex voice agent built on a cascaded, open-weight stack:

```
mic (16 kHz PCM) -> VAD + turn detection -> STT -> LLM -> TTS -> speaker (24 kHz PCM)
```

| Stage | Choice | Runs |
|---|---|---|
| VAD | Silero VAD | local CPU |
| Turn detection | Smart Turn v3 | local CPU |
| STT (streaming) | NVIDIA Nemotron Speech Streaming EN 0.6B, int8 (sherpa-onnx) | local CPU |
| LLM | gpt-oss-120b, low reasoning (open weights) | Groq free tier |
| TTS | Kokoro-82M | local |

No frontier or paid APIs. Earlier experiments (Gemini, Cartesia, MiniCPM-o,
VAP diagnostics) are retired and live only in Git history.

## Status

Working end to end: speak, pause, and the assistant answers aloud. Measured on an
M2 / 8 GB with recorded speech over the real WebSocket (5 turns, 2026-10-13):

| Stage | Time |
|---|---|
| End of turn (Silero pause + Smart Turn) | 192 ms |
| STT final transcript (local Nemotron streaming) | 207–241 ms |
| LLM first token (Groq gpt-oss-120b, low reasoning) | 470–1050 ms |
| TTS first piece (Kokoro, local) | 660–1280 ms |
| **User stops speaking to first audio sent** | **2.0–2.2 s** |

On an 8 GB Mac this assumes enough free memory: when the system swaps, the local
models are paged out and turns take several seconds longer.

How a call flows (`app/pipeline.py`):

1. The browser streams 100 ms packets of 16 kHz PCM over `/ws/voice`.
2. `TurnDetector`: Silero scores every 32 ms. After 200 ms of silence, Smart Turn
   scores the whole utterance; "complete" ends the turn, otherwise it waits for
   more speech or a 1.5 s timeout.
3. While the user speaks, `app/stt.py` streams the turn's audio into the local
   recognizer (live `partial` text); the final transcript is ready as the turn
   ends. `Call.respond` hands it to the LangGraph agent, which
   (`app/agent.py`) streams the reply from Groq and checkpoints history.
   With `SPECULATIVE_LLM=1` (default), see below, the LLM starts earlier.
4. `SpeechChunker` cuts streamed text into pieces that grow over the reply
   (about 3 words, then 5, 7, 10, 14, then whole sentences) at natural boundaries, and
   Kokoro synthesizes each while the previous one plays.
5. Audio goes back in 200 ms packets; the browser plays them gap-free.

Events sent to the browser: `ready`, `partial` (live words while the user
speaks), `transcript`, `caption` (each spoken
piece's text and audio length, just before its audio), `metrics`, `notice`,
`reply_end`, `interrupt`, `error`. The browser reveals caption words in time with
playback, so the screen never runs ahead of the voice; on an interruption the
remaining words are dropped, matching the history saved on the server. Postgres is used when running,
otherwise history is in memory (logged at startup).

### Speculative LLM start

STT finalization takes about 0.5 s on one AWS core, after the turn has already
ended. So when Smart Turn ends a turn, `Call` starts one LLM reply from the live
transcript in parallel. Its output is buffered and never spoken. When the final
transcript arrives:

- same words (case, punctuation and spacing ignored): **commit**. The buffer is
  replayed and the reply continues live;
- different words, or the user spoke again first: **cancel**. The Groq stream
  is closed and a normal call answers the final text.

A cancelled speculation leaves no history (it runs on an uncheckpointed graph
and is saved only once committed), and tools marked `side_effects` wait for
the commit. At most one runs per turn. It is skipped after a Groq 429 in the
last minute, or above 70% of the per-minute request or input-token limit.

Measured on the AWS VM (2026-10-15, 20 scripted turns over the tunnel):

- 16 of 20 turns committed. Every cancel was the live text missing the end of
  the last word ("octop" for "octopuses"). The VM benchmark agrees: 87% of 30
  turns matched (`scripts/bench_latency.py`, `live_match_pct`).
- On committed turns without tools, the first audio came about 410 ms sooner
  (median of paired turns).
- Each cancel costs one extra Groq request: 1.45 requests per turn in that run.

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
bodies), 1,000 per day and 8,000 tokens per minute. Speech-to-text is local, so
each user turn makes one or two LLM requests (two when a tool is used), plus one
for a cancelled speculative start.

- One shared Groq client for the process (connection reuse; first token about
  110-170 ms instead of opening a TLS connection per request).
- A 429 asking for a wait of 3 s or less is waited out and retried once.
- Groq occasionally sends no first token. After 3.5 s the request is abandoned
  and retried once. Root cause is not confirmed: not rate limiting (that returns
  an immediate 429) and no event-loop blocking was seen. A watchdog logs any
  event-loop block over 250 ms, and slow first tokens are logged per attempt.

### Speech-to-text choice (benchmarked, 2026-10-13)

Streaming models available in sherpa-onnx were compared on real speech fed in
100 ms packets, exactly as the browser sends it. Word error rate, fillers removed:

| Model | Svarah (Indian English, 266 clips, 19 L1s) | South Asian, 8 kHz phone | South Asian, 10 dB noise |
|---|---|---|---|
| **Nemotron Speech Streaming EN 0.6B (chosen)** | **14.9%** | **19.2%** | **17.1%** |
| Nemotron 3.5 ASR Streaming 0.6B (multilingual) | 18.2% | - | - |
| NeMo FastConformer streaming 80 ms (~100 MB) | 22.4% | 31.6% | 27.5% |

Also tried and dropped: FastConformer 480 ms (worse on accents), Kroko Zipformer
(17-22% on clean synthetic speech), Parakeet unified 0.6B (too slow on this CPU),
and Groq Whisper large-v3-turbo (8% on clean synthetic speech, but a network
round trip per turn, a request quota, and no live partial text).

- Svarah is AI4Bharat's Indian-English benchmark; EdAcc supplied conversational
  Indian and Sri Lankan English. Phone audio = 8 kHz G.711 mu-law round trip.
- Feeding 0.32 s of lead-in and 0.5 s of trailing silence lowered errors by about
  a third (the turn detector's pre-roll provides most of the lead-in).
- Cost: about 550 MB RAM and about 0.4 s of CPU per second of speech on an M2
  (higher under memory pressure). Audio is only fed while the user is speaking.
- Common words and commands are reliable ("cancel", times, Mysore, Bengaluru);
  rare local names (Coorg, Madikeri, Indiranagar) are missed by every model
  tried, Whisper included. sherpa-onnx has no hotword biasing for NeMo
  transducers, so a name-correction step is the planned fix.

### Tool calling

The agent (`app/agent.py`) is a LangGraph loop: `agent -> tools -> agent`,
at most three tool rounds per turn, each step checkpointed. Tools
(`app/tools.py`) are free and keyless:

- `get_weather`: Open-Meteo geocoding (most populous match, cached) and a
  three-day forecast, returned as short phrases to save input tokens.
- `get_time`: any other time zone, as 12-hour time. The user's own date and
  time are in the system prompt on every call, so the model never invents them.

Voice-specific behaviour:

- If a tool is still running after 300 ms and nothing has been said, the
  assistant says "Let me check that." Instant tools get no filler.
- Tool errors and timeouts (5 s) return to the model as results, so it can
  apologise or retry with another place name. Open-Meteo 429/5xx is retried once.
- A short reply that promises a lookup without calling a tool ("Let me look
  that up.") is retried once with `tool_choice="required"`.
- Earlier turns' tool calls and results are not re-sent, and history is capped
  at 12 messages: Groq's free tier allows 7,000 input tokens per minute.
  Input tokens and Groq queue time are logged for every call.

Model choice (`GROQ_LLM_MODEL`): the default is gpt-oss-120b with low reasoning,
9/9 correct in a tool-use check. Qwen 3.8 27B (reasoning off) is about 250 ms
faster to the first token but skipped tools on follow-up questions (6/9) and
made up answers instead.

Next: Temporal workflows.

## Run locally

Create `.env` from `.env.example` and set `GROQ_API_KEY`. Never commit keys.
Download the local models once (~1 GB) with `scripts/download-models.sh`.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Microphone capture needs localhost or HTTPS.
To test over the public internet, see [docs/deploy.md](docs/deploy.md).

```sh
python -m pytest
node --test tests/audio-player.test.mjs
node tests/audio-worklet.test.mjs
```

## Local PostgreSQL

Conversation history uses a native Homebrew Postgres (a Docker VM for one database
cost about 1.5 GB of RAM on an 8 GB Mac). One-time setup:

```sh
brew install postgresql@17
initdb -D /opt/homebrew/var/postgresql@17 --encoding=UTF8 --auth-local=trust --auth-host=scram-sha-256
brew services start postgresql@17          # also starts at login
psql -d postgres -c "CREATE ROLE voice_agent LOGIN PASSWORD '<choose one>'"
psql -d postgres -c "CREATE DATABASE voice_agent OWNER voice_agent"
```

Put the same password in `.env` as `POSTGRES_PASSWORD`. The app connects to
`127.0.0.1:5432` and LangGraph creates its checkpoint tables on startup. Without
Postgres the app still runs and keeps history in memory (logged at startup).
`brew services stop postgresql@17` stops it.
