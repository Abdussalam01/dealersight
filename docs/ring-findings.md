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
| Playground Device | Doorbell Pro (per `image_url`), 1080p, motion zones + privacy zones supported | none seen | Demo Mode: armed per camera position |

**Only one device per Playground account.**

## Events
- History event shape: `data[].id` (stable, long `ava1.ring.history.event...`), `attributes.event_type`, `attributes.start` / `attributes.end` (**epoch milliseconds**), `relationships.source.data.id` (device), `relationships.cv_detections.data`, `meta.riid`
- Triggered Motion, Vehicle, and Package in the Playground (2026-09-21). **All appear in history only as `event_type: "on_demand"`**, with `cv_detections: []`. There is nothing in the payload that says which trigger was used.
  - Independently confirmed by another Ring-track project (`josepha-mayo/attest`): "The Playground simulator only offers Package/Vehicle/Motion triggers, which history records as `on_demand`."
- Each event has a duration (`end - start`, ~20–40 s observed)
- Events appear in history within about a minute; they can be triggered repeatedly (good for the video)
- Webhooks: the temporary Playground token does not provide a documented path for configuring or testing webhook delivery (the helloworld README lists webhook events only under refresh-token mode). See FL-04.

## Implications
- Gate 1 is reachable: a real Ring Playground event (`on_demand`) can be fetched and change a stored metric.
- The Playground **cannot** tell entrance person vs display person vs lot vehicle. Classified motion events would require a registered app and an eligible real Ring device, and even then subtypes aren't guaranteed (see Real-device testing below).

- History `event_types` filter (`motion`, `motion.human`, `motion.vehicle`, `motion.package`, `on_demand`) returned the same 8 events for every value, and a controlled re-test (documented composite syntax, `ding` control, invalid value) confirmed the filter is ignored for Playground events (FL-02).

## Decisions (2026-09-21)
- **Zones:** three zones, four camera positions (Entrance; Display Area; Lot Entrance & Exit with Departure lane and Return lane). In production, the zone comes from the configured placement of each Ring device (`zone_source = device_configuration`). In Playground Demo Mode, the operator temporarily **arms** the single synthetic device for a camera position before generating an event (`zone_source = demo_assignment`, stored with `valid_from`/`valid_to`). The Playground does not report a location.
- **Position resolution:** by the event's Ring `start` time against the assignment timeline, never by arrival time (polling lag would mislabel events).
- **Classification source:** none. Every Ring event is generic camera activity. Always trigger the Playground **Motion** button for consistency.
- **Engagement rule input:** event duration (`end - start`) in the Display Area zone.
- **Depart vs return:** Departure-lane and Return-lane positions within Zone 3, FIFO matching, with `production` and `demo` timing profiles.
- **`on_demand` rule:** Demo Mode accepts `on_demand` only while a position is armed. Production Mode excludes `on_demand` (live views) from physical funnel metrics unless future evidence validates it.
- **Ingestion guards:** ignore events before the demo-session watermark; DealerSight never requests live view or snapshots (those would create `on_demand` events).
- **Cost:** $0. No camera purchase. In parallel: finish developer registration + webhook path, ask Ring/hackathon support for more sandbox devices.

## From the full Ring API docs (read 2026-09-21)
- **Polling is officially supported:** the docs describe Event History as the "API polling alternative" to webhooks, so our primary ingestion path is sanctioned. (A registered app still must register a Webhook URL that returns 200.)
- **`on_demand` means a live-view session** started by a user or partner. This confirms that any live view or snapshot DealerSight requests would create extra events.
- **`is_third_party_reviewed`** = whether *our app* has accessed media for that event. Useful as an audit flag.
- **History pagination:** newest first; `links.next` carries the filters and `page[key]` cursor. During initial synchronization, the poller follows `links.next` through the required pages. During later polls, it stops after reaching an already-processed event.
- **Time-gated:** history only returns events after the user's consent date.
- `GET /v1/devices?include=status,capabilities` returns related data in one call (fewer requests).
- **Webhooks (future path):** verify HMAC-SHA256 over the **raw body bytes** (`X-Signature: sha256=<hex>`), respond 2xx within 5 s, dedupe on `meta.request_id`, return 5xx (not 4xx) for retryable errors. Motion `data.id` = `<device_id>_motion_<timestamp>`; documented `sub_type` values: `motion`, `human`, `vehicle`, `other_motion` (not guaranteed on every event). `timestamp_readable` has no timezone, so always use the epoch `timestamp`.
- **Real-device testing:** Ring's staging documentation currently lists "an active Ring Protection subscription plan" and "Ring devices registered to your account (for full testing)" as prerequisites. An eligible, properly configured real camera *may* provide human or vehicle motion subtypes, depending on its capabilities, subscription, Smart Alert settings, and event payload. It does not guarantee a classification for every event.
- App credentials (Client ID, Secret, HMAC key) are shown **once** and can't be regenerated without deleting the app.

## Limitations to document in the README
- The Playground exposes one synthetic camera; demo mode temporarily arms it for one of four camera positions to stand in for multiple installed cameras.
- The Playground doesn't classify person vs vehicle; zones rely on configured camera placement.
- `on_demand` (live view) events are only counted while a demo position is armed.
- Demo timing profile compresses test-drive windows for the video.
