# Voice Agent Lab

A full-duplex voice agent built as a hand-wired cascade:

```
mic (16 kHz PCM) -> Deepgram Flux (STT + turn detection) -> LLM -> Deepgram Aura-2 (TTS) -> speaker (24 kHz PCM)
```

| Stage | Choice | Runs |
|---|---|---|
| STT and turn detection | Deepgram Flux (`flux-general-en`), streaming WebSocket | Deepgram, India region |
| LLM | gpt-oss-120b, low reasoning (open weights); gpt-oss-20b hedge | Groq free tier |
| TTS | Deepgram Aura-2 (`aura-2-thalia-en`), streaming WebSocket | Deepgram, India region |

English only. Speech moved from local open-weight models (Silero VAD, Smart Turn,
Nemotron streaming STT, Kokoro TTS) to Deepgram on 2026-10-15; that stack and
earlier experiments (Gemini, Cartesia, MiniCPM-o) live only in Git history.

## Status

Working end to end, deployed on an AWS VM in Mumbai behind a Cloudflare tunnel.
Measured through the tunnel on 2026-10-15: 64 turns of macOS `say` speech
(en-IN Aman and Rishi, en-US Samantha, en-GB Daniel), each streamed at real time
by `scripts/live_latency.py`. Times are from the true end of speech:

| | Before (local models, same VM) | After (Deepgram) |
|---|---|---|
| Final transcript at the client | about 1.5 s | p50 0.70 s, p90 1.11 s |
| **First reply audio at the client** | **p50 2.76 s, p90 4.01 s** | **p50 1.33 s, p90 1.96 s** |
| Word error rate, 24-clip `say` spot check | 1.1% | 0.5% (1 of 219 words) |

Where the time goes (server side, by packet arrival, p50 / p90):

| Stage | Time |
|---|---|
| End of speech -> Flux EndOfTurn | 676 / 1,162 ms |
| EagerEndOfTurn ahead of EndOfTurn (committed turns) | 280 / 545 ms |
| LLM first token after EndOfTurn (speculative start included) | 428 / 1,297 ms |
| Aura-2 first audio after the first sentence is sent | 72 / 76 ms |
| End of speech -> first audio sent | 1,354 / 1,954 ms |

The en-IN voices alone: first audio p50 1.35 s, p90 2.04 s. The only word error
was "Mysore" heard as "Su" (Aman). The LLM is now the largest and most variable
stage: on this run Groq's gpt-oss-120b was slow often enough that the hedge
answered 17 of 64 turns.

How a call flows (`app/pipeline.py`, `app/deepgram.py`):

1. The browser streams the microphone over WebRTC (Opus, decoded to 16 kHz on
   the server) or, with `?transport=ws`, 100 ms packets of float32 PCM over `/ws/voice`.
   The server opens one Flux and one Aura socket when the call starts and
   forwards the microphone to Flux continuously, as linear16.
2. Flux sends turn events: `Update` (live words, shown as `partial`),
   `StartOfTurn`, `EagerEndOfTurn`, `TurnResumed` and `EndOfTurn`.
3. At `EndOfTurn`, `Call.respond` hands the transcript to the LangGraph agent
   (`app/agent.py`), which streams the reply from Groq and checkpoints history.
   With `SPECULATIVE_LLM=1` (default) the LLM starts at `EagerEndOfTurn`, below.
4. `ReplyText` cuts the streamed reply into whole sentences. The first sentence
   goes to Aura as soon as it is complete; each later segment is every sentence
   written by then. Aura streams audio back as it generates it, about 4x faster
   than real time.
5. Audio goes to the browser as it arrives. Each segment's caption follows its
   audio, with its exact length; the browser reveals the words from where that
   audio started.

Events sent to the browser: `ready`, `partial` (live words while the user
speaks), `transcript`, `caption` (each spoken segment's text and audio length,
after its audio), `metrics`, `notice`, `reply_end`, `interrupt`, `error`.
The screen never runs ahead of the voice; on an interruption the remaining words
are dropped, matching the history saved on the server. Postgres is used when
running, otherwise history is in memory (logged at startup).

### Speculative LLM start

Flux sends `EagerEndOfTurn` when it is moderately sure the user has finished
(`FLUX_EAGER_EOT_THRESHOLD`, 0.4), before `EndOfTurn` (`FLUX_EOT_THRESHOLD`,
0.7). `Call` starts one LLM reply from the eager transcript. Its output is
buffered and never spoken until:

- `TurnResumed` (the user kept talking): **cancel**. The Groq stream is closed;
  the next `EagerEndOfTurn` may start another. At most one is in flight.
- `EndOfTurn` with the same words (case, punctuation and spacing ignored):
  **commit**. The buffer is replayed and the reply continues live. Flux
  guarantees the words match unless the turn resumed.
- `EndOfTurn` with other words: cancel, and a normal call answers it.

A cancelled speculation leaves no history (it runs on an uncheckpointed graph
and is saved only once committed), and tools marked `side_effects` wait for
the commit. It is skipped after a Groq 429 in the last minute, or above 70% of
the per-minute request or input-token limit.

Measured over the 64 turns: 60 committed, 1 cancelled, 3 skipped (the Groq
rate-limit guard, or no `EagerEndOfTurn` before `EndOfTurn`). Of 67 speculative
requests, 7 were discarded after `TurnResumed`: about 0.11 extra Groq requests
per turn, against 0.45 with the local stack's live-transcript speculation.
Committed turns had their eager start 280 ms (p50) before `EndOfTurn`.

### Barge-in

Talking over the assistant stops it:

1. Flux's `StartOfTurn` while a reply is in progress counts as an interruption.
   Deepgram recommends it over a separate VAD: every `StartOfTurn` already holds
   a recognised word.
2. The server cancels the LLM, sends Aura a `Clear` and drops any audio still
   on its way, then sends `interrupt`. The browser flushes queued audio and
   answers `{"type": "interrupted", "played_s": ...}`.
3. History keeps only what was heard: whole segments that played, plus the
   playing segment cut by word proportion, stored with status `interrupted`.
4. The interrupting speech continues as the user's next turn.

Measured: the client receives `interrupt` 532 ms (p50; p90 622) after it starts
talking over a reply, network both ways included (10 of 10 turns). The local
stack stopped audio about 300 ms in, because it needed only 250 ms of voice,
not a recognised word; a cough or a short "mm-hmm" now interrupts only if Flux
transcribes it. Barge-in is disabled when the browser reports no echo
cancellation, and a turn that starts while the assistant speaks is then
ignored. Headphones are the most reliable setup.

### Groq reliability

Free-tier limits for the LLM: 30 requests per minute (only stated in 429
bodies), 1,000 per day and 8,000 tokens per minute. Each user turn makes one or
two LLM requests (two when a tool is used), plus one for each speculative start
that is cancelled.

- One shared Groq client for the process (connection reuse; first token about
  110-170 ms instead of opening a TLS connection per request).
- Groq occasionally stalls before the first token (seen up to 4 s). Each request
  is hedged instead of retried: if gpt-oss-120b has sent no text or tool-call
  delta after `FIRST_TOKEN_DEADLINE_MS` (600 ms), one request with the same
  prompt and tools goes to `LLM_FALLBACK_MODEL` (gpt-oss-20b, a separate
  rate-limit bucket). Whichever starts first is used; the other stream is closed
  at once. A 429, timeout or 5xx from the primary starts the fallback
  immediately, without waiting. If neither has started after 5 s, the turn fails.
- Why 600 ms: on the AWS VM (30 requests, this prompt) gpt-oss-120b's first token
  was p50 466 ms, p75 479, p90 680, max 2.6 s; gpt-oss-20b p50 404, p90 505,
  max 722. So about one request in eight is hedged.
- Live check through the tunnel (30 turns, 43 requests, 2026-10-15): 4 hedged
  (9%), the fallback won 1. Per-request first token p50 438 ms, p90 587, max
  1.08 s. Per-turn first token p90 fell from 1,206 to 885 ms against the
  previous 40 turns (p50 unchanged at ~500 ms). No multi-second stall happened
  during the check, so the tail cut is a bound, not yet an observed rescue.
- Every request logs the winning model, its first-token time and the running
  hedge rate; the turn metrics carry `llm_model`. A watchdog logs any event-loop
  block over 250 ms.

### Transport: WebRTC (default) and WebSocket

The page calls over WebRTC by default (`static/voice-webrtc.js`, `app/webrtc.py`);
`/?transport=ws` keeps the WebSocket client for one release. Both drive the
same `Call` through `app/transport.py`, with the same events.

- Signalling is one `POST /webrtc/offer` through the tunnel, after
  `GET /webrtc/ice-servers`. Media is Opus over UDP between the browser and
  aiortc in the container; events use a data channel.
- Microphone: decoded, downsampled 48 -> 16 kHz and sent to Flux in 40 ms
  packets. aiortc's own audio jitter buffer (80 ms hold, 320 ms stall per lost
  packet) is replaced by one that passes packets on at once.
- Reply: TTS audio is paced out in 20 ms frames; an interrupt drops the queue
  at once and the page mutes what is still in its jitter buffer. The browser
  reports its playout delay (half RTT + jitter buffer + output) every 500 ms;
  from it the server works out the playing state, the seconds heard after an
  interrupt, and when each caption starts.
- Barge-in follows browser echo cancellation, which is requested on.
- Media relay: Cloudflare Realtime TURN credentials are minted per call when
  `CF_TURN_KEY_ID`/`CF_TURN_KEY_API_TOKEN` are set; until then calls use STUN
  only (see docs/deploy.md).

Measured on 2026-10-15 with `scripts/webrtc_latency.py`: headless Chrome on
this Mac (home broadband, 25 ms RTT to the Mumbai VM, direct UDP path),
synthetic microphone, reply timed at its first audible sample where the page
plays it (Aura's audio starts with near-silence, which neither side counts).
24 turns per transport, interleaved in rounds of 8, same 8 clips:

| From the true end of speech | WebRTC | WebSocket |
|---|---|---|
| First reply audio heard, p50 / p90 | 1,796 / 2,053 ms | 1,822 / 2,432 ms |
| Of which server (Flux end of turn to first audio sent), p50 / p90 | 1,468 / 1,641 ms | 1,436 / 2,082 ms |
| Barge-in: speech start -> `interrupt` on the page, p50 / p90 (9-10 turns) | 595 / 785 ms | 618 / 697 ms |

On this clean network the transports tie: the turn is dominated by Flux and
the LLM, and the p90 gap is server variance, not transport. Against a local
stub pipeline (`scripts/webrtc_latency.py` without `--url`) the transport
alone adds a median 93 ms over WebRTC and 141 ms over the WebSocket (whose
100 ms packets add up to 100 ms), and WebRTC stays near that at 1% and 3%
simulated packet loss (108 and 106 ms). WebRTC should matter more on lossy or
jittery networks, where TCP holds back every later packet; that is not yet
measured on a real one. These numbers are not comparable with the Status table
above, which times the first audio bytes received, silence included, on a
fresh call per turn.

### Speech: Deepgram

Flux (`/v2/listen`) and Aura-2 (`/v1/speak`) each keep one WebSocket per call.
Limits come from Deepgram's documentation, and our settings from them:

- A streaming socket closes after about 10 s without audio, and Flux has no
  KeepAlive message. The browser streams continuously; if it stops, the server
  sends silence after 5 s.
- No maximum session length is documented for either socket. Flux reconnects on
  any close (up to four tries), resending at most the last second of audio
  (Deepgram accepts audio at up to 1.25x real time); the turn in progress is
  lost. Aura reopens on the next sentence. An idle Aura socket stayed open for
  5.5 minutes with WebSocket pings (`websockets` sends one every 20 s).
- Aura accepts at most 20 `Flush` messages per minute. Replies are flushed per
  segment of whole sentences, about two per reply; above the limit the server
  waits for the window instead of being refused.
- `api.in.deepgram.com` (India region) is the default `DEEPGRAM_HOST`. From the
  VM, Aura's first byte took 71 ms there against 290 ms on `api.deepgram.com`,
  and Flux's `EndOfTurn` arrived about 200 ms sooner.
- Flux word timings are coarse (up to 0.5 s off the audio on test clips), so
  the server's end of speech is the last loud packet within 1 s after the last
  word's end.

Cost (pay as you go, 2026-10-15): Flux $0.0065 per streamed minute, Aura-2
$0.030 per 1,000 characters. Measured: replies averaged 155 characters (8.8 s of
speech, 17.5 characters per second). A 10-minute call streams 10 minutes to
Flux ($0.065) and, if the assistant speaks 40-70% of it, 4,200-7,400 characters
to Aura ($0.13-0.22): about **$0.19-0.29 per 10-minute call**. The scripted runs
had the assistant speaking about 70% of the time. Groq stays on the free tier.

### Tool calling

The agent (`app/agent.py`) is a LangGraph loop: `agent -> tools -> agent`,
at most three tool rounds per turn, each step checkpointed. Tools
(`app/tools.py`) are free and keyless:

- `get_weather`: Open-Meteo geocoding (most populous match, cached) and a
  three-day forecast, returned as short phrases to save input tokens.
- `get_time`: any other time zone, as 12-hour time. The user's own date and
  time are in the system prompt on every call, so the model never invents them.
- `forget_me`: deletes what is remembered about the caller (see Caller memory).

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

### Caller memory

A returning caller is recognised and the last call recalled (`app/memory.py`).

- Who: phone calls by the caller's E.164 number (Telnyx `call.initiated` `from`),
  stored as `tel:+919812345678`; browser calls by the Cloudflare Access email,
  stored as `email:someone@example.com`, read from `Cf-Access-Authenticated-User-Email`
  only with `CF_ACCESS_EMAIL_HEADER=1`. Anyone can send that header to an origin
  reachable without Access (a quick tunnel URL, for example), so set it only
  when every route to the app goes through Access. Anonymous, SIP and
  national-format numbers and unauthenticated browsers are unknown callers: no
  memory is read or written for them.
- Call start: the transport passes the key to `open_call(..., caller_id=)`. The
  memory loads beside the Deepgram sockets with a 150 ms limit; a slower load is
  dropped and the call starts without memory, so the first reply never waits.
  Measured against local Postgres: p50 0.2 ms, max 1.1 ms over 50 loads.
- Prompt: for a known caller the system prompt ends with a block of at most
  about 150 tokens (estimated at 4 characters a token): short behaviour rules
  (greet by name, mention the last topic only if natural, never read the notes
  out), the name, the last call's date, its summary (cut to fit) and open items.
- Call end: a background task sends the dialogue (no tool results, last 6,000
  characters) and the earlier notes to the same LLM, hedge included, and stores
  a two-to-four-sentence summary, up to five facts, and the updated name,
  rolling profile and open items. It never delays the call. A failed or
  unusable reply is stored as a failed summary (exception class only) and not
  retried. A call in which the caller said nothing is not recorded.
- `forget_me` tool: deletes the caller's rows, drops the memory from the
  prompt at once, and the current call is not summarized. It has side effects,
  so a speculative reply runs it only once committed. Conversation transcripts
  (LangGraph checkpoints) are untouched.

Tables, created if missing at startup in the same Postgres database (in memory,
lost on restart, without Postgres; `CALLER_MEMORY=0` turns the feature off):

```sql
callers (caller_key text PRIMARY KEY, display_name text, first_seen timestamptz,
         last_seen timestamptz, call_count integer, profile text, open_items jsonb)
call_summaries (call_id text PRIMARY KEY,  -- the conversation (LangGraph thread) id
                caller_key text, started_at timestamptz, ended_at timestamptz,
                summary text,              -- NULL when summarizing failed
                facts jsonb, error text)
```

Not yet verified on a live call: the summarizer's output with the real model
(tests use a fake LLM), and Telnyx caller numbers (the phone transport is on
another branch).

Next: Temporal workflows.

## Run locally

Create `.env` from `.env.example` and set `GROQ_API_KEY` and `DEEPGRAM_API_KEY`.
Never commit keys.

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
