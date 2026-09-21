# DealerSight

DealerSight turns anonymous Ring camera events into a physical dealership funnel (visits, vehicle-area engagements, probable test-drive sessions) and connects it to clearly labeled simulated sales and financing data.

> Status: early development (Phase 1). Full documentation comes later; this is the quick start.

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

## Tests

```powershell
pytest
```

Tests use a separate `dealersight_test` database on the same Postgres container.

## Project notes

- `docs/scope.md`: data provenance, zones, camera positions, gates
- `docs/ring-findings.md`: what the Ring Playground and API actually return
- `docs/friction-log.md`: developer friction with evidence in `docs/evidence/`
