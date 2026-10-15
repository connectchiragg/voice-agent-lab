# Deploying for a public test

The server needs about 1.5 GB of RAM resident and 2 vCPU for one call at normal
latency. Microphone capture needs HTTPS, so the page must be served over https://
and the socket over wss://. The image bakes in the roughly 1 GB of models, and
secrets are passed only at run time.

Checked on 2026-10-14:

| Option | RAM / CPU | Fits? | Cost |
|---|---|---|---|
| **Oracle Cloud Always Free, Ampere A1** (primary) | 12 GB / 2 OCPU arm64 | Yes | Always free; a card is needed to verify the account |
| **AWS Free plan, c7i-flex.large** (fallback) | 4 GB / 2 vCPU | Yes | Free-plan credits only: 6 months, then the account is suspended |
| Hugging Face Spaces, Docker on CPU Basic | 16 GB / 2 vCPU | Yes | Creating a Docker Space now needs PRO ($9/mo) |
| Cloudflare Containers | n/a | n/a | Workers Paid plan only |
| Render, Koyeb, Railway free tiers | 0.5–1 GB | No | Free but too small |
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
   GROQ_LLM_MODEL=<same as your local .env>
   GROQ_LLM_REASONING=low
   ```

   Leave `POSTGRES_PASSWORD` unset, so history stays in memory.
6. Run `sh ~/voice-agent-lab/deploy/vm-setup.sh`. It does the following:
   - installs Docker;
   - builds the image (about 10 minutes, including the model download);
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
older account is billed for anything beyond 750 h/month of t2/t3.micro, which
has 1 GB and is too small.

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
3. Health check: `curl https://<host>/healthz` returns `{"ok":true,"models_loaded":true}`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (required) | LLM; set it only at run time, never in the image |
| `GROQ_LLM_MODEL`, `GROQ_LLM_REASONING` | see `.env.example` | LLM choice |
| `HOST`, `PORT` | `0.0.0.0`, `7860` in the image | Address uvicorn binds |
| `MODELS_DIR` | `/models` in the image | Model files |
| `STT_THREADS` | `1` in the image, `2` locally | Nemotron threads; keep at or below physical cores (2 vCPU is often 1 core) |
| `TTS_THREADS` | `1` in the image, onnxruntime default locally | Kokoro threads; containers often see all host cores |
| `KOKORO_MODEL` | `kokoro-v1.0.onnx` (fp32) | Kokoro weights in `MODELS_DIR`; build with `--build-arg KOKORO_MODEL=kokoro-v1.0.fp16.onnx` to bake in another export |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_PASSWORD` | `127.0.0.1`, `5432`, unset | Optional history store; in memory when unset |

The image runs as non-root UID 1000. Dependency versions come from
`requirements.lock`, the set tested locally on Python 3.14. Linux wheels exist
for every pin on both amd64 and arm64.

## Instant test without cloud: quick tunnel to the local server

```sh
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
```

This makes the local app public at a random trycloudflare.com URL for as long as
the command runs. Anyone with the URL can use the microphone pipeline and spend
Groq quota. Stop it with Ctrl-C, or with `pkill -f "cloudflared tunnel"`.
