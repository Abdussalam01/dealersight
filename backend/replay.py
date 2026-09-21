"""Deterministic replay of Ring-format events through the normal ingest path.

Used for tests and rehearsal only. Replayed events are stored with
source = 'ring_replay' on a separate "Replay camera" device and are never
presented as live Ring events. Event payloads follow the shape recorded from
the Ring Developer Playground (see docs/ring-sample-payloads/example_history_event.json).
"""

from datetime import timedelta

from backend import correlation, ingest

REPLAY_DEVICE = "replay.device.scenario"
REPLAY_DEVICE_NAME = "Replay camera (not live)"

# (minute offset, camera position, duration in seconds)
SCENARIO_A = {
    "description": "10 visits (plus 1 repeat inside the cooldown), 6 display events -> 3 engagements, "
                   "3 departure/return pairs, 1 unmatched departure that expires",
    "timing_profile": "production",
    "steps": [
        (5, "entrance", 20), (12, "entrance", 25), (12.17, "entrance", 15), (18, "entrance", 30),
        (25, "entrance", 20), (31, "entrance", 22), (40, "entrance", 18), (47, "entrance", 26),
        (55, "entrance", 21), (63, "entrance", 19), (70, "entrance", 24),
        (8, "display_area", 35), (15, "display_area", 12), (22, "display_area", 28),
        (35, "display_area", 18), (50, "display_area", 40), (66, "display_area", 10),
        (20, "lot_departure", 20), (26, "lot_departure", 20), (45, "lot_departure", 20), (60, "lot_departure", 20),
        (48, "lot_return", 20), (58, "lot_return", 20), (85, "lot_return", 20),
    ],
    "expected": {"visit": 10, "engagement": 3, "probable_test_drive": 3, "expired_departures": 1},
}

SCENARIOS = {"scenario_a": SCENARIO_A}


def history_event(event_id, device, start, seconds):
    start_ms = int(start.timestamp() * 1000)
    return {
        "type": "history-events",
        "id": event_id,
        "attributes": {"event_type": "on_demand", "start": start_ms, "end": start_ms + seconds * 1000,
                       "is_third_party_reviewed": True},
        "relationships": {"source": {"data": {"type": "devices", "id": device}}, "cv_detections": {"data": []}},
        "meta": {"riid": None},
    }


def replay(conn, scenario, base, run_id, now=None):
    """Start a session at `base`, arm and ingest each step in time order, then rebuild."""
    conn.execute(
        "INSERT INTO demo_session (watermark, timing_profile) VALUES (%s, %s)", (base, scenario["timing_profile"])
    )
    device_id = ingest.ensure_device(conn, REPLAY_DEVICE, REPLAY_DEVICE_NAME, mode="demo")
    results = []
    for index, (offset, position, seconds) in enumerate(sorted(scenario["steps"])):
        start = base + timedelta(minutes=offset)
        event = history_event(f"replay-{run_id}-{index:03d}", REPLAY_DEVICE, start, seconds)
        if not ingest.is_known_event(conn, event["id"]):
            ingest.arm(conn, device_id, position, now=start - timedelta(seconds=5), minutes=1)
        results.append(ingest.ingest(conn, event, source="ring_replay", now=start + timedelta(minutes=1)))
    ingest.disarm(conn, device_id, now=base + timedelta(hours=2))
    return results, correlation.rebuild(conn, now=now)
