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

## Gates passed
| Gate | Date |
|---|---|
| 1 Ring feasibility | 2026-09-21: live Playground event armed at Entrance counted once (visits 0 → 1), unarmed event stored as `not_armed`; expiry, duplicate, and 9 more rules covered by 13 passing tests. Evidence: `docs/evidence/gate1_live_ring_event.png` |
| 2 Event reliability | 2026-09-23: live Playground run produced a visit (Entrance), an engagement signal (Display Area, 40 s vs. 20 s threshold), and a probable test-drive session (departure 16:34:22 → return 16:36:06, 1.7 min); 27 tests cover duplicates, out-of-order events, missing returns, expired windows, overlapping departures, and repeated replay |
| 3 Complete funnel | |
| 4 Meaningful AWS use | |
| 5 Submission readiness | |
