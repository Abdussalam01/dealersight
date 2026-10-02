#!/usr/bin/env python3
"""Compare Bedrock models on the three analyst questions.

    python scripts/benchmark_models.py                      # default candidate list
    python scripts/benchmark_models.py amazon.nova-pro-v1:0 amazon.nova-micro-v1:0

Each question is asked RUNS times per model with the real system prompt and packet, and the
answer is put through the same validator the application uses. Results are written to
docs/evidence/model-benchmark.json.

What this measures is **validator acceptance**: whether an answer parsed, named real metric ids
with the right values, units and directions, carried the required no-causation statement and
avoided contradicting itself. It is not a semantic review of whether the prose is insightful;
read the sampled summaries in the output for that.
"""

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import analyst, db, funnel, packets  # noqa: E402

DEFAULT_CANDIDATES = ["amazon.nova-pro-v1:0", "amazon.nova-micro-v1:0", "us.amazon.nova-2-lite-v1:0"]
RUNS = 3
OUT = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "model-benchmark.json"


def cases(conn):
    dealers = funnel.dealers(conn)
    declining = next(d for d in dealers if d["name"].startswith("Brookfield"))
    steady = next(d for d in dealers if d["is_demo"])
    return [
        ("q1_conversion_decline", declining["id"], "declining dealership"),
        ("q1_conversion_decline", steady["id"], "steady dealership (question assumes a decline)"),
        ("q2_test_drives_without_sales", None, "network ranking"),
        ("q3_promotion_performance", None, "promotion"),
    ]


def main(candidates):
    with db.connect() as conn:
        built = {(q, d): packets.build(conn, q, d) for q, d, _ in cases(conn)}
        case_list = cases(conn)

    client = analyst.bedrock_client()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "runs_per_case": RUNS,
              "prompt_version": analyst.PROMPT_VERSION, "packet_schema_version": packets.PACKET_SCHEMA_VERSION,
              "measures": "validator acceptance, not semantic correctness", "models": {}}

    for model in candidates:
        rows = []
        for question, dealer, label in case_list:
            packet = built[(question, dealer)]
            for _ in range(RUNS):
                started = time.perf_counter()
                try:
                    response = analyst._call_bedrock(client, packet, model)
                except Exception as exc:
                    rows.append({"case": label, "error": f"{type(exc).__name__}: {analyst.sanitize(str(exc))[:160]}"})
                    break
                latency = round((time.perf_counter() - started) * 1000)
                text = response["output"]["message"]["content"][0]["text"].strip()
                structured, reasons = analyst.validate(text, packet)
                rows.append({"case": label, "question_id": question, "latency_ms": latency,
                             "stop_reason": response.get("stopReason"), "usage": response.get("usage"),
                             "accepted": not reasons, "reasons": reasons,
                             "summary": (structured or {}).get("summary", ""),
                             "claims": len((structured or {}).get("claims", []))})
            else:
                continue
            break

        attempted = [r for r in rows if "error" not in r]
        report["models"][model] = {
            "unavailable": rows[-1]["error"] if rows and "error" in rows[-1] else None,
            "accepted": sum(r["accepted"] for r in attempted),
            "attempted": len(attempted),
            "median_latency_ms": statistics.median([r["latency_ms"] for r in attempted]) if attempted else None,
            "mean_output_tokens": round(statistics.mean([r["usage"].get("outputTokens", 0) for r in attempted]))
            if attempted else None,
            "calls": rows,
        }
        summary = report["models"][model]
        print(f"{model:45} {summary['accepted']}/{summary['attempted']} accepted"
              f"  median {summary['median_latency_ms']} ms" if attempted else f"{model:45} unavailable")

    OUT.write_text(json.dumps(report, indent=1, default=str))
    print(f"\nwritten to {OUT.relative_to(Path(__file__).resolve().parent.parent)}")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_CANDIDATES)
