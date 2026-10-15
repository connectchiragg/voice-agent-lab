# Deploying for a public test

Speech runs on Deepgram and the LLM on Groq, so the server itself is small: the
image is about 450 MB and used about 100 MB of RAM during a call on the VM. Microphone capture needs HTTPS, so the
page must be served over https:// and the socket over wss://. Secrets are passed
only at run time.

Checked on 2026-10-14:

| Option | RAM / CPU | Fits? | Cost |
|---|---|---|---|
| **Oracle Cloud Always Free, Ampere A1** (primary) | 12 GB / 2 OCPU arm64 | Yes | Always free; a card is needed to verify the account |
| **AWS Free plan, c7i-flex.large** (fallback) | 4 GB / 2 vCPU | Yes | Free-plan credits only: 6 months, then the account is suspended |
| Hugging Face Spaces, Docker on CPU Basic | 16 GB / 2 vCPU | Yes | Creating a Docker Space now needs PRO ($9/mo) |
| Cloudflare Containers | n/a | n/a | Workers Paid plan only |
| Render, Koyeb, Railway free tiers | 0.5–1 GB | Probably; not tried since speech moved to Deepgram | Free |
| Quick tunnel to a laptop | the laptop's own | Yes | Free; public only while the laptop runs it |

## Primary: Oracle Cloud Always Free (arm64)

Steps for the owner:

1. Sign up at https://www.oracle.com/cloud/free/. The card is only for
   verification. Do **not** upgrade to Pay As You Go. Pick a home region: Always
   Free A1 capacity exists only there.
2. Create an instance with:
   - Compute > Instances > Create;
   - image **Ubuntu 24.04** (aarch64);
   - shape **VM.Standard.A1.Flex** with 2 OCPU and 12 GB;
   - your SSH public key.

   Check that the "Always Free-eligible" label is shown. If you get "Out of host
   capacity", retry later or pick another availability domain.
3. You do not need any ingress rule: the Cloudflare tunnel makes outbound
   connections only. Keep port 22 open for SSH.
4. From this repo on your Mac, copy the source. The repo is private, so this
   avoids a deploy key:

   ```sh
   git archive deploy-free-cloud | ssh ubuntu@<vm-ip> 'mkdir -p voice-agent-lab && tar -x -C voice-agent-lab'
   ```

5. On the VM, create `~/voice-agent-lab/.env` with `chmod 600`. Use plain
   `KEY=value` lines, without quotes, because `docker --env-file` keeps quotes
   literally:

   ```
   GROQ_API_KEY=<your key>
   DEEPGRAM_API_KEY=<your key>
   GROQ_LLM_MODEL=<same as your local .env>
   GROQ_LLM_REASONING=low
   ```

   To copy a key from your Mac without printing it:
   `grep '^DEEPGRAM_API_KEY=' .env | ssh ubuntu@<vm-ip> 'cat >> ~/voice-agent-lab/.env'`.

   Leave `POSTGRES_PASSWORD` unset, so history stays in memory.
6. Run `sh ~/voice-agent-lab/deploy/vm-setup.sh`. It does the following:
   - installs Docker;
   - builds the image (under a minute);
   - starts the app on 127.0.0.1:7860;
   - starts a Cloudflare quick tunnel and prints `Ready: https://<random>.trycloudflare.com`.

The quick tunnel URL changes whenever the `cloudflared` container restarts. Read
it again with `sudo docker logs cloudflared 2>&1 | grep trycloudflare`. For a
stable hostname, create a named Cloudflare Tunnel on a domain in your
Cloudflare account and point it at `http://127.0.0.1:7860`.

Oracle may reclaim Always Free instances that stay idle for 7 days, meaning CPU,
network and memory all under 20%.

## Fallback: AWS Free plan (x86_64)

Only for an account on the **Free plan**, created on or after 2025-07-15. An
older account is billed for anything beyond 750 h/month of t2/t3.micro (1 GB),
which was too small for the local models and may now suffice (untested).

1. Run `aws login`, then check the account's plan in the Billing console.
2. Launch EC2 with:
   - Ubuntu 24.04 x86_64;
   - **c7i-flex.large**, 2 vCPU and 4 GB (it is on the Free plan's eligible list);
   - a 30 GB gp3 disk;
   - SSH allowed only from your IP.
3. Follow steps 4–6 above, using `ubuntu@<ec2-ip>`.
4. Stop the instance when you are not testing. Free-plan credits ($100, plus
   up to $100 more for onboarding tasks) are spent per hour on the instance,
   its disk and its public IPv4 address.

## Testing a deployment

1. Open the https URL in a browser, then click Start call and speak. Use
   headphones so barge-in works.
2. Run a scripted check from this Mac:

   ```sh
   say -o /tmp/q.aiff "Tell me a fun fact about octopuses." && afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/q.aiff /tmp/q.wav
   .venv/bin/python scripts/ws_smoke.py https://<host> /tmp/q.wav
   ```

   Expect a `ready` event, a `transcript`, `caption` and `metrics` events, reply
   audio, and finally `reply_end`.
3. Health check: `curl https://<host>/healthz` returns `{"ok":true,"speech":true}`
   (`speech` is false when `DEEPGRAM_API_KEY` is missing).
4. Latency and accuracy over many turns: `scripts/live_latency.py` (see its
   docstring); add `--barge-in` to time interruptions.
5. WebRTC against WebSocket in headless Chrome, timed where the listener hears
   the reply: `scripts/webrtc_latency.py --url https://<host> --clips <dir>`
   (`--barge-in` to time interruptions; without `--url` it measures the
   transport alone against a local stub pipeline).

## Phone calls (Telnyx)

Inbound calls to the Telnyx number reach `/telnyx/webhook` (Call Control app
`voice-agent-lab`, webhook API v2), and their audio reaches `/ws/telnyx`. Both
paths bypass Cloudflare Access: the webhook is ed25519-signed and the stream
needs a one-time per-call token. Set the three `TELNYX_*` variables in `.env`.

- An unsigned `curl -X POST https://<host>/telnyx/webhook` returns 401.
- On a trial account only verified numbers can call in (Verified Numbers in the
  portal), and calls are cut at 10 minutes: we say goodbye at 9.
- Watch a call: `sudo docker logs -f voice-agent-lab 2>&1 | grep -iE 'phone|telnyx|turn latency|barge'`.
- Local, without Telnyx: `scripts/telnyx_fake_call.py` (see its docstring).

## Updating and rolling back

Ship the source to a fresh directory, keep `.env`, rebuild only the app
container, and tag the running image first so it can be restored:

```sh
git archive <branch> | ssh ubuntu@<vm-ip> 'mkdir ~/voice-agent-lab.new && tar -x -C ~/voice-agent-lab.new'
ssh ubuntu@<vm-ip>
cp -p ~/voice-agent-lab/.env ~/voice-agent-lab.new/ && mv ~/voice-agent-lab ~/voice-agent-lab.old && mv ~/voice-agent-lab.new ~/voice-agent-lab
sudo docker tag voice-agent-lab:latest voice-agent-lab:rollback
cd ~/voice-agent-lab && sudo docker build -t voice-agent-lab:latest .
sudo docker rm -f voice-agent-lab
sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env -p 127.0.0.1:7860:7860 voice-agent-lab:latest
```

Leave the `cloudflared` container alone: restarting it changes the public URL.
To roll back, run the same `docker rm -f` and `docker run` with the rollback tag.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (required) | LLM; set it only at run time, never in the image |
| `DEEPGRAM_API_KEY` | (required) | Speech (Flux and Aura-2); run time only |
| `DEEPGRAM_HOST` | `api.in.deepgram.com` | Deepgram region; `api.deepgram.com` (US) or `api.eu.deepgram.com` elsewhere |
| `DEEPGRAM_VOICE` | `aura-2-thalia-en` | Any English Aura-2 voice |
| `FLUX_EOT_THRESHOLD`, `FLUX_EAGER_EOT_THRESHOLD`, `FLUX_EOT_TIMEOUT_MS` | `0.7`, `0.4`, `3000` | Flux end of turn: confidence for EndOfTurn, for EagerEndOfTurn (empty disables it and speculation), and the silence that forces EndOfTurn |
| `GROQ_LLM_MODEL`, `GROQ_LLM_REASONING` | see `.env.example` | LLM choice |
| `LLM_FALLBACK_MODEL` | `openai/gpt-oss-20b` | Hedge for a stalled or rate-limited LLM request; empty disables it |
| `FIRST_TOKEN_DEADLINE_MS` | `600` | Start the fallback when the primary has sent nothing for this long |
| `HOST`, `PORT` | `0.0.0.0`, `7860` in the image | Address uvicorn binds |
| `SPECULATIVE_LLM` | `1` | Start the LLM at Flux's EagerEndOfTurn; `0` turns it off |
| `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_STREAM_URL` | unset | Phone calls on the Telnyx number: Call Control key, webhook-signing public key (base64), and `wss://<host>/ws/telnyx`. Unset: `/telnyx/webhook` answers 503 |
| `TELNYX_MAX_CALL_S`, `TELNYX_MAX_CALLS` | `540`, `2` | Per phone call: goodbye and hang up after this long (trial accounts are cut at 600 s); concurrent phone calls, beyond which callers get busy |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_PASSWORD` | `127.0.0.1`, `5432`, unset | Optional history store; in memory when unset |
| `CF_TURN_KEY_ID`, `CF_TURN_KEY_API_TOKEN` | unset | Cloudflare Realtime TURN key for WebRTC media; run time only. Unset: public STUN only (see below) |
| `WEBRTC_MAX_CALL_S`, `WEBRTC_MAX_CALLS` | `900`, `4` | WebRTC call time limit (TURN credentials expire 60 s later) and concurrent WebRTC calls |

WebRTC media is UDP between the browser and the container's aiortc. With a
TURN key, each call mints its own credentials and both sides can relay through
Cloudflare, so the VM needs no inbound UDP rule. Without one, calls use STUN
only: that worked from a home network to the Mumbai VM (the security group is
stateful and the container's outbound checks open the path), but fails behind
symmetric NAT or UDP-blocking networks. `?transport=ws` keeps the WebSocket
client for comparison.

The image runs as non-root UID 1000. Dependency versions come from
`requirements.lock`, the set tested locally on Python 3.14. Linux wheels exist
for every pin on both amd64 and arm64.

## Instant test without cloud: quick tunnel to the local server

```sh
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
```

This makes the local app public at a random trycloudflare.com URL for as long as
the command runs. Anyone with the URL can use the microphone pipeline and spend
Groq quota and Deepgram credit. Stop it with Ctrl-C, or with `pkill -f "cloudflared tunnel"`.
