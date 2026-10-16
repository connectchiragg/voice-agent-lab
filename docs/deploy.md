# Deploying for a public test

Speech runs on Deepgram and the LLM on Groq, so the server itself is small: the
image is about 450 MB and used about 100 MB of RAM during a call on the VM. Microphone capture needs HTTPS, so the
page must be served over https:// and the socket over wss://. Secrets are passed
only at run time.

Checked on 2026-10-14:

| Option | RAM / CPU | Fits? | Cost |
|---|---|---|---|
| **Oracle Cloud Always Free, Ampere A1** (primary) | 12 GB / 2 OCPU arm64 | Yes | Always free; a card is needed to verify the account |
| **AWS Free plan, t4g.small** (in use) | 2 GB / 2 vCPU arm64, burstable | Yes, with 2 GB swap | Free-plan credits only: 6 months, then the account is suspended |
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

   Leave `POSTGRES_PASSWORD` unset to keep history in memory, or set up
   Postgres as below.
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

## In use: AWS Free plan, t4g.small (arm64)

Only for an account on the **Free plan**, created on or after 2025-07-15. An
older account is billed for anything beyond 750 h/month of t2/t3.micro (1 GB).

The lab runs on this since 2026-10-16 (moved from a c7i-flex.large, 4 GB x86_64,
which cost about 7.5 times as much per hour):

| | |
|---|---|
| Instance | `i-0a5a070417e9a1146`, Name `voice-agent-lab-arm`, ap-south-1b (Mumbai) |
| Type | **t4g.small**: 2 vCPU Graviton2 (arm64), 2 GB RAM, Free-plan eligible |
| CPU credits | **standard** mode, never unlimited: no surplus-credit charges |
| Disk | 25 GB gp3 root, plus a 2 GB `/swapfile` (`vm.swappiness=10`) |
| Image | Ubuntu 24.04 arm64 (`ubuntu-noble-24.04-arm64-server-20261004`) |
| Network | security group `voice-agent-lab`: SSH only, from the owner's IP; no other inbound port, the tunnels are outbound |
| SSH | `ssh -i ~/.ssh/voice-agent-lab_ed25519 ubuntu@<vm-ip>` (key pair `voice-agent-lab`) |

The public IPv4 is not an Elastic IP: it changes if the instance is stopped and
started. Read it with
`aws ec2 describe-instances --region ap-south-1 --instance-ids i-0a5a070417e9a1146 --query 'Reservations[0].Instances[0].PublicIpAddress'`,
and update the security group if your own IP changes. Nothing else depends on
it: voice.haciensus.com and valr.haciensus.com go through the named tunnel.

**CPU credits.** A t4g.small's baseline is 20% of each vCPU. It earns 24
credits an hour (one credit is one vCPU at 100% for a minute) and banks at most
576. Unlike T2, T3 and T4g get no launch credits in standard mode, so a new
instance starts at 0 and is held to the baseline until it has banked some. The
first image build took well over 10 minutes for that reason, and a rebuild of the
app layers alone takes about 1.5 minutes. The lab idles at a few percent of
CPU, so it banks about 20 credits an hour. Check the balance before a
CPU-heavy job such as a dependency rebuild:

```sh
aws cloudwatch get-metric-statistics --region ap-south-1 --namespace AWS/EC2 \
  --metric-name CPUCreditBalance --dimensions Name=InstanceId,Value=i-0a5a070417e9a1146 \
  --start-time $(date -u -v-1H +%FT%TZ) --end-time $(date -u +%FT%TZ) --period 300 --statistics Average
```

At 0 credits the instance still runs, at 20% of each vCPU.

**Memory.** All eight containers use about 600 MB together when idle. About
1 GB is left free, plus swap. `voice-research` is capped at 768 MB.

To build another one:

1. Run `aws login`, then check the account's plan in the Billing console.
2. Launch EC2 with Ubuntu 24.04 **arm64**, t4g.small, `CpuCredits=standard`, a
   25 GB gp3 disk, and SSH only from your IP. Add a 2 GB swapfile.
3. Follow steps 4–6 above, using `ubuntu@<ec2-ip>`. Every pinned wheel exists for
   arm64 (the Dockerfile installs binary wheels only, so a missing one fails the build).
4. Free-plan credits ($100, plus up to $100 more for onboarding tasks) are
   spent per hour on the instance (t4g.small $0.0112/h in ap-south-1), its
   disk ($0.0912/GB-month) and its public IPv4 address ($0.005/h).

**Moving the lab to another VM** (done on 2026-10-16 with 2m16s of public
downtime):

1. Build both images on the new VM first.
2. On the old VM, stop `voice-agent-lab`, `voice-callbacks`, `voice-research`,
   `temporal-ui` and `temporal`.
3. Dump everything:
   `sudo docker exec voice-postgres pg_dumpall -U voice_agent | gzip`.
   The dump holds the app database, `temporal` and `temporal_visibility`, so
   pending workflows, timers and schedules move with it.
4. On the new VM, create Postgres as in the Postgres section, then restore with
   `zcat dump.sql.gz | sudo docker exec -i voice-postgres psql -U voice_agent -d postgres`.
   Two "already exists" errors, for the role and the database, are expected.
5. Copy `.env`, `.env.research`, `/srv/valr-data` and `~/.cloudflared-named-token`
   with `tar -p`, which keeps their modes.
6. Start `temporal` and `temporal-ui` (their `docker run` lines are in
   `deploy/temporal-setup.sh`), then the app, `voice-callbacks` and `voice-research`.
7. Start `cloudflared-named` on the new VM, and only then stop it on the old one.
8. Check: `temporal workflow list` shows the same running workflows, and
   `temporal task-queue describe --task-queue voice-callbacks` (and
   `voice-research`) shows pollers.

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

- Audio is wideband: the call is answered with L16 at 16 kHz both ways
  (`stream_codec`, `stream_bidirectional_codec`, `stream_bidirectional_sampling_rate`),
  and Flux and Aura run at 16 kHz linear16, so our side never resamples. Telnyx's
  L16 payloads are little-endian, not RTP network order: big-endian was heard as
  loud static on a real call and Flux transcribed nothing
  (`TELNYX_L16_BYTE_ORDER`, default `little`). `TELNYX_CODEC=PCMU` returns to
  8 kHz µ-law. Each call logs what Telnyx actually sends:
  `Phone media: caller audio L16 little-endian 16000 Hz, replies L16 little-endian 16000 Hz`.
  If the caller's line is loud for 8 s after the greeting but Flux hears no words,
  the log says `caller audio may be undecodable: codec L16, byte order little`:
  try the other byte order, or `TELNYX_CODEC=PCMU`. A PSTN caller's
  own leg is usually still 8 kHz G.711, which Telnyx transcodes; that leg caps
  what the caller can hear.
- An unsigned `curl -X POST https://<host>/telnyx/webhook` returns 401.
- Recording, for debugging (`TELNYX_RECORD=1`): each answered call is recorded
  by Telnyx once its stream starts, as dual-channel WAV (the caller on one
  channel, the assistant on the other). `call.recording.saved` logs the
  recording id and duration and stores them in `call_recordings` (id, call
  control id, created_at, duration). Telnyx's download URLs expire after 10
  minutes, so only the id is kept; `scripts/fetch_recording.py [ID]` gets a fresh
  URL and saves the file (the latest recording without an id). Telnyx charges
  $0.002 per recorded minute; storage is free. Callers are not told they are
  recorded.
- On a trial account only verified numbers can call in (Verified Numbers in the
  portal), and calls are cut at 10 minutes: we say goodbye at 9.
- Watch a call: `sudo docker logs -f voice-agent-lab 2>&1 | grep -iE 'phone|telnyx|turn latency|barge'`.
- Local, without Telnyx: `scripts/telnyx_fake_call.py` (see its docstring).

## Postgres (conversation history and caller memory)

Postgres 17 runs in its own container beside the app, on a user-defined Docker
network, `voice-agent-lab`. The app reaches it by container name
(`POSTGRES_HOST=voice-postgres`); its port is also published on the VM's
loopback only (`127.0.0.1:5432`), for `psql` on the VM. We use the network
rather than `--add-host host.docker.internal:host-gateway`: a port published on
127.0.0.1 is not reachable through the host gateway address, so that route
would need Postgres to listen on the Docker bridge as well.

The data lives in the named volume `voice-agent-lab-postgres`. The password is
generated once and kept only in `~/voice-agent-lab/.env` (`POSTGRES_PASSWORD`);
Postgres reads it from a temporary file at its first start, when the volume is
initialised. Postgres 17 used 27 MB of RAM after setup (the app about 120 MB), which the 2 GB VM easily
fits. One-time setup:

```sh
cd ~/voice-agent-lab && umask 077
grep -q '^POSTGRES_PASSWORD=' .env || echo "POSTGRES_PASSWORD=$(openssl rand -hex 32)" >> .env
grep -q '^POSTGRES_HOST=' .env || echo 'POSTGRES_HOST=voice-postgres' >> .env
sudo docker network create voice-agent-lab
sudo docker volume create voice-agent-lab-postgres
init=$(mktemp) && { grep '^POSTGRES_PASSWORD=' .env; echo POSTGRES_USER=voice_agent; echo POSTGRES_DB=voice_agent; } > "$init"
sudo docker run -d --name voice-postgres --restart unless-stopped --network voice-agent-lab \
  -p 127.0.0.1:5432:5432 -v voice-agent-lab-postgres:/var/lib/postgresql/data \
  --env-file "$init" postgres:17.11-alpine
rm -f "$init"
```

The app creates its tables at startup (LangGraph's checkpoints, `callers`,
`call_summaries`, and `call_recordings` with `TELNYX_RECORD=1`). Check them with
`sudo docker exec voice-postgres psql -U voice_agent -d voice_agent -c '\dt'`.
If Postgres is down when the app starts, the app logs "Postgres unavailable"
and keeps history and memory in RAM until its next restart.

## Callbacks (Temporal)

Scheduled callbacks (README, Callbacks) need three more containers on the
`voice-agent-lab` network, all from `deploy/temporal-setup.sh`:

| Container | Image | Port (VM loopback only) | RAM measured |
|---|---|---|---|
| `temporal` | `temporalio/server:1.32.1`, all services in one process | `127.0.0.1:7233` (gRPC, for the CLI) | about 100 MB |
| `temporal-ui` | `temporalio/ui:2.55.0` | `127.0.0.1:8233` | about 15 MB |
| `voice-callbacks` | the app's image, `python -m app.callback_worker` | none | about 58 MB |

Measured in Docker on a Mac with the same images and Postgres 17.11; Postgres
itself grew from about 27 MB to 78 MB with Temporal's connections. The idle
Temporal server used 3 to 6% of one core there. All of it fits the 2 GB t4g.small (about 600 MB for all eight
containers when idle).

Temporal keeps its state in the existing `voice-postgres`, as a role `temporal`
owning two databases, `temporal` and `temporal_visibility`, so the
`voice-agent-lab-postgres` volume holds everything and is the one thing to back
up (`pg_dumpall`, or a snapshot of the VM disk). Considered instead:

- `temporal server start-dev` with a SQLite file on a volume: one container
  (about 115 MB measured, UI included) that also survived restarts with a
  timer pending, but Temporal supports it only for development, and backups
  would be a second store to copy. A fine fallback for a lab if Postgres is
  ever removed.
- `temporalio/auto-setup`: deprecated; its last tag is server 1.23.

Setup, after the app image is built and Postgres is running (it is safe to run
again, for example to upgrade Temporal by changing the versions at its top):

```sh
cd ~/voice-agent-lab && sh deploy/temporal-setup.sh
```

It:

1. adds `TEMPORAL_DB_PASSWORD` (random), `TEMPORAL_ADDRESS=temporal:7233` and
   `TEMPORAL_NAMESPACE=voice-agent-lab` to `.env` if missing;
2. creates the role and databases (the password goes through stdin) and runs
   Temporal's schema setup and migrations (`temporalio/admin-tools`);
3. starts the server (`DB=postgres12`, 4 history shards, at most 10 Postgres
   connections, `deploy/temporal-dynamicconfig.yaml` mounted read-only) and
   creates the namespace with 30 days' retention of finished workflows;
4. starts the UI and the worker.

Then restart the app container (see Updating) so it reads `TEMPORAL_ADDRESS`.
The worker uses the same `.env`: its `TELNYX_API_KEY` dials, so it calls real
numbers once the Outbound Voice Profile below is assigned. On a trial account
Telnyx only dials verified numbers anyway.

The app and worker reach the server by container name (`temporal:7233`, no
TLS); nothing is published beyond the VM's loopback, and the tunnel does not
route to it.

**Viewing workflows.** Forward the UI over SSH and open http://localhost:8233:

```sh
ssh -N -L 8233:127.0.0.1:8233 ubuntu@<vm-ip>
```

Pick the `voice-agent-lab` namespace. Or on the VM:
`sudo docker run --rm --network voice-agent-lab temporalio/admin-tools:1.32.1 temporal --address temporal:7233 -n voice-agent-lab workflow list`.
Worker logs: `sudo docker logs -f voice-callbacks`.

**Updating the worker** with the app: after rebuilding the image,
`sudo docker rm -f voice-callbacks` and run its `docker run` line from the
script again. Pending callbacks just wait while it is down. Changing the
workflow's code while callbacks are pending can break their replay; for such
a change, let them finish (or cancel them) first.

**Telnyx outbound.** Dialling needs an Outbound Voice Profile on the
voice-agent-lab Call Control app (portal: Programmable Voice > the app >
Outbound > Outbound Voice Profile > `Default` > Save). Until then every dial is
refused (HTTP 4xx, not retried) and the callback ends as failed.

## Research callbacks (VALR)

VALR (README, Research callbacks) adds one container, from the same Dockerfile's
`worker` target:

| Container | Image | Port | RAM |
|---|---|---|---|
| `voice-research` | `voice-agent-lab-worker` (`--target worker`: Python 3.13, crewai, anthropic), `python -m app.research_worker` | none | about 210 MB idle (import of crewai), capped at 768 MB (`--memory`) |

The worker image is about 1.1 GB (crewai pulls in chromadb, lancedb, pyarrow and
onnxruntime); the app image is unchanged. Check the disk first (`df -h /`); old
`rollback-*` tags share most layers, but keep only the latest two if space is
tight.

Setup, after Temporal (above):

1. Put the Anthropic key in its own file, read only by `voice-research`, without
   printing it:

   ```sh
   ssh ubuntu@<vm-ip> 'grep -q "^ANTHROPIC_API_KEY=" ~/voice-agent-lab/.env.research 2>/dev/null || echo missing'
   grep '^ANTHROPIC_API_KEY=' .env | ssh ubuntu@<vm-ip> 'umask 077; cat >> ~/voice-agent-lab/.env.research'
   ```

   The app container never gets it, so the live call path has no Claude client.
   Background Claude calls go through OpenRouter with this same Anthropic key (BYOK)
   when `OPENROUTER_API_KEY` is also in `.env.research` (app/llm_gateway.py); the
   Anthropic key stays as the direct fallback:

   ```sh
   grep '^OPENROUTER_API_KEY=' .env | ssh ubuntu@<vm-ip> 'umask 077; cat >> ~/voice-agent-lab/.env.research'
   ```

   Verify with `sudo docker exec -i voice-research python - --runs 1 --only openrouter < scripts/openrouter_smoke.py`
   (a few cents): every row ok and `is_byok: True`. Roll back to direct with
   `LLM_GATEWAY=anthropic` in `.env.research` and a restart of `voice-research`.
2. `cd ~/voice-agent-lab && sh deploy/research-setup.sh` builds the worker image
   and starts `voice-research` with `--env-file .env --env-file .env.research`.
3. Rebuild and restart the app and `voice-callbacks` too (Updating, below): the
   app has the `start_research` tool and the dashboard, and the callback worker
   passes the job id to the callback call.
4. Check it end to end without dialling anyone (a fictitious caller; it counts
   toward the limits and spends about $0.10):

   ```sh
   sudo docker exec voice-research python -m app.research_worker test "Coorg, India for a weekend trip"
   ```

   It prints the state, the time, the spend and the spoken answer.

Spend and jobs: `sudo docker exec voice-research python -m app.research_jobs`.
Worker logs: `sudo docker logs -f voice-research` (`VALR` lines: each
researcher's result and cost, `VALR cap hit ...`, and the job's total). Each
job is also a `ResearchCallbackWorkflow` in the Temporal UI, its researchers
and synthesis as activities. When updating the worker, keep `.env.research`
with `.env` (`cp -p ~/voice-agent-lab/.env* ~/voice-agent-lab.new/`).

**Dashboard** (read-only): the app serves it at `/valr`, and redirects `/` there
when the Host is `valr.haciensus.com` (`VALR_HOST`). Route the hostname in the
named tunnel to the app's origin, `http://localhost:7860`, and put a Cloudflare
Access application on it; set `VALR_ACCESS_AUD` in `.env` to that application's
AUD tag and restart the app. Every request must carry a valid
`Cf-Access-Jwt-Assertion` for it (verified like the voice page's, app/access.py),
otherwise the answer is 403; with no AUD set the dashboard is closed. It shows
anonymised caller hashes, never numbers.

**Cost** of one research request, on 2026-10-16 prices:

| Service | What VALR uses | Cost |
|---|---|---|
| Anthropic | 3 Haiku 5.5 researchers, 4 to 6 web searches, Sonnet 5.5 synthesis, Haiku claim check | about $0.08 to $0.11 per job measured; capped at $0.25 per job and $20 a month |
| Deepgram | the callback call: Flux per streamed minute ($0.0065) and Aura-2 per character ($0.030 per 1,000) | about $0.05 for a 2-minute callback |
| Telnyx | one outbound call per job (plus retries if unanswered) | Telnyx's per-minute voice rate; recordings $0.002 a minute when on |
| Groq | the callback conversation's replies | free tier (counts toward its daily limits) |

## Interview assistant (VALR)

- Private data: `/srv/valr-data` (root:1000, 0750; files 0640) holds `profile.json` and
  `evaluation.json` from the profile import, and `pronounce.json` (the TTS respelling map,
  `{"Written": "Spoken"}`, read by `app/persona.py` via VALR_PRONOUNCE_PATH, default
  `/data/pronounce.json`); never committed. It is mounted read-only
  as `/data` into `voice-agent-lab` and `voice-research`
  (`-v /srv/valr-data:/data:ro`; `deploy/research-setup.sh` adds it when the dir exists).
- Worker data (writable): the research worker's post-call loop rewrites `performance.json`
  and `performance_scores.csv` after each scored interview, so those two live in
  `/srv/valr-data-live` (owner 1000:1000, the image's `app` user; 0750; files 0600),
  mounted read-write at `/data-live` in `voice-research` only and read-only at `/data-live`
  in `voice-agent-lab`, so the pages show the fresh scores. `/data` stays read-only.
  Both containers get `VALR_PERFORMANCE_PATH=/data-live/performance.json` and
  `VALR_PERFORMANCE_CSV=/data-live/performance_scores.csv` as `-e` options (they override
  `.env`); the worker also gets `VALR_PERFORMANCE_FALLBACK=/data/performance.json`, read
  while the live file does not exist yet. `deploy/valr-data-live.sh` holds all of it:
  `sudo sh deploy/valr-data-live.sh setup` creates the dir and seeds it from
  `/srv/valr-data` with `cp -n` (never overwrites; `deploy/deploy-all.sh` runs it on every
  deploy), and `sh deploy/valr-data-live.sh app|worker` prints the docker run options
  (just the `/data` mount when the live dir does not exist). After re-running the profile
  import offline, copy the new `performance.json` into `/srv/valr-data-live` too (it is
  not re-seeded over an existing file). `evaluation.json` and `profile.json` stay in
  `/data`: nothing in the containers writes them.
- `.env`: VALR_OWNER_PHONE, VALR_OWNER_EMAILS, VALR_PROFILE_PATH=/data/profile.json,
  VALR_EVALUATION_PATH=/data/evaluation.json, VALR_CALENDAR_ICS_URL, VALR_TOKEN_KEY (a
  Fernet key), VALR_GMAIL_ACCOUNT, GOOGLE_OAUTH_CLIENT_ID/SECRET, VALR_CONTEXT_TOKEN.
  `.env.research`: FATHOM_API_KEY.
- Cloudflare Access: `valr.haciensus.com/hooks/gcal` is bypassed (Google posts there);
  everything else on the host, including `/oauth/google/*` and `/valr/context`, stays
  behind Access.
- Telegram heads-up: `valr.haciensus.com/hooks/telegram` (exact path, POST) is bypassed
  too (Telegram cannot log in); the app refuses any request without the matching
  `X-Telegram-Bot-Api-Secret-Token` and serves only the owner's linked chat. Run
  `deploy/install-telegram.sh` on the Mac (prompts for the @BotFather token, generates
  TELEGRAM_WEBHOOK_SECRET, writes both to `.env`, restarts the app and the worker, calls
  setWebhook), then link the chat with the dashboard's `/start <code>`.
- Notetakers (app/notetakers): keys are pasted on the dashboard ("Get set up") and stored
  Fernet-encrypted in Postgres with VALR_TOKEN_KEY; nothing to deploy for that. Webhooks
  need two more exact-path POST bypasses on `valr.haciensus.com`, each verified in the
  app: `/hooks/fathom` (FATHOM_WEBHOOK_SECRET, Standard Webhooks) and `/hooks/granola`
  (GRANOLA_WEBHOOK_SECRET, Standard Webhooks). Fireflies is a one-time history import
  (no webhook). An unset secret answers 503. Register them
  at each provider: Fathom Settings → API Access → Add Webhook (or `POST /webhooks`) with
  destination `https://valr.haciensus.com/hooks/fathom`, "my recordings", transcript and
  summary on, and its `whsec_` secret in `.env`; Granola Settings → Connectors → Webhooks
  (`note.generated`, Business plan and up). Everything else (`/valr/api/notetakers/*`,
  `/valr/api/recording`) stays behind Access.
- Lead tracker (docs/leads.md): `valr.haciensus.com/hooks/gmail` (exact path, POST) is
  bypassed (Pub/Sub cannot log in); the app accepts only Pub/Sub's OIDC token for the push
  service account and audience. `.env` (read by both containers): VALR_LEADS=1,
  VALR_GMAIL_TOPIC, VALR_GMAIL_SUBSCRIPTION, VALR_GMAIL_PUSH_SA, VALR_GMAIL_PUSH_AUDIENCE.
  GCP (project framer-511620, the OAuth client's project): Pub/Sub API, topic `valr-gmail`
  (gmail-api-push@system.gserviceaccount.com is Publisher), push subscription
  `valr-gmail-push` with OIDC. Check: `sudo docker exec voice-research python -m app.lead_worker stats`.
- The app container runs with the data mounts (from `~/voice-agent-lab`):
  `sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env --network voice-agent-lab $(sh deploy/valr-data-live.sh app) -p 127.0.0.1:7860:7860 voice-agent-lab:latest`
- Turning the lead tracker on and deploying, from the Mac at the repo root:
  `bash deploy/enable-leads-and-deploy.sh` (sets the five lead settings in the VM `.env`
  without printing it, sets up `/srv/valr-data-live`, runs `deploy/deploy-all.sh`, then
  prints `/healthz`, the worker's lead/post-call log lines, the two Gmail schedules and
  the lead counts).
- Check: `sudo docker exec voice-research python -m app.research_worker blocks` (dry run)
  or `... sync` (sync now, then the blocks and their workflows).

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
sudo docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env --network voice-agent-lab -p 127.0.0.1:7860:7860 voice-agent-lab:latest
```

Before replacing the container, make sure no call is live:
`ss -tn state established '( sport = :7860 )'` shows no connection from the
tunnels, and the last `Phone call connected` in the logs has a later
`Phone call ended`.

Leave the `cloudflared` container alone: restarting it changes the public URL.
To roll back, run the same `docker rm -f` and `docker run` with the rollback tag.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (required) | LLM; set it only at run time, never in the image |
| `DEEPGRAM_API_KEY` | (required) | Speech (Flux and Aura-2); run time only |
| `DEEPGRAM_HOST` | `api.in.deepgram.com` | Deepgram region; `api.deepgram.com` (US) or `api.eu.deepgram.com` elsewhere |
| `DEEPGRAM_VOICE` | `aura-2-odysseus-en` | Any English Aura-2 voice |
| `FLUX_EOT_THRESHOLD`, `FLUX_EAGER_EOT_THRESHOLD`, `FLUX_EOT_TIMEOUT_MS` | `0.7`, `0.4`, `3000` | Flux end of turn: confidence for EndOfTurn, for EagerEndOfTurn (empty disables it and speculation), and the silence that forces EndOfTurn |
| `GROQ_LLM_MODEL`, `GROQ_LLM_REASONING` | see `.env.example` | LLM choice |
| `LLM_FALLBACK_MODEL` | `openai/gpt-oss-20b` | Hedge for a stalled or rate-limited LLM request; empty disables it |
| `FIRST_TOKEN_DEADLINE_MS` | `600` | Start the fallback when the primary has sent nothing for this long |
| `HOST`, `PORT` | `0.0.0.0`, `7860` in the image | Address uvicorn binds |
| `SPECULATIVE_LLM` | `1` | Start the LLM at Flux's EagerEndOfTurn; `0` turns it off |
| `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_STREAM_URL` | unset | Phone calls on the Telnyx number: Call Control key, webhook-signing public key (base64), and `wss://<host>/ws/telnyx`. Unset: `/telnyx/webhook` answers 503 |
| `TELNYX_CODEC` | `L16` | Phone audio both ways: `L16` (16 kHz) or `PCMU` (8 kHz) |
| `TELNYX_L16_BYTE_ORDER` | `little` | L16 payload byte order, both ways (`big` is RTP network order) |
| `TELNYX_RECORD` | `0` | `1` records each phone call through Telnyx (dual-channel WAV; ids in `call_recordings`) |
| `TELNYX_MAX_CALL_S`, `TELNYX_MAX_CALLS` | `540`, `2` | Per phone call: goodbye and hang up after this long (trial accounts are cut at 600 s); concurrent phone calls, beyond which callers get busy |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_PASSWORD` | `127.0.0.1`, `5432`, unset | History and caller memory (database and role `voice_agent`); on the VM `voice-postgres`. In memory when unset or unreachable |
| `CF_ACCESS_AUD`, `CF_ACCESS_TEAM_DOMAIN` | unset, `https://small-brook-8402.cloudflareaccess.com` | Browser caller identity: the Access app's AUD tag; unset, every browser caller is anonymous |
| `CALLER_MEMORY`, `CALLER_MEMORY_LOAD_TIMEOUT_MS`, `CALLER_MEMORY_SUMMARY_WAIT_S` | `1`, `150`, `600` | Caller memory on/off, its load limit at call start, and how long a summary may wait for quiet |
| `BACKCHANNEL_IGNORE`, `BACKCHANNEL_WORDS`, `BACKCHANNEL_HOLD_MS` | `1`, see `.env.example`, `350` | While the assistant speaks, a turn of at most 3 backchannel words ("sure", "uh huh") does not barge in; speech with no words yet interrupts after the hold |
| `CALLBACK_QUIET_HOURS` | off | Callbacks refused in these hours of the caller's day, e.g. `21-9` (9 pm to 9 am); empty or `off`: any hour |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` | unset, `default` | Callbacks: the Temporal server (`temporal:7233` on the VM, no TLS) and namespace (`voice-agent-lab`). Unset: callbacks are off |
| `TEMPORAL_DB_PASSWORD` | set by `deploy/temporal-setup.sh` | Postgres password of the `temporal` role; used only by the setup script |
| `TELNYX_CONNECTION_ID`, `TELNYX_FROM_NUMBER` | `3071391970178893742`, `+12288888007` | Callback dials: the Call Control app and the caller ID |
| `ANTHROPIC_API_KEY` | unset | VALR research, in `.env.research`, read only by `voice-research` |
| `OPENROUTER_API_KEY`, `LLM_GATEWAY` | unset, `openrouter` when the key is set (else `anthropic`) | VALR research: background Claude calls via OpenRouter, pinned to Anthropic and run on our Anthropic key (BYOK, set up under openrouter.ai Integrations with "Never use shared capacity"); the voice LLM stays on Groq direct |
| `LLM_GATEWAY_FALLBACK`, `LLM_BYOK_GUARD`, `LLM_GATEWAY_TIMEOUT_S` | `1`, `refuse`, `600` | An OpenRouter error retries on Anthropic direct (`0`: fail). A response with `usage.is_byok=false` logs "VALR BYOK ALERT"; `refuse` then skips OpenRouter for the rest of the process, `warn` only logs, `off` ignores. Agent calls time out after 600 s, the inner search, check and classify calls after 60 s |
| `VALR_JOB_USD`, `VALR_JOBS_PER_CALLER_DAY`, `VALR_JOBS_PER_DAY`, `VALR_MONTH_USD` | `0.25`, `3`, `15`, `20` | VALR cost limits (README, Research callbacks); the app checks them at booking, the worker within each job |
| `VALR_SEARCHES_PER_RESEARCHER`, `VALR_PAGES_PER_RESEARCHER`, `VALR_PAGE_CHARS`, `VALR_RESULTS_PER_CALL`, `VALR_MAX_ITER`, `VALR_RESEARCHER_MODEL`, `VALR_SYNTH_MODEL` | `3`, `3`, `4000`, `5`, `5`, `claude-haiku-5-5`, `claude-sonnet-5-5` | Per-researcher caps and models (any `Caps` field in app/research_agents.py as `VALR_<NAME>`) |
| `VALR_HOST`, `VALR_ACCESS_AUD` | `valr.haciensus.com`, unset | The dashboard's hostname, and the AUD tag of its Cloudflare Access application (else `CF_ACCESS_AUD`; neither: the dashboard is closed) |
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
