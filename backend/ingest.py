"""Ring event ingestion: validate, deduplicate, resolve camera position, store.

Every Ring event is stored exactly once (unique ring_event_id). An event only
counts toward metrics when `accepted` is true; rejected events stay as evidence.

Acceptance rules:
  * started before the demo-session watermark  -> rejected: before_watermark
  * production device + on_demand (live view)  -> rejected: live_view
  * no camera position assigned at start time  -> rejected: not_armed (demo) / unassigned (production)
  * otherwise                                  -> accepted
Malformed or future-dated events are not stored at all.
"""

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from backend import config


class InvalidEvent(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def _from_epoch_ms(value):
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def normalize_history_event(payload, now=None):
    """Map a Ring Event History record to DealerSight's camera-activity fields."""
    now = now or utcnow()
    try:
        attributes = payload["attributes"]
        normalized = {
            "ring_event_id": payload["id"],
            "ring_device_id": payload["relationships"]["source"]["data"]["id"],
            "ring_event_type": attributes["event_type"],
            "started_at": _from_epoch_ms(attributes["start"]),
            "ended_at": _from_epoch_ms(attributes["end"]) if attributes.get("end") else None,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidEvent(f"missing or malformed field: {exc}") from exc
    if not normalized["ring_event_id"] or not normalized["ring_event_type"]:
        raise InvalidEvent("empty event id or type")
    if normalized["started_at"] > now + timedelta(seconds=config.FUTURE_TOLERANCE_SECONDS):
        raise InvalidEvent("event starts in the future")
    return normalized


def ensure_device(conn, ring_device_id, display_name, mode=None):
    row = conn.execute(
        """INSERT INTO device (ring_device_id, display_name, mode) VALUES (%s, %s, %s)
           ON CONFLICT (ring_device_id) DO UPDATE SET display_name = EXCLUDED.display_name
           RETURNING id""",
        (ring_device_id, display_name, mode or config.DEVICE_MODE),
    ).fetchone()
    return row["id"]


def current_session(conn, now=None):
    """The latest demo session; created on first use with the watermark set to now."""
    row = conn.execute("SELECT * FROM demo_session ORDER BY id DESC LIMIT 1").fetchone()
    return row or start_session(conn, now)


def start_session(conn, now=None):
    return conn.execute(
        "INSERT INTO demo_session (watermark) VALUES (%s) RETURNING *", (now or utcnow(),)
    ).fetchone()


def arm(conn, device_id, position_code, now=None, minutes=None):
    """Arm the demo device for one camera position until now + minutes (default ARM_MINUTES)."""
    now = now or utcnow()
    position = conn.execute("SELECT id FROM camera_position WHERE code = %s", (position_code,)).fetchone()
    if not position:
        raise ValueError(f"unknown camera position: {position_code}")
    disarm(conn, device_id, now)
    return conn.execute(
        """INSERT INTO device_assignment (device_id, camera_position_id, valid_from, valid_to, zone_source)
           VALUES (%s, %s, %s, %s, 'demo_assignment') RETURNING *""",
        (device_id, position["id"], now, now + timedelta(minutes=minutes or config.ARM_MINUTES)),
    ).fetchone()


def disarm(conn, device_id, now=None):
    now = now or utcnow()
    conn.execute(
        """UPDATE device_assignment SET valid_to = %s
           WHERE device_id = %s AND zone_source = 'demo_assignment' AND (valid_to IS NULL OR valid_to > %s)""",
        (now, device_id, now),
    )


def assignment_at(conn, device_id, moment):
    return conn.execute(
        """SELECT a.*, p.code AS position_code, p.name AS position_name
           FROM device_assignment a JOIN camera_position p ON p.id = a.camera_position_id
           WHERE a.device_id = %s AND a.valid_from <= %s AND (a.valid_to IS NULL OR a.valid_to > %s)
           ORDER BY a.valid_from DESC LIMIT 1""",
        (device_id, moment, moment),
    ).fetchone()


def ingest(conn, payload, source="ring_live", now=None):
    """Store one Ring history event. Returns {"status": accepted|rejected|duplicate|invalid, ...}."""
    now = now or utcnow()
    try:
        event = normalize_history_event(payload, now)
    except InvalidEvent as exc:
        return {"status": "invalid", "reason": str(exc)}

    device = conn.execute(
        "SELECT id, mode FROM device WHERE ring_device_id = %s", (event["ring_device_id"],)
    ).fetchone()
    if not device:
        return {"status": "invalid", "reason": "unknown device"}

    session = current_session(conn, now)
    assignment = assignment_at(conn, device["id"], event["started_at"])
    reason = None
    if event["started_at"] < session["watermark"]:
        reason = "before_watermark"
    elif device["mode"] == "production" and event["ring_event_type"] == "on_demand":
        reason = "live_view"
    elif not assignment:
        reason = "not_armed" if device["mode"] == "demo" else "unassigned"

    row = conn.execute(
        """INSERT INTO raw_event (ring_event_id, device_id, ring_event_type, started_at, ended_at, received_at,
                                  camera_position_id, zone_source, accepted, reject_reason, source, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (ring_event_id) DO NOTHING RETURNING id""",
        (
            event["ring_event_id"], device["id"], event["ring_event_type"], event["started_at"], event["ended_at"], now,
            assignment["camera_position_id"] if assignment and not reason else None,
            assignment["zone_source"] if assignment and not reason else None,
            reason is None, reason, source, Jsonb(payload),
        ),
    ).fetchone()
    if not row:
        return {"status": "duplicate"}
    if reason:
        return {"status": "rejected", "reason": reason}
    return {"status": "accepted", "position": assignment["position_code"]}


def is_known_event(conn, ring_event_id):
    return conn.execute("SELECT 1 FROM raw_event WHERE ring_event_id = %s", (ring_event_id,)).fetchone() is not None


def visit_count(conn):
    """Accepted Entrance events since the current watermark (dedup cooldown arrives in Phase 2)."""
    session = current_session(conn)
    return conn.execute(
        """SELECT count(*) AS n FROM raw_event e JOIN camera_position p ON p.id = e.camera_position_id
           WHERE e.accepted AND p.code = 'entrance' AND e.started_at >= %s""",
        (session["watermark"],),
    ).fetchone()["n"]


def recent_events(conn, limit=20):
    return conn.execute(
        """SELECT right(e.ring_event_id, 8) AS event_ref, e.ring_event_type, e.started_at, e.ended_at,
                  e.received_at, e.accepted, e.reject_reason, e.source, e.zone_source,
                  d.display_name AS device_name, p.name AS position_name, z.name AS zone_name
           FROM raw_event e
           JOIN device d ON d.id = e.device_id
           LEFT JOIN camera_position p ON p.id = e.camera_position_id
           LEFT JOIN zone z ON z.id = p.zone_id
           ORDER BY e.started_at DESC LIMIT %s""",
        (limit,),
    ).fetchall()
