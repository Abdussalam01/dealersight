# DealerSight

DealerSight turns anonymous Ring camera events into a physical dealership funnel (visits, vehicle-area engagements, probable test-drive sessions) and connects it to clearly labeled simulated sales and financing data.

> Status: early development (Phase 2). Full documentation comes later; this is the quick start.

## Quick start (local)

Requirements: Python 3.11+, Docker Desktop, a Ring Developer Playground token.

```powershell
docker compose up -d                          # Postgres on localhost:5433
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                        # paste your Ring Playground token into RING_ACCESS_TOKEN
uvicorn backend.main:app --port 8000
```

Open http://localhost:8000.

## Demo: Ring event to metric

The Ring Developer Playground exposes one synthetic camera, so demo mode lets the operator temporarily assign that camera to one of DealerSight's camera positions before generating an event. In production, each Ring device has one configured placement.

1. Click **Entrance** to arm the camera position (the arm expires after 10 minutes).
2. Trigger **Motion** in the Ring Developer Playground.
3. Within about a minute the event appears as **Counted** and visits increase by one.

Events that arrive while nothing is armed are stored as evidence but never counted.

## Zones and rules

Three zones, four camera positions. Every threshold lives in `config/rules.yaml`, and each derived event stores the `rule_version` that produced it.

| Zone | Camera position | Rule | Result |
|---|---|---|---|
| 1 Entrance | Entrance | Repeat activity on the same camera within **30 s** of a counted visit is the same visit | Anonymous visit |
| 2 Vehicle Display Area | Display Area | Activity lasting at least **20 s** (Ring `end - start`) | Vehicle-area engagement *signal* (not proof of purchase intent) |
| 3 Lot Entrance & Exit | Departure lane, Return lane | A Return-lane event inside the timing window closes the **oldest** open departure (FIFO); departures with no return expire at the end of the window | *Probable* test-drive session |

Timing windows for Zone 3: **production** 5–90 minutes, **demo** 30 seconds–5 minutes (compressed so a demo can show a full session; the UI shows which is active).

A departure and a return can't be tied to the same vehicle anonymously, so a match is a probable session inferred from time and sequence only. While a departure is open it holds a short-lived random token; the token is cleared when the departure is matched or expires.

Derived events are recomputed from stored Ring events in order of Ring start time, so late arrivals, duplicate deliveries, and replays can't change the result.

## Replay (rehearsal only)

```powershell
python scripts/replay.py scenario_a
```

Replays a fixed scenario (10 visits, 3 engagements, 3 probable sessions, 1 expired departure) through the normal ingest path on a separate "Replay camera (not live)" device. Replayed events are labeled **Ring replay** and are never shown as live. Running it starts a new counting session.

## Tests

```powershell
pytest
```

Tests use a separate `dealersight_test` database on the same Postgres container.

## Project notes

- `docs/scope.md`: data provenance, zones, camera positions, gates
- `docs/ring-findings.md`: what the Ring Playground and API actually return
- `docs/friction-log.md`: developer friction with evidence in `docs/evidence/`
