"""AI analyst: packets, claim validation, fallback and logging (Phase 4).

No test calls Amazon Bedrock. The client is a stub, so the suite is free and offline.
"""

import json
from datetime import date, datetime, timezone

import pytest

from backend import analyst, correlation, db, funnel, packets, seed
from tests.conftest import TEST_URL

SEED_DAY = date(2026, 9, 23)
NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


class StubBedrock:
    """Stands in for bedrock-runtime: returns a fixed body, or raises to simulate a failure."""

    def __init__(self, answer=None, error=None, stop_reason="end_turn", raw=None):
        self.raw = raw if raw is not None else json.dumps(answer)
        self.error, self.stop_reason, self.calls = error, stop_reason, 0

    def converse(self, **kwargs):
        self.calls += 1
        self.last_call = kwargs
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.raw}]}},
                "stopReason": self.stop_reason, "usage": {"inputTokens": 100, "outputTokens": 20}}


def answer(summary="Sales conversion fell while probable test drives held steady. These metrics do not prove a cause.",
           claims=(), investigate=("lead follow-up time",)):
    return {"summary": summary, "claims": list(claims), "investigate": list(investigate)}


def claim(packet, metric_id, **overrides):
    metric = packet["metrics"][metric_id]
    return {"metric_id": metric_id, "value": metric["value"], "unit": metric["unit"],
            "direction": metric["direction"]} | overrides


@pytest.fixture(scope="module")
def seeded():
    with db.connect(TEST_URL) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.init_schema(conn)
        seed.seed_all(conn, today=SEED_DAY)
        correlation.rebuild(conn)
        yield conn


@pytest.fixture(scope="session", autouse=True)
def empty_database():
    import psycopg
    from tests.conftest import ADMIN_URL
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = 'dealersight_test_empty'").fetchone():
            admin.execute("CREATE DATABASE dealersight_test_empty")


@pytest.fixture
def empty():
    with db.connect(TEST_URL + "_empty") as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.init_schema(conn)
        yield conn


def dealer_id(conn, name):
    return conn.execute("SELECT id FROM dealer WHERE name LIKE %s", (f"{name}%",)).fetchone()["id"]


def ask(conn, question_id, client, dealer=None):
    return analyst.ask(conn, question_id, dealer, client=client, now=NOW, model_id="test-model")


# --- packets ------------------------------------------------------------------

def test_every_required_question_builds_a_packet_with_addressable_metrics(seeded):
    assert set(packets.QUESTIONS) == {"q1_conversion_decline", "q2_test_drives_without_sales",
                                      "q3_promotion_performance"}
    for question_id in packets.QUESTIONS:
        packet = packets.build(seeded, question_id, now=NOW)
        assert packet["question"] and not packet.get("insufficient_data")
        assert packet["metrics"] and packet["packet_schema_version"]
        assert packet["data_sources"]["sales"].startswith("Simulated")
        assert "does not prove a cause" in packet["caveat"]
        for metric in packet["metrics"].values():
            assert metric["unit"] in packets.UNITS and metric["label"] and metric["scope"]


def test_q1_packet_matches_the_dashboard_numbers(seeded):
    dealer = dealer_id(seeded, "Brookfield")
    packet = packets.build(seeded, "q1_conversion_decline", dealer, now=NOW)
    comparison = funnel.compare(seeded, dealer, *funnel.periods(NOW))

    assert packet["metrics"]["sales.period_b"]["value"] == comparison["period_b"]["sales"]
    assert packet["metrics"]["sales.change_pct"]["value"] == comparison["change_pct"]["sales"]
    assert packet["metrics"]["sales_conversion.period_a"]["value"] == comparison["period_a"]["rates"]["sales_conversion"]


def test_q2_ranking_is_computed_in_code(seeded):
    packet = packets.build(seeded, "q2_test_drives_without_sales", now=NOW)
    gaps = [row["gap_points"] for row in packet["dealers"]]

    assert gaps == sorted(gaps, reverse=True)
    assert packet["dealers_matching_threshold"] == ["Larkspur Motors"]


def test_q3_packet_keeps_equal_periods_and_per_period_counts(seeded):
    packet = packets.build(seeded, "q3_promotion_performance", now=NOW)
    counts = packet["finance_penetration_pct"]["counts"]

    assert packet["equal_durations_days"] == 7
    for period in ("before", "during"):
        deals, sales = counts[period]["financed"], counts[period]["sales"]
        assert packet["metrics"][f"finance_deals.{period}"]["value"] == deals
        assert packet["metrics"][f"sales.{period}"]["value"] == sales
        # the displayed rate reproduces from this period's own counts
        assert round(100 * deals / sales, 1) == packet["finance_penetration_pct"][period]


# --- guard 1: insufficient data -----------------------------------------------

def test_missing_data_returns_insufficient_without_calling_bedrock(empty):
    client = StubBedrock(answer=answer())

    result = ask(empty, "q1_conversion_decline", client)

    assert result["answer_source"] == "insufficient_data"
    assert result["answer"]["summary"].startswith("Insufficient data")
    assert client.calls == 0
    assert empty.execute("SELECT answer_source FROM analyst_log").fetchone()["answer_source"] == "insufficient_data"


# --- guard 2: claims are checked against the packet ----------------------------

def test_valid_claims_are_accepted_and_rendered_from_the_packet(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", dealer_id(seeded, "Brookfield"), now=NOW)
    client = StubBedrock(answer=answer(claims=[claim(packet, "sales.change_pct")]))

    result = ask(seeded, "q1_conversion_decline", client, dealer_id(seeded, "Brookfield"))

    assert result["answer_source"] == "bedrock" and result["error"] is None
    stored = result["answer"]["claims"][0]
    assert stored["value"] == packet["metrics"]["sales.change_pct"]["value"]
    assert stored["label"] and stored["scope"]           # shown with the metric it belongs to


def test_right_number_attached_to_the_wrong_metric_is_rejected(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", dealer_id(seeded, "Brookfield"), now=NOW)
    sales_change = packet["metrics"]["sales.change_pct"]["value"]
    wrong = {"metric_id": "visits.change_pct", "value": sales_change, "unit": "percent_change", "direction": "decrease"}

    result = ask(seeded, "q1_conversion_decline", StubBedrock(answer=answer(claims=[wrong])),
                 dealer_id(seeded, "Brookfield"))

    assert result["answer_source"] == "fallback_rejected"
    assert "visits.change_pct" in result["error"]


def test_metric_from_another_dealership_is_rejected(seeded):
    """Q1 packets are dealership-scoped, so another dealership's metric id does not exist in them."""
    wrong = {"metric_id": "larkspur_motors.sales.change_pct", "value": 0.0, "unit": "percent_change",
             "direction": "flat"}

    result = ask(seeded, "q1_conversion_decline", StubBedrock(answer=answer(claims=[wrong])),
                 dealer_id(seeded, "Brookfield"))

    assert result["answer_source"] == "fallback_rejected"
    assert "not in the packet" in result["error"]


def test_metric_from_the_wrong_period_is_rejected(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", dealer_id(seeded, "Brookfield"), now=NOW)
    earlier = packet["metrics"]["sales.period_a"]["value"]
    recent = packet["metrics"]["sales.period_b"]["value"]
    assert earlier != recent
    wrong = {"metric_id": "sales.period_b", "value": earlier, "unit": "count", "direction": None}

    result = ask(seeded, "q1_conversion_decline", StubBedrock(answer=answer(claims=[wrong])),
                 dealer_id(seeded, "Brookfield"))

    assert result["answer_source"] == "fallback_rejected"
    assert "sales.period_b" in result["error"]


def test_right_magnitude_wrong_direction_is_rejected(seeded):
    dealer = dealer_id(seeded, "Brookfield")
    packet = packets.build(seeded, "q1_conversion_decline", dealer, now=NOW)
    wrong = claim(packet, "sales.change_pct", direction="increase")   # packet value is negative

    result = ask(seeded, "q1_conversion_decline", StubBedrock(answer=answer(claims=[wrong])), dealer)

    assert result["answer_source"] == "fallback_rejected"
    assert "direction" in result["error"]


def test_percent_cannot_stand_in_for_percentage_points(seeded):
    packet = packets.build(seeded, "q3_promotion_performance", now=NOW)
    wrong = claim(packet, "finance_penetration.change_points", unit="percent_change")

    result = ask(seeded, "q3_promotion_performance", StubBedrock(answer=answer(claims=[wrong])))

    assert result["answer_source"] == "fallback_rejected"
    assert "unit" in result["error"]


def test_a_number_that_only_appears_in_a_date_cannot_support_a_claim(seeded):
    """2026 appears in the packet's period strings but is not a metric value."""
    wrong = {"metric_id": "sales.change_pct", "value": 2026, "unit": "percent_change", "direction": "increase"}

    result = ask(seeded, "q1_conversion_decline", StubBedrock(answer=answer(claims=[wrong])))

    assert result["answer_source"] == "fallback_rejected"
    assert "claimed value 2026" in result["error"]


def test_numbers_in_the_summary_are_rejected(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", now=NOW)
    client = StubBedrock(answer=answer(summary="Sales conversion fell 12.8 points.",
                                       claims=[claim(packet, "sales.change_pct")]))

    result = ask(seeded, "q1_conversion_decline", client)

    assert result["answer_source"] == "fallback_rejected"
    assert "digits" in result["error"]


def test_causal_language_is_rejected(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", now=NOW)
    client = StubBedrock(answer=answer(summary="Sales fell because staffing dropped.",
                                       claims=[claim(packet, "sales.change_pct")]))

    result = ask(seeded, "q1_conversion_decline", client)

    assert result["answer_source"] == "fallback_rejected"
    assert "asserted a cause" in result["error"]


def test_answer_over_the_word_limit_falls_back(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", now=NOW)
    client = StubBedrock(answer=answer(summary=" ".join(["word"] * (analyst.MAX_WORDS + 5)),
                                       claims=[claim(packet, "sales.change_pct")]))

    result = ask(seeded, "q1_conversion_decline", client)

    assert result["answer_source"] == "fallback_rejected"
    assert "word limit" in result["error"]


def test_empty_or_malformed_model_output_falls_back(seeded):
    for raw in ("", "not json at all", "[1, 2, 3]", json.dumps({"summary": "fine", "claims": []})):
        result = ask(seeded, "q1_conversion_decline", StubBedrock(raw=raw))
        assert result["answer_source"] == "fallback_rejected", raw


def test_non_success_stop_reason_falls_back(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", now=NOW)
    client = StubBedrock(answer=answer(claims=[claim(packet, "sales.change_pct")]), stop_reason="max_tokens")

    result = ask(seeded, "q1_conversion_decline", client)

    assert result["answer_source"] == "fallback_unavailable"
    assert "max_tokens" in result["error"]


# --- guard 3: AWS failure ------------------------------------------------------

def test_bedrock_failure_falls_back_to_deterministic_text(seeded):
    result = ask(seeded, "q3_promotion_performance", StubBedrock(error=TimeoutError("read timeout")))

    assert result["answer_source"] == "fallback_unavailable"
    assert "TimeoutError" in result["error"]
    assert result["answer"]["claims"] and "does not prove" in analyst.as_text(result["answer"]).lower()


def test_fallback_is_deterministic_and_grounded(seeded):
    packet = packets.build(seeded, "q3_promotion_performance", now=NOW)
    first = analyst.fallback_answer(packet)

    assert first == analyst.fallback_answer(packet)
    for item in first["claims"]:
        assert item["value"] == packet["metrics"][item["metric_id"]]["value"]


def test_fallback_never_claims_a_cause(seeded):
    for question_id in packets.QUESTIONS:
        structured = analyst.fallback_answer(packets.build(seeded, question_id, now=NOW))
        text = analyst.as_text(structured).lower()
        assert "do not prove" in text or "does not prove" in text
        _, reasons = analyst.validate(json.dumps(structured), packets.build(seeded, question_id, now=NOW))
        assert not [reason for reason in reasons if "asserted a cause" in reason]


def test_a_disclaimer_mentioning_cause_is_allowed_but_an_assertion_is_not(seeded):
    packet = packets.build(seeded, "q3_promotion_performance", now=NOW)
    good = claim(packet, "sales.change_pct")

    disclaimer = answer(summary="Sales fell during the promotion. This does not prove the promotion caused it.",
                        claims=[good])
    assertion = answer(summary="The promotion caused sales to fall.", claims=[good])

    assert ask(seeded, "q3_promotion_performance", StubBedrock(answer=disclaimer))["answer_source"] == "bedrock"
    assert ask(seeded, "q3_promotion_performance", StubBedrock(answer=assertion))["answer_source"] == "fallback_rejected"


def test_errors_are_sanitized_before_they_are_stored(seeded):
    leaky = RuntimeError("AccessDenied for arn:aws:bedrock:us-east-1:123456789012:model/x by AKIAIOSFODNN7EXAMPLE "
                         "in account 210987654321 request 3c4484f3-4605-4bad-920d-4583cd2bef69")

    result = ask(seeded, "q1_conversion_decline", StubBedrock(error=leaky))

    stored = seeded.execute("SELECT error FROM analyst_log ORDER BY id DESC LIMIT 1").fetchone()["error"]
    for secret in ("123456789012", "210987654321", "AKIAIOSFODNN7EXAMPLE",
                   "3c4484f3-4605-4bad-920d-4583cd2bef69", "arn:aws"):
        assert secret not in stored and secret not in result["error"]
    assert "[arn]" in stored and "[access-key]" in stored and "[account-id]" in stored


# --- logging -------------------------------------------------------------------

def test_every_call_is_logged_with_model_versions_and_tokens(seeded):
    packet = packets.build(seeded, "q2_test_drives_without_sales", now=NOW)
    seeded.execute("DELETE FROM analyst_log")
    ask(seeded, "q2_test_drives_without_sales",
        StubBedrock(answer=answer(claims=[claim(packet, "larkspur_motors.probable_test_drives.change_pct")])))

    row = seeded.execute("SELECT * FROM analyst_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["question_id"] == "q2_test_drives_without_sales"
    assert row["answer_source"] == "bedrock" and row["model_id"] == "test-model"
    assert row["latency_ms"] is not None and row["stop_reason"] == "end_turn"
    assert row["prompt_version"] == analyst.PROMPT_VERSION
    assert row["packet_schema_version"] == packets.PACKET_SCHEMA_VERSION
    assert row["input_tokens"] == 100 and row["output_tokens"] == 20


def test_audit_endpoint_limit_is_capped(seeded):
    assert len(analyst.recent_calls(seeded, limit=1)) <= 1
    assert len(analyst.recent_calls(seeded, limit=10_000)) <= 50


def test_the_model_only_ever_receives_the_packet(seeded):
    client = StubBedrock(answer=answer())
    ask(seeded, "q2_test_drives_without_sales", client)

    sent = client.last_call["messages"][0]["content"][0]["text"]
    assert sent.startswith("{") and "q2_test_drives_without_sales" in sent
    assert "never invent" in client.last_call["system"][0]["text"].lower()
    assert client.last_call["inferenceConfig"]["temperature"] == 0


def test_answer_sources_cover_every_outcome(seeded):
    packet = packets.build(seeded, "q1_conversion_decline", now=NOW)
    good = claim(packet, "sales.change_pct")
    cases = {
        "bedrock": StubBedrock(answer=answer(claims=[good])),
        "fallback_rejected": StubBedrock(answer=answer(claims=[dict(good, value=999)])),
        "fallback_unavailable": StubBedrock(error=TimeoutError("read timeout")),
    }
    for expected, client in cases.items():
        assert ask(seeded, "q1_conversion_decline", client)["answer_source"] == expected
