# Ring Findings (Phase 0 spike)

Fill this in from real Playground results. Design decisions in later phases come from here.

## Setup
- Playground token obtained: yes, 2026-09-21
- Token lifetime observed:
- `GET /v1/devices` and `GET /v1/history/devices/{id}/events` both work from our own Python code (`scripts/ring_capture.py`)
- Bedrock Converse hello world works (2026-09-21)

## Devices
| Display name | Device type | component_ids? | Proposed zone |
|---|---|---|---|
| Playground Device | Doorbell Pro (per `image_url`), 1080p, motion zones + privacy zones supported | none seen | TBD |

**Only one device per Playground account.**

## Events
- History event shape: `data[].id` (stable, long `ava1.ring.history.event...`), `attributes.event_type`, `attributes.start` / `attributes.end` (**epoch milliseconds**), `relationships.source.data.id` (device), `relationships.cv_detections.data`, `meta.riid`
- Triggered Motion, Vehicle, and Package in the Playground (2026-09-21). **All appear in history only as `event_type: "on_demand"`**, with `cv_detections: []`. There is nothing in the payload that says which trigger was used.
  - Independently confirmed by another Ring-track project (`josepha-mayo/attest`): "The Playground simulator only offers Package/Vehicle/Motion triggers, which history records as `on_demand`."
- Each event has a duration (`end - start`, ~20–40 s observed)
- Events appear in history within about a minute; they can be triggered repeatedly (good for the video)
- Webhooks: not available in Playground token mode (the helloworld README lists webhooks only for refresh-token mode). A Playground token also cannot deliver to localhost.

## Implications
- Gate 1 is reachable: a real Ring Playground event (`on_demand`) can be fetched and change a stored metric.
- The Playground **cannot** tell entrance person vs display person vs lot vehicle. Real `motion_detected` events with `sub_type` need a registered app (refresh-token mode + webhooks) and/or a physical Ring device.

- History `event_types` filter (`motion`, `motion.human`, `motion.vehicle`, `motion.package`, `on_demand`) returns the same 8 events for every value, so it is ignored for Playground events.

## Decisions (2026-09-21)
- **Zone mapping:** a camera's zone comes from its installation location, not the event label. **Playground Demo Mode** lets the operator reassign the single Playground device to Entrance / Display Area / Lot Exit / Lot Return; each assignment is stored with `valid_from`/`valid_to` and `zone_source = demo_assignment`. **Production Mode** stores one permanent zone per Ring device ID (`zone_source = device_configuration`).
- **Zone resolution:** by the event's Ring `start` time against the assignment timeline, never by arrival time (polling lag would mislabel events).
- **Classification source:** none. Every Ring event is generic camera activity. Always trigger the Playground **Motion** button for consistency.
- **Engagement rule input:** event duration (`end - start`) in the Display Area zone.
- **Depart vs return:** separate Lot Exit and Lot Return assignments, FIFO matching, with `production` and `demo` timing profiles.
- **Ingestion guards:** ignore events before the demo-session watermark; DealerSight never requests live view or snapshots from the demo device (those would create `on_demand` events).
- **Cost:** $0. No camera purchase. In parallel: finish developer registration + webhook path, ask Ring/hackathon support for more sandbox devices.

## Limitations to document in the README
- The Playground exposes one synthetic camera; demo mode reassigns it to simulate multiple installed cameras.
- The Playground doesn't classify person vs vehicle; zones rely on camera placement.
- Demo timing profile compresses test-drive windows for the video.
