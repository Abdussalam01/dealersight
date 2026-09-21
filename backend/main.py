"""DealerSight API. Run: uvicorn backend.main:app --reload"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import config, correlation, db, ingest, metrics
from backend.ring_poller import RingPoller

logging.basicConfig(level=logging.INFO)
poller = RingPoller()


@asynccontextmanager
async def lifespan(app):
    with db.connect() as conn:
        db.init_schema(conn)
        ingest.current_session(conn)
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
    """Start counting from zero: events that started before now are never counted."""
    profile = ingest.current_session(conn)["timing_profile"]
    session = conn.execute(
        "INSERT INTO demo_session (watermark, timing_profile) VALUES (now(), %s) RETURNING *", (profile,)
    ).fetchone()
    correlation.rebuild(conn)
    return session


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
def hourly(conn=Depends(get_conn)):
    return metrics.hourly_visits(conn)


@app.get("/api/evidence/{kind}")
def evidence(kind: str, conn=Depends(get_conn)):
    if kind not in metrics.LABELS:
        raise HTTPException(404, f"unknown metric: {kind}")
    return metrics.evidence(conn, kind)


app.mount("/", StaticFiles(directory=config.ROOT / "frontend", html=True), name="frontend")
