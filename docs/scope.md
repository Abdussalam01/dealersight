# DealerSight Scope (frozen at end of Phase 0)

Governing rules: see the project instructions. If a feature conflicts with them, the feature shrinks.

## Data provenance
| Category | What | Label in UI |
|---|---|---|
| Ring-originated | Live `motion_detected` events from Ring Developer Playground devices, read via the Ring Partner API | **Ring** |
| Inferred by DealerSight | Visits (entrance), vehicle-area engagements (display), probable test drives (lot exit/return), dedup, matching | **Inferred** |
| Simulated | Fictional dealers, sales, captive-finance deals, financing promotion, historical baseline events | **Simulated** |
| AI-generated | Amazon Bedrock explanations of computed metric packets | **AI-generated** |

## Device → zone mapping
| Mode | How a Ring event gets its zone | `zone_source` |
|---|---|---|
| Playground Demo Mode | The single Playground device is reassigned to Entrance / Display Area / Lot Exit / Lot Return via a labeled "Demo camera assignment" control; the zone is resolved from the assignment active at the event's start time | `demo_assignment` |
| Production Mode | Each physical Ring device ID has one permanent installation zone | `device_configuration` |

> Because the Ring Playground exposes one synthetic camera, demo mode lets us reassign that camera to one of our dealership installation zones. A production deployment stores the same zone assignment permanently against each physical Ring device ID.

Ring's event ID, timestamps, and duration always come from Ring. Only the zone is configuration. Details: `ring-findings.md`.

## Required analyst questions
1. Why did this dealer's conversion rate decline during the selected period?
2. Which dealers had increased probable test-drive activity without a corresponding increase in sales?
3. How did the selected financing promotion perform across the complete funnel?

## Gates passed
| Gate | Date |
|---|---|
| 1 Ring feasibility | |
| 2 Event reliability | |
| 3 Complete funnel | |
| 4 Meaningful AWS use | |
| 5 Submission readiness | |
