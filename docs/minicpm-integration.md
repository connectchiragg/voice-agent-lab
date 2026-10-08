# MiniCPM integration: authenticated proxy + backend adapter

Status: implemented and tested locally against mocked inference. **Not deployed**
to Lightning; secure remote connectivity and real inference remain unverified.
Do not wake the Studio or enable a GPU without Chirag's explicit approval.

Prepared Studio: `minicpm-full-duplex-test`. The supplied preparation report says
weights (~19 GiB), dependencies and 47 CPU checks are complete, with 5.00 credits
remaining. This increment did not verify the remote balance, files or config.

## What this increment changes

- `app/minicpm_proxy.py`: a separate, authenticated audio-only WebSocket proxy.
- `app/minicpm.py`: a typed client for the actual pinned Realtime API.
- No changes to `app/main.py`, the browser, conversation persistence or current
  Gemini/Groq/Cartesia routing. No new dependencies and no model loading.

The intended connection is:

`local backend -> verified WSS + bearer token -> proxy -> loopback official gateway`

The proxy is not a fork of the official gateway. It exposes only
`/v1/realtime?mode=audio`; admin, file access, worker registration and other modes
are not forwarded. Missing/wrong tokens and browser Origin headers are rejected
before opening an upstream connection. The token is backend-only and is sent in
an Authorization header, never in a URL. One active session is allowed per proxy
process; use **one process**, not multiple workers, for this single-GPU test.

## Source contract

Official source commit: `47709a9210dfd71afa76c058e017fc8c4db5c8d2`.

- [Gateway handler](https://github.com/OpenBMB/MiniCPM-o-Demo/blob/47709a9210dfd71afa76c058e017fc8c4db5c8d2/gateway.py)
- [Worker handler](https://github.com/OpenBMB/MiniCPM-o-Demo/blob/47709a9210dfd71afa76c058e017fc8c4db5c8d2/worker.py)
- [Audio protocol](https://github.com/OpenBMB/MiniCPM-o-Demo/blob/47709a9210dfd71afa76c058e017fc8c4db5c8d2/docs-app/content/docs/en/realtime-api/audio.md)

1. Connect, wait through optional queue updates until `session.queue_done`.
2. Send `session.init` with a `payload` containing the system prompt.
3. Wait for `session.created` before sending microphone input.
4. Send `input.append` with base64 **16 kHz mono float32 PCM**, not WAV/WebM.
   The official demo normally sends one-second chunks, including silence.
5. Receive `response.output.delta`: `text`, `audio` or `listen`. Output audio is
   **24 kHz mono float32 PCM**. Text and audio do not map one-to-one.
6. Close with `session.close`; the server may respond with `session.closed`.

There is no per-turn `response.done` in audio mode. Session IDs are server-owned;
do not use a conversation UUID in the URL. Each open adapter context creates a
fresh model session; resuming PostgreSQL history is a separate future decision.

The client sends and receives concurrently, with no unbounded application queues.
Both sides bound incoming WebSocket messages (1 MiB) and transport queues; input
is limited to one second of PCM per message. PCM alignment and finite samples are
checked, but bytes alone cannot prove the caller used the correct sample rate.
Queue/initialization timeout is 60 seconds; the proxy caps sessions at 600 seconds.
No automatic retries: silently retrying would lose model-session state.

## Deployment checklist — do not execute while the Studio is asleep

1. Inspect `/teamspace/studios/this_studio/PREPARATION.txt` and the actual
   `/teamspace/studios/this_studio/minicpm-demo/config.json` before deployment.
   Confirm the checkout matches the pinned commit and recording is disabled
   (`recording.enabled` where supported). This proxy does not prevent the
   official gateway from recording if its own config enables it.
2. Bind the official gateway, internal registry and worker/backend services to
   numeric loopback. The gateway's `--host` defaults to `0.0.0.0`; override it.
   Do not expose any of those original ports through Lightning.
3. Deploy our proxy separately with the existing FastAPI/websockets dependencies.
   Set the same random `MINICPM_TOKEN` (at least 32 non-whitespace characters) on
   the proxy and local backend. Keep it in private environment configuration.
   Set `MINICPM_UPSTREAM_URL` to the real loopback gateway audio endpoint.
4. For the proxy only, the launch shape is:

   ```sh
   python -m uvicorn app.minicpm_proxy:app --host 127.0.0.1 --port 8008 --workers 1 --ws-max-size 1048576 --no-access-log
   ```

   This command does not start or load the model. Environment must already be set.
5. Establish a TLS ingress/tunnel to **only the proxy**. Verify Lightning's actual
   forwarding/auth behavior before choosing it; no public endpoint is configured
   here. TLS must use a certificate trusted by the local backend; do not disable
   certificate verification or expose a raw WS endpoint over the internet.
6. First point the private upstream at a CPU mock, not the GPU worker. Verify
   missing/wrong tokens fail before gateway contact, authorized streaming works,
   admin/registry/worker routes cannot be reached publicly, and disconnects release
   sessions. Test this from outside the Studio, not just inside loopback.
7. Only then set backend `MINICPM_URL` to the verified WSS audio URL. These new
   variables are unused by the current browser app until the next increment.

CPU mock checks establish our local contract, not Lightning's network isolation,
model latency, inference correctness, voice quality or real barge-in behavior.
The GPU smoke test is a separate approval with a time and spending budget.

## Next increment, after connectivity is verified

Wire continuous browser microphone PCM to the adapter, retain scheduled playback,
and handle independent text/audio/listen events. Do not delete Cartesia until a
human listening check passes. Persistence after delivered audio and text/audio
alignment need a separate design; existing checkpoints still store generated text.
