# Lead tracker

VALR keeps one **lead** per company or opportunity in the owner's job search. Each lead
is built from **touchpoints**: Gmail messages about his applications and interviews,
interviews on his calendar, Fathom recordings and his scored interviews. Every lead gets a
**temperature** from 0 to 100, and the parts of that score are shown with it. The page at
`/valr/leads` draws all leads as a 3D globe.

```
Gmail ──users.watch──> Pub/Sub topic valr-gmail ──push (OIDC)──> POST /hooks/gmail
                                                                  │ verify token, account
every 30 min (Schedule valr-gmail-poll) ──┐                        ▼
"Check Gmail now" on the page ────────────┴──> GmailSyncWorkflow (id valr-gmail-sync)
                                                 1. gmail_sync: history.list from the stored historyId
                                                    (first run: 30-day backfill), then for each new message:
                                                    heuristics -> Claude Haiku -> keep / drop (logged, no content)
                                                 2. leads_refresh: + calendar_events, Fathom, performance.json
                                                    -> merge -> stage, expiry, temperature -> leads table
daily (Schedule valr-gmail-watch) ──> GmailWatchWorkflow: users.watch again (Google ends a watch after 7 days)
```

## How it works

1. **Push.** Gmail's `users.watch` publishes "the mailbox changed" (account and
   historyId only) to the Pub/Sub topic. The push subscription POSTs it to
   `https://valr.haciensus.com/hooks/gmail`. Pub/Sub adds a Google-signed OIDC token.
   `app/leads_web.py` accepts the request only if:
   - the token verifies (RS256, Google's keys);
   - its audience is `VALR_GMAIL_PUSH_AUDIENCE`;
   - its email is the push service account (`VALR_GMAIL_PUSH_SA`), with `email_verified`;
   - the body names our subscription and `VALR_GMAIL_ACCOUNT`.

   The app then starts the sync with signal-with-start: a burst of pushes makes one
   more pass, not many. A bad token gets 403. If Temporal is down the answer is 503,
   and Pub/Sub retries with backoff (10 s to 10 min, for up to 1 day).
2. **Fetch.** The research worker (`app/lead_worker.py`) calls `history.list`
   (messageAdded) from the stored historyId and fetches each new message. If that
   historyId is too old (404), it searches the last 3 days instead. Messages already
   decided are skipped, so pushes and polls can overlap safely. The safety poll runs
   every 30 minutes and covers missed pushes. The daily schedule renews the watch.
3. **Classify** (`app/lead_classify.py`). Cheap heuristics run first; the model sees only
   what they cannot settle.

   The heuristics drop:
   - spam, drafts and chats;
   - job-board mail (LinkedIn, Naukri, Instahyre, Indeed, Glassdoor, Wellfound...),
     unless it says a person messaged him ("sent you a message", InMail,
     "interested in your profile");
   - Gmail's Promotions tab;
   - other bulk mail (List-Unsubscribe, List-Id, Precedence bulk/list/junk, the Social
     and Forums tabs), unless the subject has a process word such as "interview",
     "assessment" or "your application";
   - mail with no job-search word at all.

   These always go to the model: mail from ATS and scheduling tools (Greenhouse, Lever,
   Ashby, Calendly...) and replies in a thread that is already on a lead.

   The model is Claude Haiku 5.5 through `app/llm_gateway.py` (OpenRouter BYOK, with
   Anthropic direct as the fallback), with thinking off. It returns a decision, a
   category, a confidence and a reason. It also extracts the company, the role, the
   sender's role, a stage hint, the sentiment, an explicit cue (positive: "moving
   forward"; negative: "regret to inform"), a 20-word paraphrased summary and the next
   step. A message is kept when the model says keep with confidence 0.5 or more.

   Limits:
   - at most `VALR_LEADS_LLM_PER_DAY` (300) model calls a day;
   - none once the research month cap (`VALR_INTERVIEW_MONTH_USD`) is reached;
   - spend is recorded in `research_spend` under job `leads-<date>`.

   When a limit is reached, or the model fails, the message is marked **deferred**. The
   next pass retries up to 40 deferred messages.
4. **Merge** (`app/leads.py`). Leads are rebuilt from all touchpoints, in time order, on
   every refresh. The result is the same whatever order the sources arrived in.
   - A touchpoint that names a company joins the lead with that company. Names match
     fuzzily: legal suffixes are removed, then either the difflib ratio is 0.88 or more,
     or one name is the start of the other ("Juniper" matches "Juniper Robotics").
   - Otherwise it joins the lead with the same company domain. An agency's or a tool's
     domain never counts.
   - A touchpoint that names no company joins by domain, then by one of its people's
     email addresses, then by a person's name.
   - So an agency recruiter who pitches two companies **links** the two leads instead of
     merging them.
   - Emails and calendar interviews can start a lead. Recordings and scores only attach
     to an existing lead.
5. **Stage, expiry and next step.**
   - The stage moves only forward: sourced → applied → screening → interviewing → offer.
   - A touchpoint can close a lead: closed-won or closed-lost.
   - A hint of screening or later re-opens a closed-lost lead.
   - A lead that is not closed, with no touchpoint for 21 days and nothing scheduled, is
     **ghosted**.
   - With no touchpoint for **30 days** and nothing scheduled, a lead is **expired**
     (archived and hidden by default). Any new touchpoint revives it.
   - The next step is the next calendar interview, else the latest email's next step
     from the last 14 days.

### Temperature (0–100)

| Part | Points | Formula |
|---|---|---|
| Recency | 0–35 | 35 × 0.5^(days since the last touch / 7): halves every week |
| Stage | 0–25 | 25 × weight: sourced 0.2, applied 0.35, screening 0.55, interviewing 0.8, offer and won 1.0, ghosted 0.05, lost 0 |
| Momentum | 0–20 | 12 × min(1, touches in the last 14 days / 5), + 8 if an interview is on the calendar in the next 14 days |
| Quality | 0–20 | Interview performance: 8 × (mean score for the company / 10), or 4 with no score; + recruiter tone: 6 × (mean sentiment of the last 3 inbound emails + 1) / 2, or 3; + latest explicit cue: positive 6, none 3, negative 0 |

Caps: closed-lost at most 10, ghosted at most 20. Each lead carries its parts (points,
maximum and a one-line reason), and the panel shows them as bars. Example: an email
7 days ago, at screening, sentiment +1, a positive cue, and no score. That gives
17.5 + 13.75 + 2.4 + (4 + 6 + 6), so 50.

## Data model (Postgres, created if missing)

| Table | What | Not stored |
|---|---|---|
| `gmail_state` (one row) | account, last processed `history_id`, `watch_expires`, last push, last sync, backfill start, end and count, last error (class name) | tokens |
| `lead_emails` | one row per message ever looked at: message and thread id, received time, **sender domain**, decision (keep/drop/deferred), method (heuristic/llm/deferred), reason, category, confidence, $ | subject, addresses, any text |
| `lead_touchpoints` | id (`gmail:<msg>`, `cal:<event>`, `fathom:<recording>`, `perf:<interview>`), kind, time, lead id, `data` jsonb: company, domain, people [{email, name, role}], title, **derived summary** (≤ 24 words), direction, sentiment, cue, stage hint, next step, Gmail/Fathom link, interview-detail link, score, role; `snippet_enc`: the Gmail snippet (≤ 200 characters), Fernet-encrypted with VALR_TOKEN_KEY, never shown | message bodies |
| `leads` | the last rebuild: company, key, domain, role, stage, status (active/expired), temperature, parts, contacts [{email, name, role}], touch count, last touch, next step, `archived_at` | |

The page rebuilds the leads from the touchpoints on each request, so temperatures are
current. The `leads` table is the snapshot from the last worker refresh, for other readers.
Calendar touchpoints outlive `calendar_events`, which deletes events 3 days after they
end. A cancelled event's touchpoint is removed.

Logs carry counts and decisions only, for example `VALR lead email: keep by llm
(scheduling, 0.93)`. They never include an address, a subject or a text.

## The page and the globe

`GET /valr/leads` is the page. `GET /valr/api/leads` returns
`{leads, contacts, links, stages, generated_at, gmail}`. `POST /valr/api/leads/sync`
starts a sync now; it needs the CSRF token and a same-origin JSON request. All three need
the owner's Access login (VALR_ADMIN_EMAILS).

On the page:
- **Filters**: active, expired or all; stage; minimum temperature.
- **Globe**: click a lead for its panel (stage, the temperature bars, people, next step,
  and a timeline with summaries, Gmail and Fathom links and the interview-detail link).
  Click a person for their leads.
- **List**: every lead, below the globe. On phones (420 px or narrower) the list comes
  first and the globe appears on a tap. Without WebGL or JavaScript, the list is the page.

### Embedding the globe: `static/leads-globe.js`

```js
import { mount } from "/static/leads-globe.js";
const data = await (await fetch("/valr/api/leads")).json();
const globe = await mount(document.querySelector("#hero-globe"), data, {
  onSelect(node) {            // null, or {type: "lead", id, lead} / {type: "contact", id, contact}
    if (node?.type === "lead") location.href = "/valr/leads#" + node.id;
  },
  // maxNodes: 120 (40 under 640 px), labels: 24 (10), reducedMotion: media query,
  // threeUrl: "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"
});
globe.supported;      // false without WebGL: nothing is drawn, so show a list
globe.update(data);   // new data, e.g. after filtering (same shape)
globe.select(id);     // highlight a lead or person and its neighbours (no onSelect)
globe.focus(id);      // rotate it to the front (instant with reduced motion)
globe.hidden;         // leads left out by maxNodes
globe.destroy();
```

How it draws:
- Leads sit on a sphere along a Fibonacci spiral, hottest first, and leads that share
  people sit next to each other.
- Node size follows activity (touches in 30 days). Colour runs from cold blue to hot
  orange, through CSS variables `--temp-cold`, `--temp-cool`, `--temp-warm` and
  `--temp-hot` (defined in `static/leads.css`).
- People float at 1.3× the radius, above their leads, joined by hairlines. Leads that
  share a person are joined by blue arcs.
- Hot leads (60 or more) get a halo. Expired leads are faded.

Controls:
- Drag to rotate, with inertia; wheel or pinch to zoom.
- Auto-rotation starts 2.5 s after the last input and stops while a node is selected.
- With prefers-reduced-motion there is no auto-rotation, no inertia and no animated focus.
- The globe renders only while it is on screen.

The container needs only a size; the component adds a canvas and a label layer, so give
it `position: relative`. The only dependency is three.js r160, pinned on jsDelivr.
`layout()` and `tempColor()` are pure and tested with `node --test tests/leads-globe.test.mjs`.

## Set-up

**GCP (done on 2026-10-16, project `framer-511620`, number 157133842980).** This is the
same project as the OAuth client, which `users.watch` requires.
- Cloud Pub/Sub API enabled. No billing account is linked, and none was asked for.
- Topic `projects/framer-511620/topics/valr-gmail`;
  `gmail-api-push@system.gserviceaccount.com` has Pub/Sub Publisher on it.
- Service account `valr-gmail-push@framer-511620.iam.gserviceaccount.com`, with no roles
  and no keys.
- Push subscription `projects/framer-511620/subscriptions/valr-gmail-push`:
  - endpoint `https://valr.haciensus.com/hooks/gmail`;
  - authentication on with that service account, audience
    `https://valr.haciensus.com/hooks/gmail`;
  - payload unwrapping off; acknowledgement deadline 30 s; retention 1 day;
  - retry with exponential backoff, 10–600 s; never expires.

**Owner.**
1. Add a Cloudflare Access **bypass** for `valr.haciensus.com/hooks/gmail` (exact path,
   POST).
2. Set in the VM `.env`: `VALR_LEADS=1`, `VALR_GMAIL_TOPIC`, `VALR_GMAIL_SUBSCRIPTION`,
   `VALR_GMAIL_PUSH_SA` and `VALR_GMAIL_PUSH_AUDIENCE` (values in `.env.example`).
3. Deploy both containers.

At start, the research worker creates the two schedules and triggers each once. The
first poll backfills 30 days (at most 500 messages); the first watch turns push on.

To check: `sudo docker exec voice-research python -m app.lead_worker stats` (counts only),
`... sync` and `... watch`.

Settings:

| Variable | Default | |
|---|---|---|
| `VALR_LEADS` | `0` | `1` creates the schedules; pushes are acknowledged but ignored while `0` |
| `VALR_LEADS_LLM_PER_DAY` | `300` | model calls a day |
| `VALR_LEADS_BACKFILL_DAYS` | `30` | how far the first run reads back |
| `VALR_LEADS_BACKFILL_MAX` | `500` | most messages the first run reads |
| `VALR_LEADS_POLL_MINUTES` | `30` | the safety poll |
| `VALR_PERFORMANCE_PATH` | `/data/performance.json` | interview scores |
| `FATHOM_API_KEY` | | recordings from the last 60 days, up to 50 |

## Measurements and cost

**Classifier**, on the 25 synthetic emails in `tests/fixtures/lead_emails.json`
(`scripts/eval_lead_classifier.py`, real Haiku 5.5 through OpenRouter BYOK, 2026-10-16):
- **25/25** decisions correct, and stage hints 10/10.
- The heuristics settled 12 emails without the model; 13 reached it.
- Per model call: about 870 input and 170 output tokens, **$0.00017**, latency p50 1.4 s,
  maximum 2.1 s.
- A first run with thinking on hit `max_tokens` on one email. Thinking is now off.

**Monthly cost.** Pub/Sub: a notification is about 1 KB. The 10 GiB monthly free tier
is about 10 million notifications, so this costs **$0**. The model costs about $0.0002
per email that reaches it:

| Volume | Model calls | Cost |
|---|---|---|
| Backfill (500 messages, if half reach the model) | about 250 | $0.04–0.05, once |
| Steady state: 150 emails a day, 30% reaching the model | about 45 a day | $0.008 a day, about **$0.25 a month** |
| At the cap: 300 calls a day | 300 a day | $0.05 a day, $1.55 a month at most |

## Tests

| File | What it covers | How it runs |
|---|---|---|
| `tests/test_leads.py` | merging, stages, expiry, temperature, graph | |
| `tests/test_lead_classify.py` | heuristics on the eval set, reply parsing, contacts | |
| `tests/test_leads_web.py` | OIDC with generated RSA keys, the webhook's answers, owner-only page and JSON, CSRF | |
| `tests/test_lead_workflow.py` | pokes and the refresh | Temporal test server |
| `tests/test_lead_worker.py` | backfill, history, recovery, budget deferral, watch, encrypted snippet, no content in the log | fake Gmail, fake model, Postgres via `VALR_TEST_POSTGRES` |
| `tests/leads-globe.test.mjs` | the globe's layout | `node --test` |

Preview with made-up data: `VALR_DEV_PREVIEW=1 python scripts/preview_leads.py`, then
open http://127.0.0.1:8091/valr/leads.

## Not yet verified

- A real push end to end: it needs the Access bypass and a deploy.
- The backfill on the real mailbox.
- Fathom matching on real recordings.
- The interview-detail link points at `/valr#performance`; there is no per-interview
  page yet.
