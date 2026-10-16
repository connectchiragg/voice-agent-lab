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

   His **own outgoing mail** never goes to the model. In a thread that is already on a
   lead (or kept in the same pass), it is kept as a touchpoint ("You replied in this
   thread."). Otherwise it is skipped.

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
   - A later message in a Gmail thread that is already on a lead stays on that lead.
   - A touchpoint that names a company joins the lead with that company. Names match
     fuzzily: legal suffixes are removed, then either the difflib ratio is 0.88 or more,
     or one name is the start of the other ("Juniper" matches "Juniper Robotics").
   - Otherwise it joins the lead with the same company domain. An agency's or a tool's
     domain never counts.
   - A touchpoint that names no company joins by domain, then by one of its people's
     email addresses, then by a person's name.
   - So an agency recruiter who pitches two companies **links** the two leads instead of
     merging them.
   - Emails and calendar events can start a lead. Recordings and scores only attach to
     an existing lead.
   - Calendar events count when:
     - the title says interview, round, technical, coding and so on, with a company or
       an outside attendee (stage hint: interviewing);
     - the title says intro, recruiter or screen, with an outside attendee (a recruiter
       chat; stage hint: screening);
     - the organizer or an attendee is already a contact on a Gmail lead (screening).
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

### Corrections (the lead panel's "Correct this lead")

The owner's corrections are stored in `lead_overrides` and applied by every rebuild,
on the page and in the worker.
- **Dismiss** hides the lead. Its touchpoints and Gmail threads, as they were at that
  moment, never count again: a new message in the same thread does not bring it back.
  A new thread or calendar event can start it again.
- **Set stage** fixes the stage until it is set back to "Automatic stage". The
  temperature uses it, and the stage part says "(set by you)".
- **Merge into** folds the lead, with its touchpoints and people, into another lead.

API: `POST /valr/api/leads/<id>/override` {action: dismiss | stage | merge, stage?, into?}.
It needs the owner, the CSRF token and a same-origin JSON request.

### Temperature (0–100) and the four bands

Every live lead lands in one of four bands. Closed leads are their own grey band, never "cold".

| Band | Score | Means |
|---|---|---|
| **Hot** | 75–100 | An active process, next step soon (an interview within a week, an offer) |
| **Warm** | 50–74 | Engaged recently (a reply, an interview, a take-home in the last days) |
| **Cool** | 25–49 | Applied or sourced, waiting |
| **Cold** | 0–24 | Quiet or ghosted (about 2.5 weeks without their move, or 21 days of silence) |
| Closed | – | Won or not moving forward: its own filter, hidden on the globe until "Show closed" |

| Part | Points | Formula |
|---|---|---|
| Recency | 0–30 | 30 × 0.5^(days since **their** last move / 7). His own follow-up emails do not warm a lead. |
| Stage | 0–30 | 30 × weight: sourced 0.2, applied 0.3, screening 0.55, interviewing 0.8, offer and won 1.0, ghosted 0.1, lost 0 |
| Momentum | 0–25 | Next step: an interview within 7 days 15, within 14 days 9, an open offer 15; + 10 × min(1, touches in the last 14 days / 4) |
| Quality | 0–15 | Interview score 5 × mean/10 (2.5 with none) + recruiter tone 5 × (mean of the last 3 inbound sentiments + 1)/2 (2.5) + latest cue: positive 5, none 2.5, negative 0 |

Caps: ghosted at most 24 (always Cold), closed-lost at most 10. Each lead carries its parts
(points, maximum, a one-line reason), and the card shows them as bars.

The archetypes in `tests/test_leads_temperature.py` are the spec (scores within ±2):

| Situation | Score | Band | Ball in court |
|---|---|---|---|
| Final round tomorrow after two strong rounds | 90 | Hot | nobody (booked) |
| Interviewing, next interview in 3 days | 86 | Hot | nobody (booked) |
| Offer received 3 days ago | 84 | Hot | **you** (decide) |
| Recruiter screen booked in 2 days | 68 | Warm | nobody (booked) |
| Interviewed 2 days ago, waiting for feedback | 66 | Warm | them |
| Take-home sent 3 days ago, not submitted | 60 | Warm | **you** |
| Recruiter replied yesterday asking for availability | 58 | Warm | **you** |
| You answered the recruiter's question yesterday | 53 | Warm | them |
| Interviewed 2 weeks ago, then silence | 46 | Cool | them |
| Applied 2 days ago, no reply | 44 | Cool | them |
| A recruiter reached out 4 days ago | 37 | Cool | them |
| A recruiter question unanswered for 15 days | 32 | Cool | nobody (cooling) |
| Applied 20 days ago, no reply | 21 | Cold | nobody |
| Interviewing, then 25 days of silence (ghosted) | 15 | Cold | nobody |
| Rejected | 10 | Closed | nobody |

**Counts.** Every lead is in exactly one bucket: **expired** (30 quiet days, nothing
scheduled; hidden), else **closed** (won or lost; status `closed`), else **live** (status
`active`). Home's hero, the Leads header and chips, the legend and the globe all use these
buckets (`leads.counts`, `contracts.leads`), so the numbers agree: live = Hot + Warm + Cool + Cold.

### Ball in court (`waiting_on`: you, them or nobody)

`leads.ball_in_court`, beside the temperature, with a one-line reason.
- **you**, only when all hold: the lead is live and not Cold; their latest email comes from a
  real person (no-reply senders, ATS acknowledgements, job alerts and rejections never count);
  it asks something of him (availability, a reply, documents, an assignment, an offer decision,
  salary details); he has not acted since (no email of his in that thread; for a scheduling
  ask, no calendar event after it); and it is under 14 days old. Older: nobody, the lead cools.
  Reason: "Recruiter: share your availability (2 days ago)".
- **them**: he moved last (replied, applied, interviewed) or their message asks nothing.
  "Waiting on Acme for 3 days". After 21 days: nobody (the lead is Cold by then).
- **nobody**: cold, closed and expired leads; an interview already booked.

"Asks something" comes from the classifier: it now returns `action_requested` and `action`
(≤ 8 words), stored on the touchpoint's `data` (old rows: unknown). For an old row, only a
stored next step that clearly asks him ("Reply with availability"), or an offer, counts.
Home's to-dos and "waiting on you", the Leads chip "Waiting on you" (`?waiting=you`) and the
lead card all read `waiting_on`; the to-do shows the reason and the Gmail thread.

## Data model (Postgres, created if missing)

| Table | What | Not stored |
|---|---|---|
| `gmail_state` (one row) | account, last processed `history_id`, `watch_expires`, last push, last sync, backfill start, end and count, last error (class name), `reconnect_alerted_for` (the expired token's connected_at, so the alert goes once) | tokens |
| `lead_emails` | one row per message ever looked at: message and thread id, received time, **sender domain**, decision (keep/drop/deferred), method (heuristic/llm/deferred), reason, category, confidence, $ | subject, addresses, any text |
| `lead_touchpoints` | id (`gmail:<msg>`, `cal:<event>`, `fathom:<recording>`, `perf:<interview>`), kind, time, lead id, `data` jsonb: company, domain, people [{email, name, role}], title, **derived summary** (≤ 24 words), direction, sentiment, cue, stage hint, next step, Gmail/Fathom link, interview-detail link, score, role, Gmail thread id; `snippet_enc`: the Gmail snippet (≤ 200 characters), Fernet-encrypted with VALR_TOKEN_KEY, never shown | message bodies |
| `lead_overrides` | the owner's corrections, replayed in order: lead id, kind (dismiss, stage, merge), value (the stage, or the lead merged into), the dismissed touchpoint ids and threads, who, when | |
| `leads` | the last rebuild: company, key, domain, role, stage, status (active/closed/expired), temperature, parts, contacts [{email, name, role}], touch count, last touch, next step, `archived_at` | |

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

`GET /valr/leads` is the app's Leads page (app/valr_ui/leads.py), and Home uses the same
globe as its hero. The page reads `app.leads_web.collect` (the same JSON as
`/valr/api/leads`, which now adds `band`, `waiting_on`, `waiting_reason` per lead and
`counts`), normalised in app/valr_ui/contracts.py:
- **Globe**: static/leads-globe.js (three.js) gets one small record per live and closed
  lead (no people, no links); static/valr-globe-lite.js is the fallback. Clicking a lead
  opens it in the list (on Home: a small preview card).
- **Legend**: "Hot 5 · Warm 10 · Cool 35 · Cold 12", each a link to that band, and a
  "Show closed 39" toggle that adds the closed leads, in grey, at the bottom of the globe.
- **Filters**: Live, Waiting on you, the band chips (Hot, Warm, Cool, Cold, Closed), the stage
  chips, a search box, and "Expired" (`?band=&stage=&waiting=you&q=&status=expired`).
- **List and lead card**: sorted by temperature with a band pill ("Hot 82"), the ball in
  court under the company, stage, next step, last activity; the card adds whose move it is,
  the temperature breakdown, the people, the latest timeline entries with their Gmail,
  recording and interview links, and the corrections (stage, merge, dismiss). "Check Gmail
  now" posts the sync.

### Embedding the globe: `static/leads-globe.js`

```js
import { mount } from "/static/leads-globe.js";
const data = await (await fetch("/valr/api/leads")).json();
const globe = await mount(document.querySelector("#hero-globe"), data, {
  onSelect(node) {            // null, or {type: "lead", id, lead}
    if (node?.type === "lead") location.href = "/valr/leads#" + node.id;
  },
  // maxNodes: 150, labels: 80, reducedMotion: media query, now: Date (for "3 days ago"),
  // threeUrl: "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"
});
globe.supported;      // false without WebGL: nothing is drawn, so show a list
globe.update(data);   // new data, e.g. after filtering (same shape)
globe.select(id);     // highlight a lead (no onSelect)
globe.focus(id);      // rotate it to the front (instant with reduced motion)
globe.hidden;         // leads left out by maxNodes
globe.destroy();
```

How it draws:
- Only leads: people and their links belong in the lead card.
- A Fibonacci sphere ordered by temperature: hottest at the top, coldest at the bottom,
  closed last, latitudes within ±64° so nothing piles up at a pole. Auto-rotation brings
  every lead to the front.
- Colour = band: a distinct colour per band (blue, teal, amber, red; grey for closed) with a
  small ramp inside it, from CSS variables `--band-<band>-lo` and `--band-<band>` in
  `static/leads.css` (a light and a dark set; the globe panel always uses the dark one). The
  node test checks the four stay apart under deuteranopia, protanopia and tritanopia.
- Size = stage (sourced smallest, offer largest), so size and colour say different things.
  Hot leads get a halo.
- Every live lead has a label (up to 80). A label that would overlap a nearer one is hidden
  until its lead is hovered or focused; labels on the back fade out. Labels are buttons: Tab
  reaches them, focus turns the lead to the front. Hovering shows "Company · band · score ·
  stage · last touch".

Controls:
- Drag to rotate, with inertia; wheel or pinch to zoom.
- Auto-rotation starts 2.5 s after the last input and stops while a node is selected.
- With prefers-reduced-motion there is no auto-rotation, no inertia and no animated focus.
- The globe renders only while it is on screen.

The container needs only a size; the component adds a canvas and a label layer, so give
it `position: relative`. The only dependency is three.js r160, pinned on jsDelivr.
`bandOf()`, `bandColor()`, `layout()`, `placeLabels()` and `tipText()` are pure and tested with `node --test tests/leads-globe.test.mjs`.

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

**When the Gmail token expires.** If the refresh token is refused (`invalid_grant`: the
OAuth app's Testing mode lasts 7 days, or access was revoked), the sync stops. VALR sends
the owner's linked Telegram chat one message per expiry: "Gmail disconnected — reconnect:
https://valr.haciensus.com/oauth/google/start". A reconnect followed by another expiry
sends one more message.

A push for another account or subscription is acknowledged and ignored, with one log
line and no alert.

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
- The heuristics and the own-mail rule settled 13 emails without the model; 12 reached it.
- Per model call: about 870 input and 170 output tokens, **$0.00017**, latency p50 1.4 s,
  maximum 2.5 s.
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
| `tests/test_leads.py` | merging (threads too), stages, expiry, temperature, graph, corrections | |
| `tests/test_lead_classify.py` | heuristics on the eval set, reply parsing, contacts | |
| `tests/test_leads_web.py` | OIDC with generated RSA keys, the webhook's answers, owner-only page and JSON, CSRF, the correction endpoint | |
| `tests/test_lead_workflow.py` | pokes and the refresh | Temporal test server |
| `tests/test_lead_worker.py` | calendar rules, scorecard anchors; backfill, history, recovery, his own replies, budget deferral, watch, encrypted snippet, no content in the log, corrections storage, the Telegram alert (once per expiry) | fake Gmail, fake model, Postgres via `VALR_TEST_POSTGRES` |
| `tests/test_leads_temperature.py` | the band archetypes, counts, ball in court | |
| `tests/leads-globe.test.mjs` | bands, colours (colour-blind check on static/leads.css), layout, label collisions, tooltip | `node --test` |

Preview with made-up data: `VALR_DEV_PREVIEW=1 python scripts/preview_ui.py`, then open
http://127.0.0.1:8090/valr/leads: about 107 fictional leads shaped like the real mailbox
(`tests/leads_sample.live_shaped_touches`) through the real `leads.build` and `leads.graph`;
`?leads=small` for the small sample.

## Not yet verified

- A real push end to end: it needs the Access bypass and a deploy.
- The backfill on the real mailbox.
- Fathom matching on real recordings.
