"""Funnel, stakeholder views, and seeded data (Phase 3)."""

from datetime import date, datetime, timezone

import pytest

from backend import correlation, db, funnel, metrics, seed
from tests.conftest import TEST_URL

SEED_DAY = date(2026, 9, 23)
STAGE_KEYS = ["visits", "engagements", "probable_test_drives", "sales", "finance_deals"]


@pytest.fixture(scope="module")
def seeded():
    """Seed the fictional dealership data once for this module (the generator takes a few seconds)."""
    with db.connect(TEST_URL) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.init_schema(conn)
        seed.seed_all(conn, today=SEED_DAY)
        correlation.rebuild(conn)
        yield conn


@pytest.fixture(scope="module")
def spans():
    return funnel.periods(now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))


# --- rate handling -----------------------------------------------------------

def test_zero_denominator_gives_none_not_zero_percent():
    assert funnel.rate(0, 0) is None
    assert funnel.rate(5, 0) is None
    assert funnel.rate(1, 4) == 25.0
    assert funnel.change_pct(0, 7) is None


# --- funnel ------------------------------------------------------------------

def test_funnel_is_monotonic_and_labels_every_stage(seeded, spans):
    result = funnel.funnel(seeded, None, *spans[1])
    totals = [stage["total"] for stage in result["stages"]]

    assert totals == sorted(totals, reverse=True)
    assert [stage["key"] for stage in result["stages"]] == STAGE_KEYS
    for stage in result["stages"][:3]:
        assert set(stage["provenance"]) <= {"Simulated baseline", "Live Ring Playground", "Ring replay"}
        assert stage["source_type"] == "ring"
    for stage in result["stages"][3:]:
        assert stage["provenance"] == {"Simulated business data": stage["total"]}
        assert stage["source_type"] == "simulated"


def test_stage_provenance_totals_match_the_stage_total(seeded, spans):
    for stage in funnel.funnel(seeded, None, *spans[1])["stages"]:
        assert sum(stage["provenance"].values()) == stage["total"]


def test_seeded_history_is_labelled_simulated_baseline(seeded):
    sources = {row["source"] for row in seeded.execute("SELECT DISTINCT source FROM derived_event").fetchall()}
    assert sources == {"simulated_baseline"}


def test_live_scope_excludes_business_records_without_showing_zero_rates(seeded, spans):
    result = funnel.funnel(seeded, None, *spans[1], sources=["ring_live"])

    assert result["business_records_included"] is False
    assert result["counts"]["sales"] == 0
    assert result["rates"]["sales_conversion"] is None        # unavailable, not 0%
    assert result["rates"]["finance_penetration"] is None
    assert all(stage["provenance"] == {} for stage in result["stages"][3:])


# --- simulated business records ------------------------------------------------

def test_every_finance_deal_references_a_real_simulated_sale(seeded):
    orphans = seeded.execute(
        "SELECT count(*) AS n FROM finance_deal f LEFT JOIN sale s ON s.id = f.sale_id WHERE s.id IS NULL"
    ).fetchone()["n"]
    same_dealer = seeded.execute(
        "SELECT count(*) AS n FROM finance_deal f JOIN sale s ON s.id = f.sale_id WHERE s.dealer_id <> f.dealer_id"
    ).fetchone()["n"]

    assert orphans == 0 and same_dealer == 0


def test_sales_and_finance_records_are_always_simulated(seeded):
    for table in ("sale", "finance_deal"):
        sources = {row["source"] for row in seeded.execute(f"SELECT DISTINCT source FROM {table}").fetchall()}
        assert sources == {"simulated"}


# --- stakeholder views ----------------------------------------------------------

def test_views_reconcile_dealers_to_regions_to_network(seeded, spans):
    view = funnel.network(seeded, *spans)

    for key in STAGE_KEYS:
        dealer_total = sum(row["period_b"][key] for row in view["dealers"])
        region_total = sum(region["period_b"][key] for region in view["regions"])
        assert dealer_total == region_total == view["network"]["period_b"][key]
    assert sum(region["dealers"] for region in view["regions"]) == len(view["dealers"])


def test_manufacturer_view_returns_aggregates_only(seeded, spans):
    view = funnel.network(seeded, *spans)

    assert set(view) == {"dealers", "regions", "network", "period_a", "period_b"}
    assert all("ring_events" not in row and "source_event_ids" not in row for row in view["dealers"])


def test_promotion_comparison_uses_equal_durations(seeded):
    comparison = funnel.promotion_comparison(seeded)

    before = comparison["before"]["end"] - comparison["before"]["start"]
    during = comparison["during"]["end"] - comparison["during"]["start"]
    assert before == during
    assert comparison["before"]["end"] == comparison["during"]["start"]
    assert comparison["equal_durations_days"] == before.days
    assert set(comparison["before"]["per_day"]) == set(STAGE_KEYS)
    assert "does not prove" in comparison["note"]


def test_promotion_shows_higher_finance_penetration(seeded):
    comparison = funnel.promotion_comparison(seeded)

    assert comparison["during"]["rates"]["finance_penetration"] > comparison["before"]["rates"]["finance_penetration"]
    assert comparison["finance_penetration_change_pts"] > 0


def test_no_dealership_shows_every_sale_financed(seeded, spans):
    for row in funnel.network(seeded, *spans)["dealers"]:
        for period in ("period_a", "period_b"):
            penetration = row[period]["rates"]["finance_penetration"]
            assert penetration is None or penetration <= 100 * seed.MAX_PENETRATION


def test_hourly_traffic_is_scoped_to_one_dealership_and_period(seeded, spans):
    dealer = funnel.dealers(seeded)[0]
    rows = metrics.hourly_visits(seeded, dealer["id"], *spans[1])

    assert len(rows) <= 24
    assert all(0 <= row["hour"] <= 23 for row in rows)
    assert sum(row["visits"] for row in rows) == funnel.funnel(seeded, dealer["id"], *spans[1])["counts"]["visits"]


# --- pattern detection -----------------------------------------------------------

def test_patterns_are_detected_from_the_numbers(seeded, spans):
    found = {p["pattern"]: p for p in funnel.patterns(seeded, *spans)}

    assert found["sales_fell_while_test_drives_held"]["dealer"] == "Brookfield Auto Group"
    assert found["test_drives_grew_without_sales"]["dealer"] == "Larkspur Motors"
    assert found["finance_penetration_shift"]["dealer"] == "Network"
    # the steady dealerships must not be flagged
    assert "Vantage Hills Auto" not in {p["dealer"] for p in found.values()}


def test_pattern_descriptions_state_what_changed_without_claiming_a_cause(seeded, spans):
    for pattern in funnel.patterns(seeded, *spans):
        assert not any(word in pattern["description"].lower() for word in ("because", "caused", "due to"))


# --- determinism ------------------------------------------------------------------

def test_reseeding_restores_the_same_numbers(seeded, spans):
    before = funnel.funnel(seeded, None, *spans[1])["counts"]

    seed.seed_all(seeded, today=SEED_DAY)
    correlation.rebuild(seeded)

    assert funnel.funnel(seeded, None, *spans[1])["counts"] == before
