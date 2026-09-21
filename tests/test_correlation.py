"""Zone rules from config/rules.yaml (Phase 2). Default test session uses the demo timing profile
(test drive window 30 s - 300 s) unless a test switches to production (300 s - 5400 s)."""

from datetime import timedelta

from backend import correlation, ingest, metrics
from backend.replay import SCENARIO_A, replay
from tests.conftest import T0, history_event


def at(minutes=0, seconds=0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def activity(conn, device_id, event_id, position, start, seconds=30):
    """Arm the position just before the event starts, then ingest it (as the live demo does)."""
    ingest.arm(conn, device_id, position, now=start - timedelta(seconds=5), minutes=1)
    return ingest.ingest(conn, history_event(event_id, start, seconds=seconds), now=start + timedelta(minutes=1))


def rebuild(conn, now=None):
    return correlation.rebuild(conn, now=now or at(minutes=120))


def use_production_timing(conn):
    conn.execute("UPDATE demo_session SET timing_profile = 'production'")


def candidate_rows(conn):
    return conn.execute("SELECT status, anon_token FROM test_drive_candidate ORDER BY departed_at").fetchall()


# --- Zone 1: visits ----------------------------------------------------------

def test_repeat_entrance_activity_inside_cooldown_is_one_visit(conn, device_id):
    activity(conn, device_id, "a", "entrance", at(1))
    activity(conn, device_id, "b", "entrance", at(1, seconds=10))   # inside 30 s cooldown
    activity(conn, device_id, "c", "entrance", at(1, seconds=45))   # outside cooldown

    assert rebuild(conn)["visit"] == 2
    first_visit = conn.execute("SELECT source_event_ids FROM derived_event ORDER BY started_at LIMIT 1").fetchone()
    assert len(first_visit["source_event_ids"]) == 2


def test_hourly_traffic_groups_visits_by_hour(conn, device_id):
    activity(conn, device_id, "a", "entrance", at(1))
    activity(conn, device_id, "b", "entrance", at(70))
    rebuild(conn)

    assert [row["visits"] for row in metrics.hourly_visits(conn)] == [1, 1]


# --- Zone 2: engagement --------------------------------------------------------

def test_engagement_threshold_boundary(conn, device_id):
    activity(conn, device_id, "short", "display_area", at(1), seconds=19)
    activity(conn, device_id, "exact", "display_area", at(3), seconds=20)

    counts = rebuild(conn)

    assert counts["engagement"] == 1
    assert metrics.evidence(conn, "engagement")[0]["ring_events"][0]["position"] == "Display Area"


# --- Zone 3: probable test-drive sessions --------------------------------------

def test_departure_and_return_in_window_is_one_probable_session(conn, device_id):
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(3))

    assert rebuild(conn)["probable_test_drive"] == 1
    session = metrics.evidence(conn, "probable_test_drive")[0]
    assert session["confidence"] == "probable"
    assert session["duration_minutes"] == 2.0
    assert [e["position"] for e in session["ring_events"]] == ["Lot: Departure lane", "Lot: Return lane"]


def test_departure_without_return_stays_open_then_expires(conn, device_id):
    activity(conn, device_id, "dep", "lot_departure", at(1))

    rebuild(conn, now=at(3))
    assert [r["status"] for r in candidate_rows(conn)] == ["open"]

    rebuild(conn, now=at(10))  # demo window max is 5 minutes
    assert [r["status"] for r in candidate_rows(conn)] == ["expired"]
    assert metrics.summary(conn)["probable_test_drive"] == 0


def test_return_too_soon_is_ignored(conn, device_id):
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(1, seconds=20))  # under 30 s minimum

    assert rebuild(conn, now=at(3))["probable_test_drive"] == 0


def test_return_after_window_does_not_match(conn, device_id):
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(8))  # past the 5 minute maximum

    assert rebuild(conn)["probable_test_drive"] == 0
    assert [r["status"] for r in candidate_rows(conn)] == ["expired"]


def test_return_without_departure_is_ignored(conn, device_id):
    activity(conn, device_id, "ret", "lot_return", at(2))

    assert rebuild(conn)["probable_test_drive"] == 0


def test_overlapping_departures_match_oldest_first(conn, device_id):
    activity(conn, device_id, "dep1", "lot_departure", at(1))
    activity(conn, device_id, "dep2", "lot_departure", at(2))
    activity(conn, device_id, "ret1", "lot_return", at(3))
    activity(conn, device_id, "ret2", "lot_return", at(4))

    assert rebuild(conn)["probable_test_drive"] == 2
    durations = sorted(s["duration_minutes"] for s in metrics.evidence(conn, "probable_test_drive"))
    assert durations == [2.0, 2.0]  # dep1->ret1 and dep2->ret2, not dep1->ret2


def test_out_of_order_arrival_gives_same_result(conn, device_id):
    # The return is polled before the departure; rebuild orders by Ring start time.
    ingest.arm(conn, device_id, "lot_return", now=at(2, seconds=55), minutes=1)
    ingest.ingest(conn, history_event("ret", at(3)), now=at(4))
    ingest.arm(conn, device_id, "lot_departure", now=at(0, seconds=55), minutes=1)
    ingest.ingest(conn, history_event("dep", at(1)), now=at(5))

    assert rebuild(conn)["probable_test_drive"] == 1


def test_anon_token_exists_only_while_departure_is_open(conn, device_id):
    activity(conn, device_id, "dep1", "lot_departure", at(1))
    activity(conn, device_id, "ret1", "lot_return", at(3))
    activity(conn, device_id, "dep2", "lot_departure", at(4))

    rebuild(conn, now=at(5))
    rows = candidate_rows(conn)
    assert rows[0] == {"status": "matched", "anon_token": None}
    assert rows[1]["status"] == "open" and rows[1]["anon_token"]


def test_timing_profiles_use_documented_windows(conn, device_id):
    assert correlation.timing_window("demo") == (timedelta(seconds=30), timedelta(minutes=5))
    assert correlation.timing_window("production") == (timedelta(minutes=5), timedelta(minutes=90))

    use_production_timing(conn)
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(3))  # fine for demo, too short for production

    assert rebuild(conn)["probable_test_drive"] == 0


# --- Replay ------------------------------------------------------------------

def test_scenario_a_produces_expected_counts(conn):
    base = T0 + timedelta(hours=1)
    _, counts = replay(conn, SCENARIO_A, base, run_id="t", now=base + timedelta(hours=3))

    expected = SCENARIO_A["expected"]
    assert counts == {k: expected[k] for k in ("visit", "engagement", "probable_test_drive")}
    expired = conn.execute("SELECT count(*) AS n FROM test_drive_candidate WHERE status = 'expired'").fetchone()["n"]
    assert expired == expected["expired_departures"]


def test_repeated_replay_gives_identical_metrics(conn):
    base = T0 + timedelta(hours=1)
    now = base + timedelta(hours=3)
    _, first = replay(conn, SCENARIO_A, base, run_id="t", now=now)
    results, second = replay(conn, SCENARIO_A, base, run_id="t", now=now)

    assert first == second
    assert all(r["status"] == "duplicate" for r in results)
