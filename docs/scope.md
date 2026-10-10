# DealerSight Scope (frozen at end of Phase 0)

Governing rules: see the project instructions. If a feature conflicts with them, the feature shrinks.

## Data provenance
| Category | What | Label in UI |
|---|---|---|
| Ring-originated | Live events from the Ring Developer Playground device (`on_demand`, counted only while a demo position is armed), read via the Ring Partner API Event History; real `motion` / `motion_detected` events in production | **Ring** |
| Inferred by DealerSight | Visits (entrance), vehicle-area engagements (display), probable test drives (lot departure/return lanes), dedup, matching | **Inferred** |
| Simulated | Fictional dealers, sales, captive-finance deals, financing promotion, historical baseline events | **Simulated** |
| AI-generated | Amazon Bedrock explanations of computed metric packets | **AI-generated** |

## Zones and camera positions
Three zones (per the project rules), four camera positions:

| Zone | Camera position(s) |
|---|---|
| 1 Entrance | Entrance |
| 2 Vehicle Display Area | Display Area |
| 3 Lot Entrance & Exit | Departure lane · Return lane |

| Mode | How a Ring event gets its camera position | `zone_source` |
|---|---|---|
| Playground Demo Mode | The operator temporarily **arms** the single synthetic Playground device for one camera position before generating an event. The position is resolved from the arm active at the event's start time. | `demo_assignment` |
| Production Mode | Each Ring device has one configured placement | `device_configuration` |

> In production, the zone comes from the configured placement of each Ring device. In Playground Demo Mode, the operator temporarily assigns the single synthetic device to a camera position before generating an event. The Playground itself does not report a location.

**`on_demand` rule:** the Ring docs define `on_demand` as a live-view session. In Demo Mode it is accepted only while a position is armed; in Production Mode it is excluded from physical funnel metrics unless future evidence validates it.

Ring's event ID, timestamps, and duration always come from Ring. Only the camera position is configuration. Details: `ring-findings.md`.

## How the demo data was designed
The deterministic dataset was designed to contain specific dealership scenarios for demonstrating the analytics. DealerSight's metrics and alert logic still calculate and detect those scenarios rather than displaying hard-coded conclusions. The fixed seed in `backend/seed.py` was selected so those scenarios are present. Seeded activity is stored in Ring event shape but is always labelled `simulated_baseline`; only Ring Developer Playground events are actual Ring API events.

## Required analyst questions
1. Why did this dealer's conversion rate decline during the selected period?
2. Which dealers had increased probable test-drive activity without a corresponding increase in sales?
3. How did the selected financing promotion perform across the complete funnel?

## Full live walkthrough, 2026-10-02

One operator ran the complete chain on live Playground events, with every figure traced back to a Ring event id:

| Time (UTC) | Armed position | Ring event | Effect |
|---|---|---|---|
| 19:25:24 | Entrance | `…Z3FMTBJ3` | Counted as a visit. Dealer view moved to **252 = 1 Live Ring + 251 Simulated baseline** — the first screen on which both provenances appear on one figure. |
| 20:12:17 | Lot: Departure lane | `…PJQP2JOS` | Opened a test-drive candidate. |
| 20:16:45 | Lot: Return lane | `…S5ALHFVA` | Paired with the departure at **4.5 min**, inside the 30 s – 5 min demo window, producing **1 probable test-drive session** (confidence `probable`, rule `2026-09-21.1`, source `ring_live`). |

An earlier unarmed event on 2026-09-23 remains in the audit history stamped *Not counted · no camera armed*,
so the record shows both what counted and what did not.

## Gates passed
| Gate | Date |
|---|---|
| 1 Ring feasibility | 2026-09-21: live Playground event armed at Entrance counted once (visits 0 → 1), unarmed event stored as `not_armed`; expiry, duplicate, and 9 more rules covered by 13 passing tests. Evidence: `docs/evidence/gate1_live_ring_event.png` |
| 2 Event reliability | 2026-09-23: live Playground run produced a visit (Entrance), an engagement signal (Display Area, 40 s vs. 20 s threshold), and a probable test-drive session (departure 16:34:22 → return 16:36:06, 1.7 min); 27 tests cover duplicates, out-of-order events, missing returns, expired windows, overlapping departures, and repeated replay |
| 3 Complete funnel | 2026-09-23: live Playground event counted once on the demo dealership (252 = 1 Live Ring Playground + 251 Simulated baseline; live-session view shows the event on its own with business rates unavailable). Views reconcile dealer→region→network, promotion periods are equal 7 days with no overlap, finance deals reference real simulated sales, and findings disappear when their data changes. 46 tests. |
| 4 Meaningful AWS use | 2026-10-02: Bedrock answers grounded in a metric packet and validated before display; a rejected answer fell back deterministically and was labelled as such on screen. Model benchmark in `docs/evidence/model-benchmark.json`. **2026-10-10:** Amazon SQS verified in the live event path — a Playground trigger at 20:29:30 UTC travelled poller → queue → worker → PostgreSQL, arriving as a counted Entrance visit (`…P3P3MPL6`, live-session visits 3 → 4) with the worker's handled count moving 0 → 1 and the queue returning to empty. The same event in `local` mode earlier (`…L7K2LACX`) produced an identical result, so the route changed and the outcome did not. |
| 5 Submission readiness | **Partly verified, 2026-10-10.** Runs from the written instructions: a `.env` copied straight from `.env.example`, with no Ring token and an AWS profile the machine does not have, starts cleanly (no errors in the server log), serves the full seeded funnel, reports `RING_ACCESS_TOKEN is not set in .env` as the Ring status, and returns the labelled deterministic fallback from the analyst (`ProfileNotFound` logged, five claims still read from the packet). Repository is public with an MIT licence. Seeded data verified to regenerate for a future date: simulated for 2026-12-15, the funnel repopulated for that week with all three designed scenarios intact and existing live Ring events preserved. **Outstanding:** demonstration video, and checking every submission link while logged out. |
