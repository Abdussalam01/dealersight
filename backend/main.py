"""DealerSight API. Run: uvicorn backend.main:app --reload"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import config, correlation, db, funnel, ingest, metrics, seed
from backend.ring_poller import RingPoller

logging.basicConfig(level=logging.INFO)
poller = RingPoller()


@asynccontextmanager
async def lifespan(app):
    with db.connect() as conn:
        db.init_schema(conn)
        ingest.current_session(conn)
        if not conn.execute("SELECT 1 FROM dealer LIMIT 1").fetchone():
            seed.seed_all(conn)
            correlation.rebuild(conn)
    if os.getenv("DISABLE_POLLER") != "1":
        poller.start()
    yield
    poller.stop()


app = FastAPI(title="DealerSight", lifespan=lifespan)


def get_conn():
    with db.connect() as conn:
        yield conn


def demo_device(conn):
    device = conn.execute("SELECT * FROM device WHERE mode = 'demo' AND ring_device_id NOT LIKE 'replay.%%' ORDER BY id LIMIT 1").fetchone()
    if not device:
        raise HTTPException(409, "No Ring device discovered yet. Check the Ring token and wait for the first poll.")
    return device


class ArmRequest(BaseModel):
    position: str


@app.get("/api/status")
def status(conn=Depends(get_conn)):
    device = conn.execute("SELECT * FROM device WHERE mode = 'demo' AND ring_device_id NOT LIKE 'replay.%%' ORDER BY id LIMIT 1").fetchone()
    now = ingest.utcnow()
    armed = ingest.assignment_at(conn, device["id"], now) if device else None
    return {
        "ring": poller.status,
        "mode": config.DEVICE_MODE,
        "device": device["display_name"] if device else None,
        "armed": {"position": armed["position_code"], "name": armed["position_name"], "until": armed["valid_to"]} if armed else None,
        "watermark": ingest.current_session(conn)["watermark"],
        "server_time": now,
    }


@app.get("/api/positions")
def positions(conn=Depends(get_conn)):
    return conn.execute(
        """SELECT p.code, p.name, z.name AS zone FROM camera_position p JOIN zone z ON z.id = p.zone_id ORDER BY p.id"""
    ).fetchall()


@app.post("/api/demo/arm")
def arm(request: ArmRequest, conn=Depends(get_conn)):
    try:
        row = ingest.arm(conn, demo_device(conn)["id"], request.position)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"armed": request.position, "until": row["valid_to"]}


@app.post("/api/demo/disarm")
def disarm(conn=Depends(get_conn)):
    ingest.disarm(conn, demo_device(conn)["id"])
    return {"armed": None}


class TimingRequest(BaseModel):
    profile: str


@app.post("/api/demo/new-session")
def new_session(conn=Depends(get_conn)):
    """Reload the same seeded baseline and start a fresh live session.

    Live Ring events from earlier sessions stay in the audit history (recent events)
    but no longer count toward the dashboard.
    """
    profile = ingest.current_session(conn)["timing_profile"]
    seeded = seed.seed_all(conn)
    session = conn.execute(
        "INSERT INTO demo_session (watermark, timing_profile) VALUES (now(), %s) RETURNING *", (profile,)
    ).fetchone()
    correlation.rebuild(conn)
    return {"session": session, "seeded": seeded}


@app.post("/api/demo/timing")
def set_timing(request: TimingRequest, conn=Depends(get_conn)):
    """Switch the test-drive timing profile (demo = compressed for the video, production = real window)."""
    if request.profile not in correlation.load_rules()["test_drive"]["profiles"]:
        raise HTTPException(400, f"unknown timing profile: {request.profile}")
    conn.execute("UPDATE demo_session SET timing_profile = %s WHERE id = %s",
                 (request.profile, ingest.current_session(conn)["id"]))
    correlation.rebuild(conn)
    return metrics.summary(conn)


@app.get("/api/events/recent")
def recent(conn=Depends(get_conn)):
    return ingest.recent_events(conn)


@app.get("/api/metrics/visits")
def visits(conn=Depends(get_conn)):
    return {"visits": metrics.visit_count(conn), "source": "Ring events at the Entrance position (inferred)"}


@app.get("/api/metrics/summary")
def summary(conn=Depends(get_conn)):
    return metrics.summary(conn)


@app.get("/api/metrics/hourly")
def hourly(dealer_id: int | None = None, conn=Depends(get_conn)):
    _, period_b = funnel.periods()
    row = conn.execute("SELECT timezone FROM dealer WHERE id = %s", (dealer_id,)).fetchone() if dealer_id else None
    return metrics.hourly_visits(conn, dealer_id, *period_b, timezone=row["timezone"] if row else "UTC")


@app.get("/api/dealers")
def dealer_list(conn=Depends(get_conn)):
    return funnel.dealers(conn)


@app.get("/api/funnel")
def dealer_funnel(dealer_id: int | None = None, scope: str = "all", conn=Depends(get_conn)):
    """Dealer view: the five-stage funnel for the current period, with each stage's sources.

    scope=live shows only Ring events from the current demo session, so a live
    Playground event is visible on its own instead of being lost in the seeded history.
    """
    period_a, period_b = funnel.periods()
    if scope == "live":
        watermark = ingest.current_session(conn)["watermark"]
        period_b = (watermark, period_b[1])
    dealer = conn.execute(
        "SELECT * FROM dealer WHERE id = %s", (dealer_id,)
    ).fetchone() if dealer_id else conn.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    if not dealer:
        raise HTTPException(404, "no dealership found: seed the demo data first")
    return {
        "dealer": dealer,
        "funnel": funnel.funnel(conn, dealer["id"], *period_b, sources=["ring_live"] if scope == "live" else None),
        "scope": scope,
        "comparison": funnel.compare(conn, dealer["id"], period_a, period_b),
        "patterns": [p for p in funnel.patterns(conn, period_a, period_b) if p["dealer"] == dealer["name"]],
        "note": "Anonymous operational funnel: stages compare aggregate activity and never follow an individual.",
    }


@app.get("/api/network")
def network(conn=Depends(get_conn)):
    """Manufacturer view: aggregates per dealership and region. No individual events."""
    period_a, period_b = funnel.periods()
    return funnel.network(conn, period_a, period_b) | {"patterns": funnel.patterns(conn, period_a, period_b)}


@app.get("/api/finance")
def finance(conn=Depends(get_conn)):
    """Captive-finance view: equal periods before and during the simulated financing promotion."""
    comparison = funnel.promotion_comparison(conn)
    if not comparison:
        raise HTTPException(404, "no promotion found: seed the demo data first")
    return comparison


@app.get("/api/patterns")
def patterns(conn=Depends(get_conn)):
    period_a, period_b = funnel.periods()
    return funnel.patterns(conn, period_a, period_b)


@app.get("/api/business/{kind}")
def business_records(kind: str, dealer_id: int | None = None, conn=Depends(get_conn)):
    """Simulated sales and finance records behind the last two funnel stages."""
    if kind not in ("sales", "finance_deals"):
        raise HTTPException(404, f"unknown record type: {kind}")
    if kind == "sales":
        return conn.execute(
            """SELECT s.occurred_at, s.model_group, s.source, d.name AS dealer,
                      (f.id IS NOT NULL) AS financed
               FROM sale s JOIN dealer d ON d.id = s.dealer_id
               LEFT JOIN finance_deal f ON f.sale_id = s.id
               WHERE (%s::int IS NULL OR s.dealer_id = %s)
               ORDER BY s.occurred_at DESC LIMIT 15""", (dealer_id, dealer_id)).fetchall()
    return conn.execute(
        """SELECT f.occurred_at, f.source, d.name AS dealer, s.model_group, p.name AS promotion
           FROM finance_deal f JOIN dealer d ON d.id = f.dealer_id JOIN sale s ON s.id = f.sale_id
           LEFT JOIN promotion p ON p.id = f.promotion_id
           WHERE (%s::int IS NULL OR f.dealer_id = %s)
           ORDER BY f.occurred_at DESC LIMIT 15""", (dealer_id, dealer_id)).fetchall()


@app.get("/api/evidence/{kind}")
def evidence(kind: str, conn=Depends(get_conn)):
    if kind not in metrics.LABELS:
        raise HTTPException(404, f"unknown metric: {kind}")
    return metrics.evidence(conn, kind)


app.mount("/", StaticFiles(directory=config.ROOT / "frontend", html=True), name="frontend")
