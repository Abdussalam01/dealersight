"""Turns accepted Ring camera activity into derived events, using config/rules.yaml.

`rebuild` recomputes everything from stored raw events, processed in order of
Ring start time. Arrival order, duplicate deliveries, and replays therefore
cannot change the result: the same raw events always give the same metrics.

  Zone 1 Entrance      -> visit (repeat activity within the cooldown is the same visit)
  Zone 2 Display Area  -> engagement signal (activity lasting >= min duration)
  Zone 3 Lot           -> probable test-drive session: a Return-lane event inside the
                          timing window closes the oldest open Departure-lane event (FIFO)

A departure and a return can't be tied to the same vehicle anonymously, so a
matched pair is a *probable* session inferred from time and sequence only.
"""

import secrets
from datetime import timedelta
from functools import lru_cache

import yaml

from backend import config, ingest


@lru_cache
def load_rules():
    return yaml.safe_load((config.ROOT / "config" / "rules.yaml").read_text())


def timing_window(profile, rules=None):
    window = (rules or load_rules())["test_drive"]["profiles"][profile]
    return timedelta(seconds=window["min_seconds"]), timedelta(seconds=window["max_seconds"])


def _counted_events(conn, watermark):
    return conn.execute(
        """SELECT e.id, e.device_id, e.started_at, e.ended_at, e.source, p.code AS position, p.zone_id
           FROM raw_event e JOIN camera_position p ON p.id = e.camera_position_id
           WHERE e.accepted AND e.started_at >= %s
           ORDER BY e.started_at, e.id""",
        (watermark,),
    ).fetchall()


def rebuild(conn, now=None, rules=None):
    """Recompute derived events and test-drive candidates. Returns counts by type."""
    now = now or ingest.utcnow()
    rules = rules or load_rules()
    version = rules["rule_version"]
    session = ingest.current_session(conn)
    cooldown = timedelta(seconds=rules["visit"]["dedup_cooldown_seconds"])
    min_engagement = timedelta(seconds=rules["engagement"]["min_duration_seconds"])
    min_gap, max_gap = timing_window(session["timing_profile"], rules)

    derived, candidates = [], []
    last_visit = {}  # device_id -> the visit repeat activity collapses into

    for event in _counted_events(conn, session["watermark"]):
        position = event["position"]

        if position == "entrance":
            visit = last_visit.get(event["device_id"])
            if visit and event["started_at"] - visit["started_at"] < cooldown:
                visit["source_event_ids"].append(event["id"])
                visit["ended_at"] = max(visit["ended_at"] or event["started_at"], event["ended_at"] or event["started_at"])
                continue
            visit = _derived("visit", event, [event["id"]], "inferred", version)
            last_visit[event["device_id"]] = visit
            derived.append(visit)

        elif position == "display_area":
            if event["ended_at"] and event["ended_at"] - event["started_at"] >= min_engagement:
                derived.append(_derived("engagement", event, [event["id"]], "signal", version))

        elif position == "lot_departure":
            candidates.append({
                "departed_event_id": event["id"], "departed_at": event["started_at"],
                "expires_at": event["started_at"] + max_gap, "status": "open",
                "matched_event_id": None, "zone_id": event["zone_id"], "source": event["source"],
            })

        elif position == "lot_return":
            open_candidates = [c for c in candidates if c["status"] == "open"]
            for candidate in open_candidates:
                if candidate["expires_at"] < event["started_at"]:
                    candidate["status"] = "expired"
            match = next((c for c in candidates if c["status"] == "open"
                          and event["started_at"] - c["departed_at"] >= min_gap), None)
            if match:  # otherwise: no open departure, or the return came too soon
                match.update(status="matched", matched_event_id=event["id"])
                session_event = _derived("probable_test_drive", event, [match["departed_event_id"], event["id"]], "probable", version)
                session_event["started_at"] = match["departed_at"]
                session_event["ended_at"] = event["started_at"]
                derived.append(session_event)

    for candidate in candidates:
        if candidate["status"] == "open" and candidate["expires_at"] < now:
            candidate["status"] = "expired"

    _store(conn, derived, candidates, version)
    return {kind: sum(1 for d in derived if d["type"] == kind) for kind in ("visit", "engagement", "probable_test_drive")}


def _derived(kind, event, source_ids, confidence, version):
    return {
        "type": kind, "zone_id": event["zone_id"], "started_at": event["started_at"], "ended_at": event["ended_at"],
        "confidence": confidence, "rule_version": version, "source_event_ids": source_ids, "source": event["source"],
    }


def _store(conn, derived, candidates, version):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(4210)")  # one rebuild at a time (poller vs. API)
        conn.execute("DELETE FROM derived_event")
        conn.execute("DELETE FROM test_drive_candidate")
        for d in derived:
            conn.execute(
                """INSERT INTO derived_event (type, zone_id, started_at, ended_at, confidence, rule_version, source_event_ids, source)
                   VALUES (%(type)s, %(zone_id)s, %(started_at)s, %(ended_at)s, %(confidence)s, %(rule_version)s,
                           %(source_event_ids)s, %(source)s)""",
                d,
            )
        for c in candidates:
            # Short-lived anonymous token only while a departure is open; cleared on match or expiry.
            token = secrets.token_hex(8) if c["status"] == "open" else None
            conn.execute(
                """INSERT INTO test_drive_candidate (anon_token, departed_event_id, departed_at, expires_at, status,
                                                    matched_event_id, rule_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (token, c["departed_event_id"], c["departed_at"], c["expires_at"], c["status"], c["matched_event_id"], version),
            )
