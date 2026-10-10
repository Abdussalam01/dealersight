"""Run the scenario evaluation and write the report.

    python scripts/run_evaluation.py              # run against the configured database
    python scripts/run_evaluation.py --json-only  # no console table

Each scenario builds clearly labelled synthetic data, runs it through the real pipeline, and
compares what happened with what should happen. Every scenario runs inside a transaction that is
rolled back, so this is safe against a live database: it writes nothing except the report.

Output: docs/evidence/scenario-evaluation.json
"""

import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import config, db, ingest, scenarios   # noqa: E402

REPORT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "evidence" / "scenario-evaluation.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-only", action="store_true", help="write the report without printing a table")
    args = parser.parse_args()

    now = ingest.utcnow()
    with db.connect() as conn:
        results = scenarios.run_all(conn, now)

    report = {
        "evaluation": "DealerSight scenario evaluation",
        "run_at": now.isoformat(),
        "method": ("Each scenario builds labelled synthetic data (stored as simulated_baseline: Ring event "
                   "shape, not Ring-originated), runs the real ingestion, correlation, funnel, pattern and "
                   "analyst code, then compares the behaviour with the stated expectation. Every scenario "
                   "runs in a transaction that is rolled back."),
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "scenarios": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if not args.json_only:
        width = max(len(r["title"]) for r in results)
        print(f"\nScenario evaluation  ({report['passed']}/{report['total']} as expected)\n")
        for result in results:
            print(f"  {'PASS' if result['passed'] else 'FAIL'}  {result['title']:<{width}}  {result['expectation']}")
            print(f"        observed: {json.dumps(result['observed'], default=str)}")
        print(f"\nReport written to {REPORT.relative_to(pathlib.Path.cwd()) if REPORT.is_relative_to(pathlib.Path.cwd()) else REPORT}\n")

    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
