# Voice Agent Lab

A full-duplex voice agent built as a hand-wired cascade:

```
mic (16 kHz PCM) -> Deepgram Flux (STT + turn detection) -> LLM -> Deepgram Aura-2 (TTS) -> speaker (24 kHz PCM)
```

| Stage | Choice | Runs |
|---|---|---|
| STT and turn detection | Deepgram Flux (`flux-general-en`), streaming WebSocket | Deepgram, India region |
| LLM | gpt-oss-120b, low reasoning (open weights); gpt-oss-20b hedge | Groq free tier |
| TTS | Deepgram Aura-2 (`aura-2-odysseus-en`), streaming WebSocket | Deepgram, India region |

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
   than real time. Aura shapes each segment's intonation on its own, so a
   segment never ends inside a phrase: "a.m." in "at ten a.m. with Priya" is not
   a sentence end (it was, and split every calendar answer in two). Before Aura,
   `speakable()` drops web addresses ("kestrelapp.com" -> "kestrelapp"), writes
   "a.m." as "AM" (Aura paused after each letter) and applies the respellings in
   `app/persona.py` ("Brightline" -> "Bright Line"); captions keep the original.
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
  delta by the deadline, one request with the same
  prompt and tools goes to `LLM_FALLBACK_MODEL` (gpt-oss-20b, a separate
  rate-limit bucket). Whichever starts first is used; the other stream is closed
  at once. A 429, timeout or 5xx from the primary starts the fallback
  immediately, without waiting. If neither has started after 5 s, the turn fails.
- The deadline adapts: the p75 of the primary's last 20 winning first tokens plus
  200 ms, between `FIRST_TOKEN_DEADLINE_MS` (900 ms) and 2.5 s. A fixed 600 ms
  (from 2026-10-15: p50 466 ms, p90 680) was below the 120b's first token on
  2026-10-16 (620-930 ms), so every request was hedged, the 20b's quota ran out,
  and both models then failed on the same turn. Near the rate limit a slow
  primary is waited for, not hedged (a failing one still falls back).
- Groq reports no cached tokens at all for this account: identical 2,100-token
  requests, streamed or not, with or without tools, return
  `prompt_tokens_details: null` (checked 2026-10-16). So every turn costs its full
  prompt against the free tier's 8,000 tokens a minute and 200,000 a day per
  model; the 120b's daily budget is about 50-70 owner turns.
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
- Backchannels do not barge in: a turn that starts while the assistant speaks is
  held until Flux's first words (or 350 ms of speech with none, which then
  interrupts). If they are only words like "sure", "okay" or "uh huh" (at most
  three), the reply goes on and the turn is not answered; anything else
  interrupts at once. When the assistant is silent, "okay" is an ordinary turn.
  Measured with the fake Telnyx harness and real Deepgram, two runs each:
  speech start to `clear` 363 and 438 ms with the hold, 477 and 549 ms without
  (no slower: Flux's StartOfTurn usually carries the first word already); a
  spoken "Sure." over a reply was ignored.
- Ending a call: a clear goodbye ("bye", "you can end the call", "that's all,
  thanks") is answered with a fixed goodbye and hangs up without the LLM
  (`app/closing.py`), so it works while Groq is rate-limited. A likely one ("and
  that's all") goes to the LLM, which has an `end_call` tool; if the LLM fails, it
  is taken as a goodbye too. Phone calls ask "Still there?" after 20 s of silence
  and say goodbye and hang up at 45 s (`IDLE_PROMPT_S`, `IDLE_END_S`).
- Calls we place (callbacks, briefings) wait for the callee's pickup "Hello?" (up
  to 2.5 s; 1 s of silence if they say nothing) before greeting. A "Hello?" over
  the greeting does not cut it, and a lone "Hello?" before anything was answered
  repeats the greeting instead of going to the LLM.
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
- `schedule_callback`, `reschedule_callback`, `cancel_callback`, `get_callback`:
  phone callbacks (see Callbacks).

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

- Who: phone calls by the caller's E.164 number (Telnyx `call.initiated` `from`,
  carried through the one-time stream token), stored as `tel:+919812345678`;
  browser calls by the email in the Cloudflare Access JWT (`Cf-Access-Jwt-Assertion`),
  stored as `email:someone@example.com`. The JWT is verified (RS256 only) against
  the team's certs, cached for an hour, for the Access app's AUD tag
  (`CF_ACCESS_AUD`), the team issuer and its expiry (`app/access.py`); the
  email header alone is ignored. Requests through the quick tunnel carry no
  JWT and are anonymous, as are anonymous, SIP and national-format numbers:
  no memory is read or written for them.
- Caller ID can be spoofed: anyone who fakes a known caller's number hears
  that caller's name and last topic. Accepted for this lab (no PIN); a real
  deployment would need a second factor before reading memory to a caller.
- Call start: the transport passes the key to `open_call(..., caller_id=)`. The
  memory loads beside the Deepgram sockets with a 150 ms limit; a slower load is
  dropped and the call starts without memory, so the first reply never waits.
  Measured against local Postgres: p50 0.2 ms, max 1.1 ms over 50 loads.
- Phone greeting: once the media stream starts, the assistant speaks first
  through Aura, fixed text with no LLM call: "Hi Asha, this is Cadence. How can
  I help?" for a known caller with a name, else "Hi, this is Cadence. How can I
  help?". Barge-in interrupts it like any reply; it is stored as an assistant
  message (only the words heard, if interrupted).
- Prompt: for a known caller the system prompt ends with short fixed rules
  (greet by name, mention the last topic only if natural, never read the notes
  out), then at most about 150 tokens (estimated at 4 characters a token; the
  rules are not counted): the name, the last call's date, open items, the last
  summary and the rolling profile. The profile is cut first, then the summary.
- Call end: a background task sends the dialogue (no tool results, last 6,000
  characters) and the earlier notes to the same LLM, hedge included, and stores
  a two-to-four-sentence summary, up to five facts, and the updated name,
  rolling profile and open items. It never competes with live calls: while any
  call is in progress or Groq is near its free-tier limit it waits, checking
  after 5 s and backing off to every 60 s, for up to 10 minutes
  (`CALLER_MEMORY_SUMMARY_WAIT_S`); a 429 on the summary waits again. After
  that it runs anyway if only a call is live, and is stored as failed
  (`RateLimitWait`) if Groq is still near its limit. Its output cap is 1,200
  tokens, separate from a spoken reply's 400. A failed or unusable reply is
  stored as a failed summary (exception class only). A call in which the caller
  said nothing is no visit: nothing is recorded and its conversation (perhaps
  only the greeting) is deleted.
- `forget_me` tool: in one transaction, deletes the caller's rows and the
  LangGraph checkpoint threads of every call in `call_summaries` for them;
  drops the memory from the prompt at once; the current call is not summarized
  and its thread is deleted when it ends. It has side effects, so a speculative
  reply runs it only once committed.

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
(tests use a fake LLM).

### Callbacks (Temporal)

A phone caller can say "call me back tomorrow at 5 pm about the weekend
weather", and the assistant calls them back then. The waiting is done by
[Temporal](https://temporal.io), self-hosted on the VM: a server that keeps
timers and state in Postgres, so a callback survives restarts of the app, the
worker or the whole VM.

Temporal's words: a **workflow** is a function whose progress
Temporal records, so it can sleep for days and resume where it was; an
**activity** is the workflow's one step that touches the outside world (here,
asking Telnyx to dial), retried on failure; a **worker** is our process that
runs both; a **signal** pokes a running workflow with news; a **query** reads
its state; an **update** is a signal that answers yes or no.

How one callback flows:

1. During a phone call the caller asks. The model says the day and time back,
   gets a yes, and books it. The yes is enforced in code: `schedule_callback`
   without `confirmed` only proposes (nothing is booked), and `confirmed=true`
   books that proposed time only if the caller's very next turn is a clear yes
   (no "no" in it); `reschedule_callback` works the same way. The tool reads the
   time ("tomorrow at 5 pm", "in 2 hours", "Friday 10:30 am", or ISO) in the
   caller's time zone (`ASSISTANT_TIMEZONE`, or a `time_zone` the caller
   names), refuses the past, more than two weeks ahead and, when
   `CALLBACK_QUIET_HOURS` is set (e.g. `21-9`: 9 pm to 9 am there; off by default
   for testing), quiet hours, and starts `CallbackWorkflow` with id
   `callback-<hash of the caller>`. The caller's number comes from the call,
   never from the model; web callers are politely declined.
2. The workflow sleeps on a durable timer until the time. Nothing runs meanwhile.
3. When it wakes it takes one of the day's 5 dial slots (`DialBudgetWorkflow`,
   one per UTC day, for all callers together), then runs the `place_call`
   activity: Telnyx Dial from +1 228 888 8007 through the voice-agent-lab
   Call Control app, with `client_state` naming the workflow, attempt and reason.
4. Telnyx's webhooks come to the app as usual. `call.answered`: the app signals
   "answered" and starts the media stream (`streaming_start`, same codec and
   one-time token as an inbound answer); the call then runs like any inbound
   one, opening with "Hi Asha, you asked me to call back about the weekend
   weather." (the name from caller memory, if known). `call.hangup` before an
   answer: `timeout`/`no_answer` is no answer, `user_busy`/`call_rejected` busy.
5. Not answered (or no webhook within 3 minutes): it waits 15 minutes and tries
   again, at most 3 attempts in all, then ends as failed. Each attempt and the
   outcome are kept in the workflow's result.

While it waits, `reschedule_callback` moves the timer (an update, so a
quiet-hours time is refused on the spot), `cancel_callback` ends it (a signal;
a call already ringing is left to ring out) and `get_callback` reads it (a query,
or the result of the last finished one).

Decisions:

- One workflow id per caller, so Temporal itself guarantees at most one pending
  callback per caller with no table or lock of ours; a finished callback frees
  the id. The id is a hash, so phone numbers are not the workflow list's names
  (they are in the workflow's input, visible in the UI).
- Separate worker container (same image, `python -m app.callback_worker`) rather
  than a task inside the app: the realtime audio path stays free of it, app
  redeploys do not stop it (and the reverse), and it can restart on its own.
  It used about 57 MB of RAM.
- The dial cap lives in Temporal too (an update on the day's budget workflow,
  processed one at a time), so two callbacks due at once cannot both take the
  last slot. Activity retries are counted once.
- Answering-machine detection is off: it is a paid add-on ($0.002 a call). A
  voicemail that picks up therefore counts as answered.
- `get_callback` has no side effects, so a speculative reply may run it; the
  other three wait for the reply to be committed.

Settings: `TEMPORAL_ADDRESS` (unset: callbacks are off and the tools say so),
`TEMPORAL_NAMESPACE` (default `default`; the VM uses `voice-agent-lab`),
`CALLBACK_QUIET_HOURS` (empty or `off`, the default: any hour; `21-9`: no calls
from 9 pm to 9 am the caller's time; the setting at booking time travels in the
workflow's input and also governs its reschedules and retries), and
for the worker `TELNYX_API_KEY` plus optional `TELNYX_CONNECTION_ID` and
`TELNYX_FROM_NUMBER`. Callbacks need caller memory on (it supplies the caller's
key). The VM setup is in [docs/deploy.md](docs/deploy.md#callbacks-temporal).

Locally:

```sh
brew install temporal
temporal server start-dev --namespace voice-agent-lab   # UI: http://localhost:8233
TEMPORAL_ADDRESS=localhost:7233 TEMPORAL_NAMESPACE=voice-agent-lab python -m app.callback_worker
```

Set the same two variables for the app. Without `TELNYX_API_KEY` a due
callback fails without dialling; `TELNYX_API_URL` can point the worker at a
local stand-in. The Temporal UI lists workflows (filter by type
`CallbackWorkflow`); open one to see its timer, attempts, signals and result.
From the CLI: `temporal workflow list`, `temporal workflow describe -w <id>`,
`temporal workflow query -w <id> --type status_query`, and
`temporal workflow signal -w <id> --name cancel`.

Tests run in Temporal's time-skipping test server (hours pass in milliseconds;
the SDK downloads it on first use): the timer firing the call, no-answer
retries, the unreported call, reschedule, cancel (waiting and ringing), quiet
hours, one per caller, refused dials and the daily cap. Tool tests use a fake
Temporal client. Checked by hand against a real server with a local stand-in for
Telnyx: a callback scheduled a minute ahead dialled on time, including after
stopping and restarting both the server and the worker while it waited.

Not yet verified: a real outbound call (the voice-agent-lab app needs an
Outbound Voice Profile first), and Telnyx's actual `hangup_cause` for an
unanswered call to the verified number.

### Research callbacks (VALR)

VALR (Voice Agent Lab Research) lets a phone caller say "Research Coorg for a
weekend trip and call me back". Cadence answers "Let me look into that and call
you back in about 3 minutes. Is that okay?", and on the caller's yes it books the
job. When the caller hangs up, three researchers look into it at the same time,
a fourth model merges what they found, and Cadence calls back to say it in about
a minute of speech, then answers follow-up questions from the notes.

```
call ── start_research(topic) ── yes ──> ResearchCallbackWorkflow (Temporal, queue voice-research)
                                           waits for the call to end
                                           ├─ web researcher       Claude web search + page reading ┐ in parallel,
                                           ├─ reviews and news     Claude web search                ├ Claude Haiku 5.5,
                                           ├─ facts                Wikivoyage, Wikipedia,           ┘ CrewAI agents
                                           │                       OpenStreetMap, Open-Meteo (free)
                                           ├─ synthesizer          Claude Sonnet 5.5: spoken answer + cited notes,
                                           │                       then a Haiku check of each spoken sentence
                                           └─ CallbackWorkflow (the callbacks above): dials at once
callback call: "Hi, it's Cadence, calling back with what I found about Coorg." -> the answer -> follow-ups
```

How a job flows:

1. **Booking (on the call).** `start_research` first only proposes: it checks
   that the caller is a phone caller, that the topic is not about a private
   person, that no callback or research is pending for them, that a dial slot is
   left today, and the cost limits below. It returns the promise N for the
   agent to say. Only a clear yes on the caller's very next turn books it (the
   same code-enforced yes as callbacks): a `research_jobs` row is written under
   the limits in one transaction, and the workflow starts and waits.
2. **The promise N** is computed in code, not guessed by the model: the 90th
   percentile of the last 20 finished jobs' research time, plus 60 s to dial
   and ring, plus 30 s margin, rounded up to whole minutes and kept between 2
   and 5. Until 5 jobs have finished it uses the spike's p90 (57 s), so N is 3.
3. **Research starts when the call ends** (the app signals the workflow; if
   that signal is lost, it starts on its own after 11 minutes). The whole job
   must finish within N minutes minus the 60 s for dialling: 120 s for N = 3.
   The researchers get 80 s; synthesis gets the rest.
4. **Partial results are fine.** Two of three researchers are enough; a
   researcher that ran out of time or budget hands over what its tools
   returned. If anything is missing the answer starts "Here's what I found so
   far". If nothing usable came back, Cadence still calls and briefly apologizes.
5. **The callback** is the existing `CallbackWorkflow`, started as a child as
   soon as the answer is saved: the same daily dial limit (research shares the
   5 dials a day), retries and one pending callback per caller. On that call the
   spoken answer and notes go into the system prompt as quoted data; the agent
   relays the answer naturally and answers follow-ups only from the notes.

What the answer may say: 90 to 150 words written for speech (no lists, URLs or
symbols; numbers as words); every note has a source URL that one of the
researchers' tools actually returned (checked in code); every rating names
what is rated; safety only if sourced, recent and practical, and no crime
stories or allegations (the notes may keep them, marked sensitive). A Haiku
check then drops spoken sentences the cited text does not support (in the spike
it would have caught "each tunnel on the route charges 100 kroner", true only of
tunnels between islands).

**Cost controls**, all enforced in code (environment overrides in brackets):

| Limit | Default |
|---|---|
| Per job, all requests (`VALR_JOB_USD`) | $0.25: researchers stop softly at 70% of their slice, hard at 100%; synthesis uses what is left |
| Jobs per caller per UTC day (`VALR_JOBS_PER_CALLER_DAY`) | 3 |
| Jobs per UTC day, everyone (`VALR_JOBS_PER_DAY`) | 15 (and the 5 callback dials a day) |
| Per month (`VALR_MONTH_USD`) | $20: new jobs are declined politely once the month's spend plus one job would pass it |
| Web searches per researcher (`VALR_SEARCHES_PER_RESEARCHER`) | 3, each one Claude `web_search` with `max_uses` 1 |
| Pages read per researcher (`VALR_PAGES_PER_RESEARCHER`) | 3, each cut to 4,000 characters (`VALR_PAGE_CHARS`) before the model sees it |
| Results per search or lookup (`VALR_RESULTS_PER_CALL`) | 5 |
| Free-API lookups (facts researcher) | 6 |
| Findings per researcher | 8 claims, 120-word summary (truncated in code) |
| Notes | 12 (about 600 tokens) |
| max_tokens per call | researcher 1,500, search 1,200, synthesis 3,000 |
| `max_iter` per agent | 5 |

Spend is recorded per request from the usage the API returns (tokens, cache
reads and writes, web searches at $10 per 1,000) in `research_spend`, so the
month's total is real, not estimated per job. `docker exec voice-research
python -m app.research_jobs` prints today's and this month's spend and the
latest jobs; the dashboard at valr.haciensus.com (`/valr` on the app, behind
Cloudflare Access) shows the same plus each job's answer, notes and sources.
Cap hits are logged as `VALR cap hit <name>` and stored per job.

Measured (phase 1 spike, 3 topics, 9 jobs on these models): 41 to 59 s per job
(p90 about 57 s), 24 to 29 model calls, about 138,000 input and 17,000 output
tokens, 4 to 6 web searches, **$0.08 to $0.10 per job**: about half of that is
the searches. Sonnet researchers cost 3.5 times as much with no clear gain.
Whole-call costs for a research request: the first call as usual, plus a
callback call of a minute or two (Deepgram Flux and Aura-2 about $0.05, see
Speech; Telnyx outbound minutes at its voice rates; Groq on the free tier).

Why these pieces:

- **Temporal runs the fan-out, CrewAI runs each researcher.** Each researcher
  is its own activity: a crash reruns only that one, partial results are
  natural, and the Temporal UI shows each one's time and errors. CrewAI gives
  each researcher its role, tools, `max_iter`, `output_pydantic` schema and a
  guardrail that rejects claims without a source. (A single crew with three
  async tasks took the same time and money in the spike, but leaves no place for
  the code checks between research and synthesis.)
- **Claude's own web search**, wrapped as a CrewAI tool: each use is a Messages
  call with `web_search` and `max_uses` 1 that returns cited facts. CrewAI's
  Anthropic provider cannot pass server tools, and the wrapper lets code count
  and cap every search. No search API signups.
- **A separate container and image.** crewai requires Python below 3.14 and
  pins pydantic below 2.13 and anthropic 0.73; it also brings chromadb and
  lancedb (the worker image is 1.1 GB, the app's 525 MB). So the `worker` target
  of the same Dockerfile is Python 3.13 with its own lock
  (`requirements-worker.lock`), and runs as `voice-research`. The app image is
  unchanged and has no Anthropic client: the live call path never calls Claude,
  and only `voice-research` gets `ANTHROPIC_API_KEY`. Downgrading pydantic to
  2.12.5 in the app was tested and is safe (the full suite passes), but with a
  separate image it is not needed.
- **Tracing stays local**: Temporal's history and the worker's logs. CrewAI's
  telemetry and tracing are off (`CREWAI_DISABLE_TELEMETRY`, `OTEL_SDK_DISABLED`).

Local run: `temporal server start-dev`, then with the worker's venv (Python
3.13, `pip install -r requirements-worker.lock`) and `ANTHROPIC_API_KEY`,
`POSTGRES_PASSWORD`, `TEMPORAL_ADDRESS` set: `python -m app.research_worker`, and
`python -m app.research_worker test "Coorg for a weekend"` runs one job end to
end for a fictitious caller without dialling. Tests: `python -m pytest`
(the agent tests need the worker venv: `.venv-worker/bin/python -m pytest
tests/test_research_agents.py`; the Postgres tests need `VALR_TEST_POSTGRES`).
The spike that chose all this is `scripts/research_spike.py`.

Not yet verified: a real research callback call end to end on the phone.

### VALR interview assistant (owner only)

For its owner, VALR (displayed "VALR", spoken "Valor": `app/persona.py`) is a personal
interview assistant: it knows his profile, says what is on his calendar, researches each
upcoming interview and calls him once, 30 minutes before it, with a briefing, answers follow-ups
from the notes and quizzes him on likely questions. Every other caller gets the generic
assistant with none of his data.

```
Google Calendar push (/hooks/gcal) ─┐                 6-hourly sweep (Temporal Schedule)
   or the ICS URL if OAuth lapsed   ├─> CalendarSyncWorkflow ── diff each event vs its snapshot
"research tomorrow's interviews" ───┘        │
                                             ├─> InterviewSlotWorkflow per event (long-lived)
                                             │     T-60 of its block: R1 private + R2 round
                                             │     + R2 interviewers + R3 company (7-day cache),
                                             │     then a 60-90 s spoken brief and cited notes
                                             └─> BlockCallWorkflow per block (gaps < 30 min)
                                                   one call at T-30, no retries
```

**Owner gating.** Interview mode applies only to the owner's caller keys: `tel:`
VALR_OWNER_PHONE and `email:` each VALR_OWNER_EMAILS (or VALR_OWNER_KEYS). Only then
does the system prompt carry his profile and self-evaluation (`app/profile/briefing.py`,
including his pay targets) and are `get_calendar` and `research_interviews` offered; the
tools also refuse anyone else in code. Pay figures and email details are spoken only on
those calls. Caller ID can be spoofed (see Caller memory): accepted for this lab.

**Calendar.** Preferred source: Google Calendar (calendar.readonly) through the same
one-time OAuth consent as Gmail (`/oauth/google/start` on valr.haciensus.com; the refresh
token is Fernet-encrypted in Postgres). A push channel (`events.watch`) posts to
`/hooks/gcal` (an Access bypass; the request must carry our channel id, its secret token
and resource id); each notification starts one incremental sync (`syncToken`; a 410 means
a full resync). The 6-hourly sweep renews the channel a day before it expires and is the
safety net. Without OAuth (or when the Testing-mode token lapsed after 7 days: the
dashboard says "reconnect needed") the ICS URL is used, with the same diffs. By hand:
`docker exec voice-research python -m app.research_worker sync` (or `blocks`: a dry run
that parses the calendar and starts nothing; both print titles and times only).

**What a change does** (per event, from its stored snapshot; `app/interview_types.py`):

| Change | Work |
|---|---|
| new | full research (R3 reused if the company was researched in the last 7 days) |
| interviewer added | only the new interviewer(s); removed ones' notes dropped, brief rewritten |
| time only | the slot's timer and its block call move; no research |
| title or description (round) | R2's round part only |
| company | R1, R2 round and R3 |
| cancelled / declined | the slot ends and its block call is cancelled; the company cache stays |
| nothing material | nothing (and inputs unchanged on a re-run: a logged cache hit) |

Interviewer research never covers the owner, rooms, scheduling and ATS tools (Greenhouse,
Lever, Ashby, GoodTime, Gem, Calendly, Workday...), `no-reply`/`recruiting`/`talent`/`hr@`
style addresses, or people whose display name says recruiter, talent, HR, people ops,
coordinator, sourcer or TA. The rest go to a Haiku classifier with their name, domain and
any title or signature lines from R1's emails; only "interviewer" is researched. Being the
organizer does not make someone a recruiter. Skips are logged as counts, never names.

**Researchers** (CrewAI on Claude Haiku, `app/interview_agents.py`, same caps, budget,
sourcing rules and claim check as topic research):

- R1 private context: Gmail read-only (subject, sender name and domain, relevant lines,
  quoted replies dropped; at most 5 threads, 700 characters each), Fathom recordings
  (FATHOM_API_KEY), the derived history of earlier recorded rounds (the profile import's
  one-time export, data/evaluation.json) and pushed context. Skipped if none is available.
- R2 interview intel: experiences for this company and role (Glassdoor, LeetCode
  Discuss, Reddit, Blind, GeeksforGeeks, engineering blogs) and, separately, the
  interviewers' public professional info (LinkedIn snippets in search results only).
- R3 company: employee and product reviews, news, funding, layoffs, culture, and the pay
  range for the role and location. The owner's targets never go to Claude: the call
  compares them live from his profile.

Limits for interview mode: 30 jobs a day and $40 a month (VALR_INTERVIEW_JOBS_PER_DAY,
VALR_INTERVIEW_MONTH_USD; $0.40 per job, VALR_INTERVIEW_JOB_USD); briefing calls have their
own 12 dials a day (VALR_BRIEFING_DIALS_PER_DAY), so callbacks cannot starve them.
VALR_BRIEFING_DIAL=0 keeps everything but never dials; VALR_BRIEFINGS=0 turns the sweep off.

**The briefing call** goes out once, 30 minutes before the block's first interview, and
rings for 60 seconds (Telnyx `timeout_secs`); unanswered is final (the dashboard shows it).
An interview added later is called as soon as its research is ready, if at least five
minutes are left. Zoom Phone (the owner's number) must not forward to voicemail before 60
seconds: a voicemail that picks up counts as answered. If a brief is already ready when he
calls in and asks ("brief me", "my next interview"), it is given on that call
(`get_briefing`); research and a callback are offered only for interviews without one.
"Call me now" on the dashboard, or `python -m app.research_worker call-now [next|<slot>]`,
calls him at once about his next interview (researching it first if needed).

**The VALR page** (valr.haciensus.com, `/valr`, behind Cloudflare Access; `app/valr_view.py`
gathers, `app/valr_page.py` renders, `static/valr.js` adds the live parts). Written for a
user who did not build VALR: plain words, every time as "11:15 AM IST" (ASSISTANT_TIMEZONE)
with a relative "in 2 h 25 min". Top to bottom: what VALR does, the next briefing call and
the three actions (call VALR now, pause or resume calls, sync the calendar); set-up steps
until Google, Fathom, the phone number and a test call are done; the week's interviews,
each opening a detail view (the briefing, who you're meeting, what this round is like,
likely questions, the pay band against your target, a prep checklist, what your emails
and past rounds say, the company); performance (scorecards out of 10, a trend with
3-interview moving averages and a dimension picker, per-question feedback, most improved,
biggest risk, focus this week); and, folded under System, spend, call budgets,
connections and recent research. Attendee emails and phone numbers are never shown.
`GET /valr/api/state` returns the same view as JSON. Owner actions (POST, same-origin
JSON with the page's CSRF token, owner's Access login): sync, refresh one interview's
research (caches bypassed), and pause or resume automatic calls, a `valr_flags` row the
briefing-call workflows read before every scheduled dial (a skipped attempt is just
skipped (one call at T-30, no retries); calls you ask for still ring;
VALR_BRIEFING_DIAL=0 stays the hard off). Performance reads `interview_scorecards.json`
(VALR_SCORECARDS_PATH) when the profile importer writes it, else adapts
`interview_scores.json` and `evaluation.json` (1-5 scores shown out of 10).
`VALR_DEV_PREVIEW=1 python scripts/preview_ui.py` serves both pages with made-up data at
http://127.0.0.1:8090 (`/valr?scenario=empty`, `error`, `down` for the other states).

**Context push (optional).** A Mac-side job (not built) may POST per-interview email
context to `/valr/context` (JSON {event_key?, company?, subject?, summary?, excerpt?, notes?},
8 KB) with an Access service token plus `Authorization: Bearer VALR_CONTEXT_TOKEN`;
it is stored encrypted and R1 reads it for that event or company.

**Trade-offs.** A briefing call's prompt carries his profile (shortened) and the block's
briefs (about 2,500 tokens in all); Groq's free tier allows 8,000 input tokens a minute,
so long quiz sessions lean on the gpt-oss-20b hedge. ICS keys are mapped to Google's ids,
but switching sources can make a recurring event look new once (a cache hit if unchanged).

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
