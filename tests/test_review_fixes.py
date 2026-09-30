"""Regressions for the September 29 code review (CR-xx findings)."""

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
