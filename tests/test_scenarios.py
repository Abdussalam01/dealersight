"""Scenario evaluation: does the whole product behave sensibly in the situations it exists for?

The unit tests prove the parts work. These run clearly labelled synthetic cases through the real
pipeline — ingestion, correlation, the funnel, the pattern detector, the finding checks and the
analyst — and assert the behaviour a reader would expect, including the cases where the honest
answer is that the evidence supports nothing.

Each scenario is also reported by `scripts/run_evaluation.py` for the README and the demo.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend import scenarios

EVAL_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def counts(conn):
    return conn.execute(
        """SELECT (SELECT count(*) FROM raw_event) AS events, (SELECT count(*) FROM derived_event) AS derived,
                  (SELECT count(*) FROM sale) AS sales, (SELECT count(*) FROM dealer) AS dealers"""
    ).fetchone()


@pytest.mark.parametrize("key,title,expectation,run", scenarios.SCENARIOS,
                         ids=[entry[0] for entry in scenarios.SCENARIOS])
def test_scenario(conn, key, title, expectation, run):
    observed, passed, detail = run(conn, EVAL_NOW)
    assert passed, f"{title}\n  expected: {expectation} ({detail})\n  observed: {observed}"


def test_run_all_leaves_no_trace(conn):
    """The evaluation must be runnable against a live database without changing it."""
    before = counts(conn)
    results = scenarios.run_all(conn, EVAL_NOW)

    assert len(results) == len(scenarios.SCENARIOS)
    assert counts(conn) == before, "run_all must roll every scenario back"


def test_every_scenario_states_an_expectation():
    """A scenario with no stated expectation cannot fail, so it proves nothing."""
    for key, title, expectation, run in scenarios.SCENARIOS:
        assert expectation and title, f"{key} is missing a title or expectation"
        assert run.__doc__, f"{key} must say in its docstring why the expectation is the right one"


def test_ambiguous_pairing_is_reported_as_assumption_dependent(conn):
    """The headline claim of the evaluation: the same events give different answers under
    different matching windows, and the product says so rather than picking one quietly."""
    observed, passed, _ = scenarios.several_departures_could_match_a_return(conn, EVAL_NOW)
    by_setting = observed["probable_test_drives_by_setting"]

    assert passed
    assert len(set(by_setting.values())) > 1, by_setting
    assert by_setting["generous"] > by_setting["strict"], "a wider window cannot match fewer pairs"


def test_outage_fallback_states_no_figures_of_its_own(conn):
    """A fallback that invented numbers would be worse than no answer at all."""
    observed, passed, _ = scenarios.bedrock_is_unavailable(conn, EVAL_NOW)

    assert passed
    assert observed["answer_source"] == "fallback_unavailable"
    assert not any(character.isdigit() for character in observed["summary"])
