from datetime import timedelta

from backend import ingest
from tests.conftest import T0, history_event


def minutes(n):
    return timedelta(minutes=n)


# --- Phase 1 checkpoint tests ---------------------------------------------

def test_armed_entrance_event_counts_once(conn, device_id):
    ingest.arm(conn, device_id, "entrance", now=T0)
    assert ingest.visit_count(conn) == 0

    result = ingest.ingest(conn, history_event("evt-1", T0 + minutes(1)), now=T0 + minutes(2))

    assert result == {"status": "accepted", "position": "entrance"}
    assert ingest.visit_count(conn) == 1


def test_duplicate_event_does_not_change_count(conn, device_id):
    ingest.arm(conn, device_id, "entrance", now=T0)
    event = history_event("evt-1", T0 + minutes(1))
    ingest.ingest(conn, event, now=T0 + minutes(2))

    assert ingest.ingest(conn, event, now=T0 + minutes(3)) == {"status": "duplicate"}
    assert ingest.visit_count(conn) == 1
    assert conn.execute("SELECT count(*) AS n FROM raw_event").fetchone()["n"] == 1


def test_unarmed_event_is_stored_but_not_counted(conn, device_id):
    result = ingest.ingest(conn, history_event("evt-1", T0 + minutes(1)), now=T0 + minutes(2))

    assert result == {"status": "rejected", "reason": "not_armed"}
    stored = conn.execute("SELECT accepted, reject_reason FROM raw_event").fetchone()
    assert stored == {"accepted": False, "reject_reason": "not_armed"}
    assert ingest.visit_count(conn) == 0


def test_event_after_arm_expires_is_not_counted(conn, device_id):
    ingest.arm(conn, device_id, "entrance", now=T0, minutes=10)

    result = ingest.ingest(conn, history_event("evt-1", T0 + minutes(11)), now=T0 + minutes(12))

    assert result == {"status": "rejected", "reason": "not_armed"}
    assert ingest.visit_count(conn) == 0


# --- Supporting rules -------------------------------------------------------

def test_position_comes_from_event_start_time_not_arrival_time(conn, device_id):
    ingest.arm(conn, device_id, "entrance", now=T0)
    ingest.arm(conn, device_id, "display_area", now=T0 + minutes(2))

    # Started while Entrance was armed, but only polled after Display Area was armed.
    result = ingest.ingest(conn, history_event("evt-1", T0 + minutes(1)), now=T0 + minutes(3))

    assert result == {"status": "accepted", "position": "entrance"}


def test_disarm_stops_counting(conn, device_id):
    ingest.arm(conn, device_id, "entrance", now=T0)
    ingest.disarm(conn, device_id, now=T0 + minutes(1))

    result = ingest.ingest(conn, history_event("evt-1", T0 + minutes(2)), now=T0 + minutes(3))

    assert result == {"status": "rejected", "reason": "not_armed"}


def test_event_before_watermark_is_not_counted(conn, device_id):
    ingest.arm(conn, device_id, "entrance", now=T0 - minutes(30))

    result = ingest.ingest(conn, history_event("old", T0 - minutes(5)), now=T0)

    assert result == {"status": "rejected", "reason": "before_watermark"}


def test_production_mode_excludes_live_view_events(conn):
    device_id = ingest.ensure_device(conn, "ava1.ring.device.PROD", "Front Door", mode="production")
    conn.execute(
        """INSERT INTO device_assignment (device_id, camera_position_id, valid_from, zone_source)
           SELECT %s, id, %s, 'device_configuration' FROM camera_position WHERE code = 'entrance'""",
        (device_id, T0),
    )

    live_view = history_event("lv-1", T0 + minutes(1), device="ava1.ring.device.PROD")
    motion = history_event("m-1", T0 + minutes(2), event_type="motion", device="ava1.ring.device.PROD")

    assert ingest.ingest(conn, live_view, now=T0 + minutes(3)) == {"status": "rejected", "reason": "live_view"}
    assert ingest.ingest(conn, motion, now=T0 + minutes(3)) == {"status": "accepted", "position": "entrance"}


def test_missing_fields_are_rejected_and_not_stored(conn, device_id):
    broken = history_event("evt-1", T0 + minutes(1))
    del broken["attributes"]["start"]

    assert ingest.ingest(conn, broken, now=T0 + minutes(2))["status"] == "invalid"
    assert conn.execute("SELECT count(*) AS n FROM raw_event").fetchone()["n"] == 0


def test_future_event_is_rejected(conn, device_id):
    result = ingest.ingest(conn, history_event("evt-1", T0 + minutes(60)), now=T0)

    assert result == {"status": "invalid", "reason": "event starts in the future"}


def test_unknown_camera_position_cannot_be_armed(conn, device_id):
    try:
        ingest.arm(conn, device_id, "parking_lot", now=T0)
    except ValueError as exc:
        assert "unknown camera position" in str(exc)
    else:
        raise AssertionError("expected ValueError")
