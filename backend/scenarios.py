"""Named evaluation scenarios: synthetic cases with an expected behaviour, run through the real pipeline.

Unit tests prove the parts work. These prove the whole behaves sensibly in the situations the
product exists to handle — including the ones where the honest answer is "the evidence does not
support that". Each scenario builds clearly labelled synthetic data, runs ingestion, correlation,
the funnel, the pattern detector, the finding checks and the analyst, then compares what happened
with what should happen.

Every event here is stored as `simulated_baseline`: Ring event shape, not Ring-originated. Nothing
in this module is presented as real dealership activity, and `run_all` writes nothing permanent.
"""

import json
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from backend import analyst, checks, correlation, funnel, ingest

OPENS = 10   # synthetic activity sits inside business hours


# --- fixture builders --------------------------------------------------------

def _dealer(conn, code, name):
    return conn.execute(
        """INSERT INTO dealer (code, name, region, is_demo) VALUES (%s, %s, 'Evaluation', false)
           ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id""",
        (code, name),
    ).fetchone()["id"]


def _cameras(conn, dealer_id, code, placed_at):
    """One permanently placed camera per position, as a production deployment would have."""
    devices = {}
    for position in ("entrance", "display_area", "lot_departure", "lot_return"):
        device_id = conn.execute(
            """INSERT INTO device (ring_device_id, display_name, mode, dealer_id)
               VALUES (%s, %s, 'production', %s)
               ON CONFLICT (ring_device_id) DO UPDATE SET dealer_id = EXCLUDED.dealer_id RETURNING id""",
            (f"eval.{code}.{position}", f"{position.replace('_', ' ').title()} camera", dealer_id),
        ).fetchone()["id"]
        position_id = conn.execute("SELECT id FROM camera_position WHERE code = %s", (position,)).fetchone()["id"]
        if not conn.execute("SELECT 1 FROM device_assignment WHERE device_id = %s", (device_id,)).fetchone():
            conn.execute(
                """INSERT INTO device_assignment (device_id, camera_position_id, valid_from, zone_source)
                   VALUES (%s, %s, %s, 'device_configuration')""",
                (device_id, position_id, placed_at),
            )
        devices[position] = (device_id, position_id)
    return devices


def _event(conn, device, start, seconds=25):
    device_id, position_id = device
    conn.execute(
        """INSERT INTO raw_event (ring_event_id, device_id, ring_event_type, started_at, ended_at, received_at,
                                  camera_position_id, zone_source, accepted, source, payload)
           VALUES (%s, %s, 'motion', %s, %s, %s, %s, 'device_configuration', true, 'simulated_baseline', %s)
           ON CONFLICT (ring_event_id) DO NOTHING""",
        (f"eval-{device_id}-{int(start.timestamp())}-{seconds}", device_id, start,
         start + timedelta(seconds=seconds), start + timedelta(seconds=seconds + 30), position_id,
         Jsonb({"simulated_baseline": True, "note": "Not a Ring event: evaluation scenario in Ring event shape"})),
    )


def _visits(conn, devices, period, count):
    day = period[0] + timedelta(hours=OPENS)
    for index in range(count):
        _event(conn, devices["entrance"], day + timedelta(minutes=index * 7))


def _drives(conn, devices, period, count, minutes=25):
    """A departure and a matching return `minutes` apart: a probable test-drive session."""
    day = period[0] + timedelta(hours=OPENS)
    for index in range(count):
        departure = day + timedelta(minutes=index * 90)
        _event(conn, devices["lot_departure"], departure)
        _event(conn, devices["lot_return"], departure + timedelta(minutes=minutes))


def _sales(conn, dealer_id, period, count, financed=0):
    day = period[0] + timedelta(hours=OPENS)
    for index in range(count):
        sale_id = conn.execute(
            "INSERT INTO sale (dealer_id, occurred_at, model_group) VALUES (%s, %s, 'Sedan A') RETURNING id",
            (dealer_id, day + timedelta(minutes=index * 40)),
        ).fetchone()["id"]
        if index < financed:
            conn.execute(
                "INSERT INTO finance_deal (sale_id, dealer_id, occurred_at) VALUES (%s, %s, %s)",
                (sale_id, dealer_id, day + timedelta(minutes=index * 40 + 30)),
            )


def _fixture(conn, code, name, now):
    period_a, period_b = funnel.periods(now)
    dealer_id = _dealer(conn, code, name)
    devices = _cameras(conn, dealer_id, code, period_a[0] - timedelta(days=1))
    return dealer_id, devices, period_a, period_b


def _patterns_for(conn, name, period_a, period_b):
    return [p["pattern"] for p in funnel.patterns(conn, period_a, period_b) if p.get("dealer") == name]


# --- the scenarios -----------------------------------------------------------

def steady_activity_and_sales(conn, now):
    """Nothing changed. The product must not manufacture a decline."""
    dealer_id, devices, a, b = _fixture(conn, "eval-steady", "Evenkeel Motors", now)
    for period in (a, b):
        _visits(conn, devices, period, 20)
        _drives(conn, devices, period, 6)
        _sales(conn, dealer_id, period, 3)
    correlation.rebuild(conn, now=now)

    found = _patterns_for(conn, "Evenkeel Motors", a, b)
    review = checks.check_finding(conn, dealer_id, now=now)
    observed = {"patterns_flagged": found, "verdict": review["verdict"]}
    passed = not found and review["verdict"] == "not_supported"
    return observed, passed, "no pattern is flagged and the decline premise is reported as not supported"


def test_drives_rise_while_sales_fall(conn, now):
    """The situation the product exists to surface. It must flag it and show the evidence."""
    dealer_id, devices, a, b = _fixture(conn, "eval-gap", "Rivermouth Motors", now)
    _visits(conn, devices, a, 20)
    _drives(conn, devices, a, 5)
    _sales(conn, dealer_id, a, 4)
    _visits(conn, devices, b, 22)
    _drives(conn, devices, b, 9)          # +80%
    _sales(conn, dealer_id, b, 2)         # -50%
    correlation.rebuild(conn, now=now)

    found = _patterns_for(conn, "Rivermouth Motors", a, b)
    evidence = funnel.compare(conn, dealer_id, a, b)
    observed = {"patterns_flagged": found,
                "probable_test_drives": [evidence["period_a"]["probable_test_drives"],
                                         evidence["period_b"]["probable_test_drives"]],
                "sales": [evidence["period_a"]["sales"], evidence["period_b"]["sales"]]}
    passed = "test_drives_grew_without_sales" in found
    return observed, passed, "the pattern is flagged with the counts behind it"


def one_period_has_no_data(conn, now):
    """A missing period is not a decline. The product must explain the gap and withhold."""
    dealer_id, devices, a, b = _fixture(conn, "eval-gap-data", "Northfell Motors", now)
    _visits(conn, devices, b, 20)         # recent period only; nothing in the earlier period
    _drives(conn, devices, b, 5)
    _sales(conn, dealer_id, b, 2)
    correlation.rebuild(conn, now=now)

    review = checks.check_finding(conn, dealer_id, now=now)
    observed = {"verdict": review["verdict"], "reason": review["withheld_reason"],
                "failed_checks": [c["key"] for c in review["checks"] if c["outcome"] == checks.FAIL]}
    passed = review["verdict"] == "withheld" and bool(review["withheld_reason"])
    return observed, passed, "the verdict is withheld and names what is missing"


def several_departures_could_match_a_return(conn, now):
    """Two departures and one return: which pair is a test drive depends on the window, not the camera."""
    dealer_id, devices, a, b = _fixture(conn, "eval-ambiguous", "Trellis Motors", now)
    for period in (a, b):
        _visits(conn, devices, period, 20)
        _sales(conn, dealer_id, period, 2)
    _drives(conn, devices, a, 4, minutes=25)
    # Recent period: short round trips that only a generous window counts as test drives.
    day = b[0] + timedelta(hours=OPENS)
    for index in range(4):
        departure = day + timedelta(minutes=index * 90)
        _event(conn, devices["lot_departure"], departure)
        _event(conn, devices["lot_return"], departure + timedelta(minutes=3))
    correlation.rebuild(conn, now=now)

    table = correlation.sensitivity(conn, dealer_id, {"period_b": b}, now=now)
    counts = {row["setting"]: row["counts"]["period_b"]["probable_test_drives"] for row in table}
    observed = {"probable_test_drives_by_setting": counts}
    passed = len(set(counts.values())) > 1
    return observed, passed, "the session count changes with the matching window, so the result is shown as assumption-dependent"


class _UnavailableBedrock:
    """Stands in for Bedrock being unreachable."""

    def converse(self, **_kwargs):
        raise RuntimeError("simulated Bedrock outage")


def bedrock_is_unavailable(conn, now):
    """The analyst must degrade to a labelled deterministic answer, never to silence or invention."""
    dealer_id, devices, a, b = _fixture(conn, "eval-outage", "Caldwell Motors", now)
    for period in (a, b):
        _visits(conn, devices, period, 20)
        _drives(conn, devices, period, 6)
        _sales(conn, dealer_id, period, 3)
    correlation.rebuild(conn, now=now)

    answer = analyst.ask(conn, "q1_conversion_decline", dealer_id, client=_UnavailableBedrock(), now=now)
    summary = answer["answer"]["summary"]
    observed = {"answer_source": answer["answer_source"], "summary": summary,
                "claims": len(answer["answer"].get("claims", []))}
    passed = (answer["answer_source"].startswith("fallback")
              and bool(summary)
              and not any(character.isdigit() for character in summary))
    return observed, passed, "a labelled deterministic fallback is returned, with no invented figures"


class _ContradictingBedrock:
    """Returns a schema-valid answer whose words contradict the figures it cites.

    The claim is copied straight out of the packet it was given, so metric id, value, unit and
    direction all check out. Only the prose is wrong. A validator that checked structure alone
    would accept this answer.
    """

    METRIC = "sales_conversion.change_points"

    def converse(self, **kwargs):
        packet = json.loads(kwargs["messages"][0]["content"][0]["text"])
        metric = packet["metrics"][self.METRIC]
        body = {
            "summary": ("Sales conversion fell sharply between the two periods. These metrics describe "
                        "what changed and do not prove a cause."),
            "claims": [{"metric_id": self.METRIC, "label": metric["label"], "value": metric["value"],
                        "unit": metric["unit"], "direction": metric["direction"]}],
            "investigate": ["lead follow-up time"],
        }
        return {"output": {"message": {"content": [{"text": json.dumps(body)}]}},
                "stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 40}}


def model_answer_contradicts_the_figures(conn, now):
    """A schema-valid answer can still be false. The meaning must be checked, not just the shape."""
    dealer_id, devices, a, b = _fixture(conn, "eval-contradiction", "Ashgrove Motors", now)
    for period in (a, b):
        _visits(conn, devices, period, 20)
        _drives(conn, devices, period, 6)
        _sales(conn, dealer_id, period, 3)     # steady: conversion does not move
    correlation.rebuild(conn, now=now)

    answer = analyst.ask(conn, "q1_conversion_decline", dealer_id, client=_ContradictingBedrock(), now=now)
    observed = {"answer_source": answer["answer_source"], "error": answer.get("error"),
                "summary": answer["answer"]["summary"]}
    passed = (answer["answer_source"] == "fallback_rejected"
              and "fell" not in answer["answer"]["summary"].lower())
    return observed, passed, "the answer is rejected for contradicting the figures and the labelled fallback is shown"


SCENARIOS = [
    ("steady_activity_and_sales", "Activity and sales remain steady",
     "No misleading decline alert", steady_activity_and_sales),
    ("test_drives_rise_while_sales_fall", "Probable test-drive activity rises while sales fall",
     "Flag the pattern and show its evidence", test_drives_rise_while_sales_fall),
    ("one_period_has_no_data", "One comparison period has missing data",
     "Explain the gap and withhold unsupported conclusions", one_period_has_no_data),
    ("several_departures_could_match_a_return", "Several departures could match a return",
     "Show that the result depends on the matching assumption", several_departures_could_match_a_return),
    ("bedrock_is_unavailable", "Bedrock is unavailable",
     "Return the labelled deterministic fallback", bedrock_is_unavailable),
    ("model_answer_contradicts_the_figures", "The model's answer contradicts the figures it cites",
     "Reject the answer on meaning, not just on shape", model_answer_contradicts_the_figures),
]


def run_all(conn, now=None):
    """Run every scenario in its own transaction and roll it back. Writes nothing permanent."""
    now = now or ingest.utcnow()
    results = []
    for key, title, expectation, run in SCENARIOS:
        try:
            with conn.transaction() as transaction:   # noqa: F841 - rolled back below
                observed, passed, detail = run(conn, now)
                results.append({"key": key, "title": title, "expectation": expectation,
                                "observed": observed, "passed": passed, "detail": detail})
                raise psycopg.Rollback
        except psycopg.Rollback:
            pass
    return results
