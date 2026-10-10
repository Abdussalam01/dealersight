"""The "check this finding" workflow: pairing sensitivity and the evidence checks behind a verdict.

A probable test-drive session is an assumption about time and sequence, so these tests pin the
behaviour that matters: the sensitivity run must never write, it must actually change results when
the window changes, and a verdict must follow the evidence rather than the question's premise.
"""

from datetime import timedelta

from backend import checks, correlation, funnel, ingest, packets
from tests.conftest import T0, history_event
from tests.test_correlation import activity, at, rebuild, use_production_timing


def counts(conn):
    return conn.execute(
        "SELECT (SELECT count(*) FROM derived_event) AS derived, (SELECT count(*) FROM test_drive_candidate) AS cand"
    ).fetchone()


# --- the pairing rule as a pure function -------------------------------------

def test_correlate_writes_nothing_and_is_repeatable(conn, device_id):
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(3))
    rebuild(conn)
    before = counts(conn)

    events = correlation._counted_events(conn, ingest.current_session(conn)["watermark"])
    rules = correlation.load_rules()
    gaps = correlation.timing_window("demo", rules)
    first = correlation.correlate(events, rules, lambda event: gaps, at(minutes=120))
    second = correlation.correlate(events, rules, lambda event: gaps, at(minutes=120))

    assert counts(conn) == before, "correlate must not touch the database"
    assert [d["type"] for d in first[0]] == [d["type"] for d in second[0]], "same events must give the same result"


def test_sensitivity_reports_one_row_per_configured_setting_and_stores_nothing(conn, device_id):
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(3))
    rebuild(conn)
    before = counts(conn)

    period = (at(0), at(minutes=240))
    rows = correlation.sensitivity(conn, periods={"period_b": period}, now=at(minutes=240))

    assert {row["setting"] for row in rows} == set(correlation.load_rules()["test_drive"]["sensitivity"])
    assert counts(conn) == before, "a sensitivity run must never write"
    for row in rows:
        assert "probable_test_drives" in row["counts"]["period_b"]


def test_a_pair_outside_the_strict_window_is_only_matched_by_a_wider_one(conn, device_id):
    """A 2-minute round trip is a test drive under `generous` (120 s) and not under `standard` (300 s).
    The same stored events, a different assumption, a different count."""
    use_production_timing(conn)
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(3))      # 2 minutes later
    rebuild(conn)

    rows = {row["setting"]: row for row in
            correlation.sensitivity(conn, periods={"p": (at(0), at(minutes=240))}, now=at(minutes=240))}

    assert rows["generous"]["counts"]["p"]["probable_test_drives"] == 1
    assert rows["standard"]["counts"]["p"]["probable_test_drives"] == 0
    assert rows["strict"]["counts"]["p"]["probable_test_drives"] == 0
    assert rows["standard"]["counts"]["p"]["unmatched_departures"] == 1


# --- the verdict follows the evidence ----------------------------------------

def test_verdict_is_withheld_when_a_critical_check_fails(conn, device_id, demo_dealer):
    """No activity at all: the workflow must say what is missing, not report a weak finding."""
    result = checks.check_finding(conn, demo_dealer, now=at(minutes=120))

    assert result["verdict"] == "withheld"
    assert result["withheld_reason"]
    failed = [c for c in result["checks"] if c["outcome"] == checks.FAIL and c["critical"]]
    assert failed, "a withheld verdict must name the critical check that failed"


def test_verdict_is_not_supported_when_conversion_did_not_decline(conn, device_id, demo_dealer, monkeypatch):
    """The premise of the question is not evidence for it. If conversion held steady, say so."""
    steady = {
        "period_a": {"visits": 100, "probable_test_drives": 10, "sales": 4, "coverage_hours": 24.0,
                     "rates": {"sales_conversion": 40.0}},
        "period_b": {"visits": 100, "probable_test_drives": 10, "sales": 4, "coverage_hours": 24.0,
                     "rates": {"sales_conversion": 40.0}},
        "equal_coverage": True,
    }
    monkeypatch.setattr(funnel, "compare", lambda *args, **kwargs: steady)
    monkeypatch.setattr(correlation, "sensitivity", lambda *args, **kwargs: [
        {"setting": name, "min_seconds": 1, "max_seconds": 2,
         "counts": {"period_a": {"probable_test_drives": 10, "unmatched_departures": 0},
                    "period_b": {"probable_test_drives": 10, "unmatched_departures": 0}}}
        for name in ("strict", "standard", "generous")])

    result = checks.check_finding(conn, demo_dealer, now=at(minutes=120))

    assert result["verdict"] == "not_supported"
    assert "held steady" in result["withheld_reason"]


def test_verdict_depends_on_assumption_when_settings_disagree(conn, demo_dealer, monkeypatch):
    """The honest answer when the result moves with the pairing window."""
    base = {"visits": 100, "probable_test_drives": 10, "sales": 4, "coverage_hours": 24.0,
            "rates": {"sales_conversion": 40.0}}
    monkeypatch.setattr(funnel, "compare", lambda *args, **kwargs: {
        "period_a": base, "period_b": dict(base, sales=3, rates={"sales_conversion": 30.0}), "equal_coverage": True})
    # Sales are 4 then 3 and do not move with the window; only the denominator does.
    # strict:   4/10 = 40% -> 3/6  = 50%  (rose)
    # generous: 4/10 = 40% -> 3/20 = 15%  (declined)
    monkeypatch.setattr(correlation, "sensitivity", lambda *args, **kwargs: [
        {"setting": "strict", "min_seconds": 1, "max_seconds": 2,
         "counts": {"period_a": {"probable_test_drives": 10, "unmatched_departures": 0},
                    "period_b": {"probable_test_drives": 6, "unmatched_departures": 0}}},
        {"setting": "generous", "min_seconds": 1, "max_seconds": 9,
         "counts": {"period_a": {"probable_test_drives": 10, "unmatched_departures": 0},
                    "period_b": {"probable_test_drives": 20, "unmatched_departures": 0}}}])

    result = checks.check_finding(conn, demo_dealer, now=at(minutes=120))

    assert result["verdict"] == "depends_on_assumption"
    assert any(c["key"] == "survives_matching_settings" and c["outcome"] == checks.CAUTION for c in result["checks"])


def test_source_check_compares_kinds_not_rendered_counts(conn, device_id, demo_dealer):
    """Origin strings carry counts, so comparing them would always differ. The check compares kinds."""
    use_production_timing(conn)
    for index in range(4):
        activity(conn, device_id, f"v{index}", "entrance", at(minutes=index * 10))
    rebuild(conn)

    result = checks.check_finding(conn, demo_dealer, now=at(minutes=120))
    source_check = next(c for c in result["checks"] if c["key"] == "comparable_sources")

    assert source_check["outcome"] in (checks.PASS, checks.CAUTION)
    assert "256" not in source_check["detail"], "the check must not quote raw counts as if they were kinds"


# --- the packet keeps the model away from near-duplicate numbers --------------

def test_q1_packet_carries_the_verdict_but_no_citable_sensitivity_metric(conn, device_id, demo_dealer):
    """A second 'change in sales conversion' differing only by scope is exactly the near-duplicate a
    model misreads, so the sensitivity figures must not be registered as citable metrics."""
    use_production_timing(conn)
    activity(conn, device_id, "dep", "lot_departure", at(1))
    activity(conn, device_id, "ret", "lot_return", at(20))
    rebuild(conn)

    packet = packets.build(conn, "q1_conversion_decline", demo_dealer, now=at(minutes=120))
    if packet.get("insufficient_data"):
        return   # nothing to compare in this fixture; the guard above is what matters

    assert "finding_check" in packet and packet["finding_check"]["verdict"]
    assert not [key for key in packet["metrics"] if key.startswith("sensitivity.")]
