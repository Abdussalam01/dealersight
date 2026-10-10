# DealerSight

DealerSight turns anonymous Ring camera events into a physical dealership funnel — visits, vehicle-area
engagements, probable test-drive sessions — and connects it to clearly labelled simulated sales and
financing data, so a weak week can be read as a traffic problem or a conversion problem.

Every figure on screen states **what measured it** and **how it was worked out**. Live Ring events,
rule-inferred results, simulated baseline history, simulated business records and AI-written text never
blur together.

Built for the Build, Ship, Shape: Amazon Developer Hackathon — **primary track: Ring**, with the
**AWS Builder** mini challenge (Amazon Bedrock). Begun after 31 August 2026; nothing here predates the
hackathon window.

![The dealer funnel](docs/screenshots/dealer-funnel.png)

---

## For reviewers: what runs without any credentials

**You do not need a Ring token or an AWS account to evaluate this project.** Clone it, start Postgres,
run the server, and everything below works immediately against the seeded dataset:

| Works with no credentials | What you will see |
|---|---|
| The whole five-stage funnel, all four dealerships, both comparison periods | Figures sourced from **Simulated baseline** events |
| Opening any figure to the records behind it | Event references, rule version, timing |
| Manufacturer and captive-finance views | Aggregates that reconcile to the network total |
| Ring events audit history | Every event DealerSight has ever seen, counted or not |
| The AI analyst | Answers via the **labelled deterministic fallback** |
| `python scripts/run_evaluation.py` | The six-scenario evaluation, 6/6 |
| `pytest tests/test_pipeline.py` | The SQS pipeline's delivery guarantees, proven against a fake queue |
| `pytest` | 129 tests |

The optional Amazon SQS event pipeline is **off by default**, so no queue, no AWS account and no extra
setup are involved in any of the above. If you do want to exercise it, see
[Trying the SQS pipeline](#trying-the-sqs-pipeline-optional) — it takes about five minutes and does not
need a Ring token.

Two things will look "off" and are **correct, designed behaviour**:

- **"Live counting paused — the Ring Playground token expired."** Ring Developer Playground tokens last
  about 30 minutes and are tied to the account that generated them, so a reviewer has none. The seeded
  funnel is unaffected; only new live events stop arriving. The header states the reason.
- **The analyst says "Deterministic fallback · Bedrock unavailable".** Without AWS credentials the
  analyst still answers, from numbers DealerSight computed, and labels the answer as not model-written.
  It never invents figures. With credentials it calls Amazon Bedrock and labels that instead.

The seeded dataset **regenerates itself relative to today**, so opening this in December produces a full
funnel for that week, with the same designed scenarios present. Live Ring events already captured are
never deleted by that refresh.

## Quick start

Requirements: Python 3.11+, Docker Desktop. A Ring Developer Playground token and AWS credentials are
optional (see above).

```powershell
docker compose up -d                 # Postgres 16 on localhost:5433
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env               # works as-is; fill in credentials only if you have them
uvicorn backend.main:app --port 8000
```

Open <http://localhost:8000>. The database schema is created and the demo dataset seeded when the server
starts — there is no separate migration or seed step. The dataset is regenerated automatically whenever
it has aged out of the current comparison period.

### Environment variables

All optional; every one has a working default. See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `RING_ACCESS_TOKEN` | *(empty)* | Ring Developer Playground token. Empty means no live events; everything else works |
| `RING_API_BASE` | `https://api.amazonvision.com` | Ring Partner API base |
| `RING_POLL_SECONDS` | `5` | Event History poll interval |
| `DEVICE_MODE` | `demo` | `demo` arms the one Playground camera per position; `production` uses fixed placements |
| `ARM_MINUTES` | `10` | How long a demo camera assignment lasts |
| `DATABASE_URL` | `postgresql://dealersight:dealersight@localhost:5433/dealersight` | Local development credentials only |
| `AWS_PROFILE` | *(empty)* | Named AWS profile, or empty for the default credential chain |
| `AWS_REGION` | `us-east-1` | Bedrock region |
| `BEDROCK_MODEL_ID` | `amazon.nova-pro-v1:0` | Model used by the analyst |
| `EVENT_PIPELINE` | `local` | `local` ingests directly and needs no AWS account. `sqs` routes Ring events through Amazon SQS |
| `SQS_QUEUE_URL` / `SQS_DLQ_URL` | *(empty)* | Required only for `sqs` mode. If the queue URL is empty, DealerSight stays in `local` mode |

Never commit `.env`. It is in `.gitignore`.

## Architecture

```mermaid
flowchart LR
  subgraph Ring
    RP["Ring Developer Playground - one synthetic camera"]
    API["Ring Partner API - devices and event history"]
  end

  subgraph DealerSight["DealerSight - local FastAPI app"]
    POLL["Poller - every 5s, server-side only"]
    WRK["Queue worker - sqs mode only"]
    ING["Ingestion - validate, de-duplicate, resolve camera position"]
    COR["Correlation - zone rules, FIFO pairing, rebuilt from raw events"]
    FUN["Funnel and patterns - per-stage provenance"]
    CHK["Finding checks - pairing sensitivity"]
    PKT["Metric packet - addressable facts only"]
    VAL["Claim validator - value, unit, direction, meaning"]
    UI["Single-page UI"]
  end

  DB[("PostgreSQL 16 - raw events, derived events, simulated business records")]
  BR["Amazon Bedrock - Converse API - amazon.nova-pro-v1:0"]
  SQS["Amazon SQS - optional, EVENT_PIPELINE=sqs"]
  DLQ["Dead-letter queue - after 5 failed receives"]

  RP --> API
  API --> POLL
  POLL -->|local mode| ING
  POLL -->|sqs mode| SQS
  SQS --> WRK
  SQS --> DLQ
  WRK --> ING
  ING --> DB
  DB --> COR
  COR --> DB
  DB --> FUN
  FUN --> UI
  FUN --> CHK
  CHK --> UI
  CHK --> PKT
  FUN --> PKT
  PKT --> BR
  BR --> VAL
  VAL --> UI
```

**Two AWS services, both doing real work.** Amazon Bedrock explains the metric packet. Amazon SQS
optionally carries Ring events from the poller to ingestion, separating polling from processing so a
failed ingest is retried rather than lost — with a dead-letter queue for messages that keep failing.
AWS IAM scopes the project's credentials to exactly those two. **The queue is opt-in and off by
default**, so the project runs end to end with no AWS account at all. Nothing is deployed to AWS; the
application runs locally and calls these services over their APIs. Setup, the IAM policy, failure
handling and a model benchmark are in [`docs/aws.md`](docs/aws.md).

At-least-once delivery is safe here because ingestion is keyed on the Ring event id under a unique
constraint, and a message is deleted only after the database transaction commits.

### Trying the SQS pipeline (optional)

**Without an AWS account**, the delivery guarantees are still verifiable — a fake queue stands in for SQS
and records exactly what the application asked it to do:

```powershell
pytest tests/test_pipeline.py -v
```

Those tests pin the three properties the design depends on: a duplicate delivery changes no metric, a
failed ingest leaves the message undeleted so SQS can redeliver it, and an unreadable message is dropped
rather than retried forever.

**With an AWS account**, about five minutes of setup runs real events through a real queue:

1. Create a standard queue `dealersight-events-dlq`, then `dealersight-events` with **visibility timeout
   60s** and a redrive policy pointing at the first with **maximum receives 5**.
2. Add the `sqs:SendMessage`, `ReceiveMessage`, `DeleteMessage` and `GetQueueAttributes` statement to
   your IAM policy, listing **both** queue ARNs.
3. In `.env`: `EVENT_PIPELINE=sqs`, plus `SQS_QUEUE_URL` and `SQS_DLQ_URL`.

Full console steps and the exact IAM statement are in
[`docs/aws.md`](docs/aws.md#setting-up-the-queue).

Then prove the round trip **without needing a Ring token** (stop the server first, or its own worker
consumes the message before the script sees it):

```powershell
python scripts\queue_check.py
```

It publishes one synthetic Ring-shaped event and reports each step — queue depth before, sent, received,
handled, deleted, queue depth after. The event names a device that does not exist, so ingestion rejects
it as unknown and **the funnel data is left untouched**: this checks the transport, not the funnel.

Restart the app and the **Ring events** page will read *Event delivery: **Amazon SQS** · 0 queued · 0 in
flight · 0 dead-lettered* instead of *direct*. A live Ring event then travels
`poller → SQS → worker → PostgreSQL` and produces exactly the same counted visit as in `local` mode —
the route changes, the result does not.

The AWS work is a multi-step workflow, not a single text-generation call: DealerSight computes every
number, derives a structured metric packet, runs deterministic checks over alternative pairing
assumptions, sends only facts to Bedrock, then validates every claim in the reply against the packet —
for value, unit, direction **and meaning** — before any of it reaches the screen.

**Not implemented:** webhook ingestion. Ring's event classification (`sub_type`: human, vehicle,
other_motion) exists only on the `motion_detected` webhook, and the Playground simulates live-view
sessions rather than motion webhooks, so there is no way to exercise that path without real hardware.
DealerSight polls Event History instead. This is a planned capability, not a built one.

## Demo: a Ring event becomes a metric

The Playground exposes one synthetic camera, so demo mode assigns it to a camera position before you
generate an event. In production each Ring device has one configured placement.

1. Open **Ring events** → **Camera and session controls**, and click a position, e.g. **Entrance**.
   The assignment lasts 10 minutes.
2. Trigger any event in the Ring Developer Playground.
3. Within about a poll the event appears in the audit history stamped **Counted**, and the dealer
   funnel's visits increase by one.

For a full test-drive session: arm **Lot: Departure lane**, trigger, then arm **Lot: Return lane** and
trigger again 30 seconds to 5 minutes later. The pair becomes one *probable test-drive session*.

Events arriving while nothing is armed are kept in the audit history and stamped **Not counted · no
camera armed**. Nothing is hidden.

![Ring audit history](docs/screenshots/ring-audit-history.png)

## Zones and rules

Three zones, four camera positions. Every threshold lives in [`config/rules.yaml`](config/rules.yaml),
and each derived event stores the `rule_version` that produced it.

| Zone | Camera position | Rule | Result |
|---|---|---|---|
| 1 Entrance | Entrance | Repeat activity on the same camera within **30 s** of a counted visit is the same visit | Anonymous visit |
| 2 Vehicle Display Area | Display Area | Activity lasting at least **20 s** (Ring `end - start`) | Vehicle-area engagement *signal* (not proof of purchase intent) |
| 3 Lot Entrance & Exit | Departure lane, Return lane | A Return-lane event inside the timing window closes the **oldest** open departure (FIFO); departures with no return expire | *Probable* test-drive session |

Timing windows for Zone 3: **production** 5–90 minutes, **demo** 30 seconds–5 minutes (compressed so a
demo can show a full session; the UI states which is active).

A departure and a return cannot be tied to the same vehicle anonymously, so a match is a probable session
inferred from time and sequence only. While a departure is open it holds a short-lived random token,
cleared when the departure is matched or expires.

Derived events are recomputed from stored raw events in order of Ring start time, under one advisory-locked
transaction, so late arrivals, duplicate deliveries and replays cannot change the result.

## The funnel and the three views

One connected, **anonymous operational funnel**. It compares aggregate activity at each stage and never
identifies a customer or follows one person from a visit to a sale.

```
visits -> vehicle-area engagements -> probable test-drive sessions -> vehicle sales -> captive-finance deals
\_______________ Ring events + DealerSight rules _______________/   \____ simulated business data ____/
```

Rates: engagements ÷ visits · probable sessions ÷ engagements · sales ÷ probable sessions · finance deals
÷ sales. A rate with a zero denominator shows `—`, never 0%.

| View | Shows | Data |
|---|---|---|
| Dealer | Five-stage funnel with each stage's sources, change vs the previous period, visits by hour, patterns worth investigating | Ring + simulated |
| Manufacturer | Aggregates per dealership and region, reconciling to the network total | Aggregates only |
| Captive finance | A financing promotion against the equal-length period before it | Aggregates only |

![Manufacturer view](docs/screenshots/manufacturer-network.png)

![Captive-finance view](docs/screenshots/finance-promotion.png)

### Data provenance

Five origins, never merged:

| Label | Meaning |
|---|---|
| **Live Ring** | An actual Ring Partner API event from the Developer Playground |
| **Ring replay** | A recorded Ring event replayed for rehearsal, never shown as live |
| **Simulated baseline** | Seeded history in Ring event shape — *not* Ring-originated |
| **Simulated record** | A simulated sale or finance deal. Ring cannot observe either |
| **AI-generated analysis** | Text written by Amazon Bedrock, validated against the metric packet |

Colour on this page means *how a figure was obtained*, never whether it is good. System state — counted,
not counted, failed — prints in ink and rule weight only, so a status can never be mistaken for a
provenance claim.

**Every number can be opened.** Clicking a funnel stage shows the records behind exactly that figure,
with their event references and the rule version that produced them.

![Opening a figure](docs/screenshots/evidence-drawer.png)

**Seeded demo data** ([`backend/seed.py`](backend/seed.py), fixed seed, all names fictional): four
dealerships in two regions. Cedar Ridge Motors receives live Ring Playground events on top of its seeded
history; the others are simulated only. Counts are monotonic — visits ≥ engagements ≥ probable sessions ≥
sales ≥ finance deals — and every finance deal references a real simulated sale.

**Patterns are detected, not written.** The metrics layer compares periods and flags situations such as
"probable test drives held steady while sales fell 30%". It describes what changed and never claims a
cause. A finding disappears when its underlying data changes.

## The AI analyst

Three fixed questions, no free text. **DealerSight computes every number**; Amazon Bedrock receives a
packet of facts derived from the database, explains it, and never calculates.

![The AI analyst](docs/screenshots/ai-analyst.png)

Each claim in the reply must name a metric in the packet and match it on value, unit and direction. The
reply is also checked for *meaning*: a summary that describes a metric as moving the opposite way, or
calls a material change flat, is rejected even though it satisfies every structural rule. A rejected
answer is replaced by the labelled deterministic fallback, and the rejection is logged and shown.

### Check this finding

A probable test-drive session is an assumption about time and sequence, not an observation of a vehicle.
**Check this finding** re-runs the pairing rule over the same stored events under three configured
matching windows and reports whether the finding survives them, alongside checks for usable data in both
periods, equal coverage, comparable sources and the presence of business records.

![Checks performed](docs/screenshots/checks-performed.png)

If a critical check fails, the verdict is **withheld** and names what is missing. If the evidence does not
support the premise, it says **not supported** rather than finding a weaker claim that holds. Nothing in
this workflow is written to the database.

## Evaluation

```powershell
python scripts/run_evaluation.py
```

Six named scenarios run through the real pipeline and compare behaviour with a stated expectation,
including the cases where the honest answer is that the evidence supports nothing. Each runs in a
transaction that is rolled back, so it is safe against a live database. Full method and results:
[`docs/evaluation.md`](docs/evaluation.md).

## Privacy and ethics

DealerSight measures **activity, not identity**.

- **No facial recognition, no biometrics, no identity, no credit decisions.** None of these exist in the
  codebase and none are planned.
- **No footage.** DealerSight requests only Event History metadata — event type, start, end, device. It
  never requests live view, clips or snapshots, deliberately: those create `on_demand` events that would
  pollute the history it counts.
- **No individual is followed between stages.** Stages compare aggregate counts. A visit is never linked
  to a sale.
- **Short-lived anonymous tokens only.** While a lot departure is open it holds a random token to pair it
  with a return; the token is cleared the moment it matches or expires. It identifies nothing.
- **Aggregates leave the dealership, raw events do not.** The manufacturer and captive-finance views
  receive totals only.
- **User-assigned device names**, never raw Ring device identifiers or tokens, are shown in the interface.
- All dealerships, regions, promotions, model groups, sales and finance records are **fictional**.

## Limitations

Stated plainly, because the product's argument is that you should know what a number can support:

- **Test-drive sessions are probable, not confirmed.** The same vehicle cannot be verified anonymously.
  The count depends on the matching window, which is why "Check this finding" exists.
- **Engagements are signals, not intent.** Twenty seconds in the vehicle display area is activity, not a
  purchase decision.
- **Sales and finance records are simulated.** Ring cannot observe a sale, so the business half of the
  funnel demonstrates the analysis workflow rather than a verified dealership outcome.
- **The Playground cannot classify events.** All simulated triggers arrive as `on_demand`, so zones come
  from configured camera placement rather than from what the camera saw. See
  [`docs/friction-log.md`](docs/friction-log.md).
- **One synthetic camera.** The Playground provides a single device, so demo mode assigns it to one
  position at a time.
- **Polling, not webhooks**, with the consequences described under Architecture.
- **Counting a visit is not counting a customer.** Successful polling does not prove every person was
  captured, and DealerSight does not claim a confidence score it cannot justify.

## Replay (rehearsal only)

```powershell
python scripts/replay.py scenario_a
```

Replays a fixed scenario (10 visits, 3 engagements, 3 probable sessions, 1 expired departure) through the
normal ingest path on a separate "Replay camera (not live)" device. Replayed events are labelled
**Ring replay** and are never shown as live. Running it starts a new counting session.

## Tests

```powershell
pytest                                  # 129 tests
pytest tests/test_scenarios.py -q       # the six evaluation scenarios
pytest tests/test_pipeline.py -q        # the SQS delivery guarantees, against a fake queue
python scripts/run_evaluation.py        # the same scenarios, as a report
```

Tests use a separate `dealersight_test` database on the same Postgres container, created automatically.
They need Docker running; they do **not** need Ring or AWS credentials — Bedrock and SQS are both
stubbed, including a scenario that asserts the labelled fallback appears when Bedrock is unavailable.
Tests are pinned to `local` mode regardless of what is in your `.env`, so enabling the queue cannot
change what the suite exercises.

## Project documentation

- [`docs/scope.md`](docs/scope.md) — data provenance, zones, camera positions, gates, live walkthrough
- [`docs/aws.md`](docs/aws.md) — AWS services, setup, IAM policy, failure handling, model benchmark
- [`docs/evaluation.md`](docs/evaluation.md) — the scenario evaluation and what it does not claim
- [`docs/ring-findings.md`](docs/ring-findings.md) — what the Ring Playground and API actually return
- [`docs/friction-log.md`](docs/friction-log.md) — developer friction, with evidence in `docs/evidence/`
- [`docs/product-feedback.md`](docs/product-feedback.md) — feedback on every tool, API and SDK used
- [`docs/submission.md`](docs/submission.md) — project description and submission answers

## Licence

[MIT](LICENSE). Dependencies are listed in [`requirements.txt`](requirements.txt). The self-hosted fonts
in `frontend/fonts/` are Archivo and Archivo Narrow under the SIL Open Font License 1.1; their provenance
is recorded in [`frontend/fonts/PROVENANCE.md`](frontend/fonts/PROVENANCE.md). No third-party trademark,
logo or copyrighted asset is included.
