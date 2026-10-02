"""Regressions for the September 29 code review (CR-xx findings)."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend import analyst, correlation, db, funnel, ingest, metrics, packets, seed
from tests.conftest import TEST_URL, history_event

SEED_DAY = date(2026, 9, 23)
NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def seeded():
    with db.connect(TEST_URL) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.init_schema(conn)
        seed.seed_all(conn, today=SEED_DAY)
        correlation.rebuild(conn)
        yield conn


def dealer_named(conn, name):
    return conn.execute("SELECT * FROM dealer WHERE name LIKE %s", (f"{name}%",)).fetchone()


# --- CR-01: a discovered camera must belong to the demo dealership ------------------

def test_live_event_lands_on_the_demo_dealer_on_a_fresh_database(seeded):
    """Startup order: seed, discover the camera, arm, ingest. The demo dealer's total must rise by one."""
    demo = seeded.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    ingest.start_session(seeded, now=NOW - timedelta(hours=1))   # live events count from here
    device = ingest.ensure_device(seeded, "ava1.ring.device.LIVE1", "Playground Device", mode="demo")
    assert seeded.execute("SELECT dealer_id FROM device WHERE id = %s", (device,)).fetchone()["dealer_id"] == demo["id"]

    period = (NOW - timedelta(minutes=30), NOW + timedelta(minutes=30))
    before_dealer = funnel.funnel(seeded, demo["id"], *period)["counts"]["visits"]
    before_network = funnel.funnel(seeded, None, *period)["counts"]["visits"]

    ingest.arm(seeded, device, "entrance", now=NOW - timedelta(minutes=2))
    ingest.ingest(seeded, history_event("live-1", NOW - timedelta(minutes=1), device="ava1.ring.device.LIVE1"),
                  now=NOW)
    correlation.rebuild(seeded, now=NOW)

    assert funnel.funnel(seeded, demo["id"], *period)["counts"]["visits"] == before_dealer + 1
    assert funnel.funnel(seeded, None, *period)["counts"]["visits"] == before_network + 1
    # network still reconciles to the sum of its dealerships
    per_dealer = sum(funnel.funnel(seeded, d["id"], *period)["counts"]["visits"] for d in funnel.dealers(seeded))
    assert per_dealer == funnel.funnel(seeded, None, *period)["counts"]["visits"]


# --- CR-02: evidence must explain the number that was clicked ----------------------

def test_evidence_is_filtered_to_the_selected_dealership_and_period(seeded):
    dealer = dealer_named(seeded, "Larkspur")
    _, period = funnel.periods(NOW)
    shown = funnel.funnel(seeded, dealer["id"], *period)["counts"]["visits"]

    result = metrics.evidence(seeded, "visit", limit=500, dealer_id=dealer["id"], start=period[0], end=period[1])

    assert result["matching_rows"] == shown
    assert {row["dealer"] for row in result["rows"]} == {dealer["name"]}
    assert all(period[0] <= row["started_at"] < period[1] for row in result["rows"])


def test_live_only_evidence_contains_only_live_events(seeded):
    dealer = seeded.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    _, period = funnel.periods(NOW)

    result = metrics.evidence(seeded, "visit", dealer_id=dealer["id"], start=period[0], end=period[1],
                              sources=["ring_live"])
    live_total = funnel.funnel(seeded, dealer["id"], *period, sources=["ring_live"])["counts"]["visits"]

    assert result["matching_rows"] == live_total
    assert all(row["source"] == "ring_live" for row in result["rows"])


# --- CR-03: packets must describe where the events actually came from --------------

def test_packet_calls_seeded_history_simulated_not_ring(seeded):
    dealer = dealer_named(seeded, "Brookfield")
    packet = packets.build(seeded, "q1_conversion_decline", dealer["id"], now=NOW)

    visits_origin = packet["event_origins"]["period_b"]["visits"]
    assert "simulated baseline" in visits_origin.lower()
    assert "not ring-originated" in visits_origin.lower()   # never presented as a Ring-originated event
    # the method is still "inferred by rules", which is separate from where the events came from
    assert "inferred" in packet["data_sources"]["visits"]
    assert "simulated business record" in packet["data_sources"]["sales"]


def test_packet_origin_shows_the_mix_when_live_events_are_present(seeded):
    demo = seeded.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    packet = packets.build(seeded, "q1_conversion_decline", demo["id"], now=NOW)

    origins = packet["event_origins"]["period_b"]["visits"].lower()
    assert "simulated baseline" in origins
    if "live ring" in origins:                 # live events exist in this database
        assert "ring-originated" in origins


# --- CR-06: a network question must not be narrowed by another tab's selection -----

def test_promotion_question_is_network_wide_whatever_dealer_is_selected(seeded):
    network_sales = funnel.promotion_comparison(seeded)["during"]["sales"]
    cedar = seeded.execute("SELECT * FROM dealer WHERE is_demo").fetchone()

    packet = packets.build(seeded, "q3_promotion_performance", cedar["id"], now=NOW)

    assert packet["metrics"]["sales.during"]["value"] == network_sales
    assert "network" in packet["scope"].lower()


def test_the_analyst_states_its_scope_for_the_promotion_question(seeded):
    packet = packets.build(seeded, "q3_promotion_performance", now=NOW)
    text = analyst.as_text(analyst.fallback_answer(packet))

    assert packet["scope"]
    assert text


# --- CR-05: AWS setup failures must fall back, not escape --------------------------

def test_a_bad_aws_profile_falls_back_and_is_logged(seeded, monkeypatch):
    from botocore.exceptions import ProfileNotFound
    monkeypatch.setattr(analyst, "bedrock_client",
                        lambda: (_ for _ in ()).throw(ProfileNotFound(profile="missing")))

    result = analyst.ask(seeded, "q1_conversion_decline", now=NOW, model_id="test-model")

    assert result["answer_source"] == "fallback_unavailable"
    assert "ProfileNotFound" in result["error"]
    assert result["answer"]["claims"]          # a usable deterministic answer, not a crash
    logged = seeded.execute("SELECT error, answer_source FROM analyst_log ORDER BY id DESC LIMIT 1").fetchone()
    assert logged["answer_source"] == "fallback_unavailable" and "ProfileNotFound" in logged["error"]


# --- CR-04: the prose must agree with the validated claims -------------------------

def _answer(summary, claims, investigate=("lead follow-up time",)):
    return {"summary": summary, "claims": list(claims), "investigate": list(investigate)}


def _claim(packet, metric_id):
    metric = packet["metrics"][metric_id]
    return {"metric_id": metric_id, "value": metric["value"], "unit": metric["unit"],
            "direction": metric["direction"]}


def test_summary_that_contradicts_its_claim_is_rejected(seeded):
    dealer = dealer_named(seeded, "Brookfield")
    packet = packets.build(seeded, "q1_conversion_decline", dealer["id"], now=NOW)
    assert packet["metrics"]["sales.change_pct"]["direction"] == "decrease"

    wrong = _answer("Sales increased while probable test drives held steady. These metrics do not prove a cause.",
                    [_claim(packet, "sales.change_pct")])
    right = _answer("Sales fell while probable test drives held steady. These metrics do not prove a cause.",
                    [_claim(packet, "sales.change_pct")])

    assert any("opposite" in reason for reason in analyst.validate(json.dumps(wrong), packet)[1])
    assert analyst.validate(json.dumps(right), packet)[1] == []


def test_invented_numbers_in_investigation_text_are_rejected(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", now=NOW)
    bad = _answer("Sales conversion held steady. These metrics do not prove a cause.",
                  [_claim(packet, "sales.change_pct")],
                  investigate=["Check why exactly 999 customers left"])

    reasons = analyst.validate(json.dumps(bad), packet)[1]

    assert any("not in the packet: 999" in reason for reason in reasons)


# --- CR-11: missing denominators must not become numbers --------------------------

def test_question_one_abstains_when_either_period_has_no_sessions(seeded):
    dealer = dealer_named(seeded, "Vantage")
    _, period = funnel.periods(NOW)
    seeded.execute("""DELETE FROM derived_event WHERE dealer_id = %s AND type = 'probable_test_drive'
                      AND started_at >= %s""", (dealer["id"], period[0]))
    try:
        packet = packets.build(seeded, "q1_conversion_decline", dealer["id"], now=NOW)
        assert "recent period" in packet["insufficient_data"]
    finally:
        correlation.rebuild(seeded)


def test_promotion_change_is_undefined_when_a_period_has_no_sales(seeded):
    promotion = seeded.execute("SELECT * FROM promotion ORDER BY starts_at DESC LIMIT 1").fetchone()
    seeded.execute("DELETE FROM sale WHERE occurred_at >= %s AND occurred_at < %s",
                   (promotion["starts_at"], promotion["ends_at"]))
    try:
        comparison = funnel.promotion_comparison(seeded)
        assert comparison["during"]["rates"]["finance_penetration"] is None
        assert comparison["finance_penetration_change_pts"] is None       # not a fabricated -59.4
        assert comparison["finance_penetration_unavailable_reason"]
        assert packets.build(seeded, "q3_promotion_performance", now=NOW)["insufficient_data"]
    finally:
        seed.seed_all(seeded, today=SEED_DAY)
        correlation.rebuild(seeded)


# --- CR-13: the stated deadline is enforced ---------------------------------------

def test_a_late_answer_is_rejected_by_the_deadline(seeded, monkeypatch):
    import time

    clock = {"now": 1000.0}
    monkeypatch.setattr(analyst.time, "monotonic", lambda: clock["now"])

    class SlowBedrock:
        def converse(self, **kwargs):
            clock["now"] += analyst.DEADLINE_SECONDS + 1     # answer arrives after the budget
            return {"output": {"message": {"content": [{"text": "{}"}]}}, "stopReason": "end_turn"}

    result = analyst.ask(seeded, "q1_conversion_decline", client=SlowBedrock(), now=NOW, model_id="test-model")

    assert result["answer_source"] == "fallback_unavailable"
    assert "deadline" in result["error"].lower()


def test_the_sdk_does_not_add_its_own_retries(seeded):
    client = analyst.bedrock_client()
    assert client.meta.config.retries["total_max_attempts"] == 1


# --- CR-12: compared periods must cover equal elapsed time ------------------------

def test_comparison_periods_have_equal_coverage(seeded):
    period_a, period_b = funnel.periods(NOW)
    assert funnel.coverage_hours(period_a) == funnel.coverage_hours(period_b)
    assert period_a[1] == period_b[0]

    comparison = funnel.compare(seeded, dealer_named(seeded, "Cedar")["id"], period_a, period_b)
    assert comparison["equal_coverage"] is True
    assert comparison["period_b"]["per_day"]["visits"] > 0


def test_demo_data_is_refreshed_when_it_ages(seeded):
    """ensure_fresh regenerates without needing a server restart."""
    assert seed.ensure_fresh(seeded, today=SEED_DAY) is False
    assert seed.ensure_fresh(seeded, today=SEED_DAY + timedelta(days=4)) is True
    seed.seed_all(seeded, today=SEED_DAY)
    correlation.rebuild(seeded)


# --- CR-08: a replayed departure must not pair with a live return -----------------

def test_replay_and_live_lot_events_do_not_pair(seeded):
    """CR-08: provenance is part of the matching key, so sources cannot be mixed."""
    demo = seeded.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    device = ingest.ensure_device(seeded, "ava1.ring.device.MIX", "Mixed camera", mode="demo")
    seeded.execute("UPDATE device SET dealer_id = %s WHERE id = %s", (demo["id"], device))
    ingest.start_session(seeded, now=NOW - timedelta(hours=1))

    ingest.arm(seeded, device, "lot_departure", now=NOW - timedelta(minutes=10), minutes=5)
    ingest.ingest(seeded, history_event("mix-dep", NOW - timedelta(minutes=9), device="ava1.ring.device.MIX"),
                  source="ring_replay", now=NOW)
    ingest.arm(seeded, device, "lot_return", now=NOW - timedelta(minutes=6), minutes=5)
    ingest.ingest(seeded, history_event("mix-ret", NOW - timedelta(minutes=5), device="ava1.ring.device.MIX"),
                  source="ring_live", now=NOW)
    correlation.rebuild(seeded, now=NOW)

    sessions = seeded.execute(
        """SELECT source FROM derived_event WHERE type = 'probable_test_drive' AND dealer_id = %s
           AND started_at >= %s""", (demo["id"], NOW - timedelta(hours=1))).fetchall()
    try:
        assert sessions == []          # different provenance: no pairing, and nothing labelled live
    finally:
        seed.seed_all(seeded, today=SEED_DAY)   # clears derived rows that reference these events
        seeded.execute("DELETE FROM raw_event WHERE ring_event_id IN ('mix-dep', 'mix-ret')")
        correlation.rebuild(seeded)


# --- CR-10: only real camera activity may become a metric -------------------------

def test_unexpected_event_types_are_stored_but_not_counted(seeded):
    """CR-10: a doorbell press or an invented type must not become a visit."""
    demo = seeded.execute("SELECT * FROM dealer WHERE is_demo").fetchone()
    device = ingest.ensure_device(seeded, "ava1.ring.device.TYPES", "Type test camera", mode="production")
    seeded.execute("UPDATE device SET dealer_id = %s WHERE id = %s", (demo["id"], device))
    seeded.execute("""INSERT INTO device_assignment (device_id, camera_position_id, valid_from, zone_source)
                      SELECT %s, id, %s, 'device_configuration' FROM camera_position WHERE code = 'entrance'""",
                   (device, NOW - timedelta(days=1)))
    ingest.start_session(seeded, now=NOW - timedelta(hours=1))

    for index, event_type in enumerate(("ding", "sensor_opened", "button_press")):
        payload = history_event(f"type-{index}", NOW - timedelta(minutes=30 - index),
                                device="ava1.ring.device.TYPES")
        payload["attributes"]["event_type"] = event_type
        assert ingest.ingest(seeded, payload, now=NOW) == {"status": "rejected", "reason": "event_type_not_counted"}

    motion = history_event("type-motion", NOW - timedelta(minutes=20), device="ava1.ring.device.TYPES")
    motion["attributes"]["event_type"] = "motion"
    assert ingest.ingest(seeded, motion, now=NOW)["status"] == "accepted"

    seed.seed_all(seeded, today=SEED_DAY)
    seeded.execute("DELETE FROM raw_event WHERE ring_event_id LIKE 'type-%%'")
    correlation.rebuild(seeded)


def test_an_event_that_ends_before_it_starts_is_rejected(seeded):
    payload = history_event("backwards", NOW - timedelta(minutes=5))
    payload["attributes"]["end"] = payload["attributes"]["start"] - 60_000

    result = ingest.ingest(seeded, payload, now=NOW)

    assert result == {"status": "invalid", "reason": "event ends before it starts"}


# --- CR-09: a stale rebuild must not overwrite a newer one ------------------------

def test_rebuild_reads_and_writes_under_one_lock(seeded):
    """The snapshot is read inside the locked transaction, so a slow rebuild cannot
    commit an older result on top of a newer one."""
    import inspect
    source = inspect.getsource(correlation.rebuild)

    assert "pg_advisory_xact_lock" in source
    assert source.index("pg_advisory_xact_lock") < source.index("_rebuild_locked")
    assert "pg_advisory_xact_lock" in inspect.getsource(seed.seed_all)   # reseed shares the lock
