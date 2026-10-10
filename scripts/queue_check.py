"""Prove the Amazon SQS round trip without needing a Ring token.

    python scripts/queue_check.py

Publishes one synthetic Ring-shaped event, has the worker consume it, and reports what SQS did
at each step: sent, received, handled, deleted. It is a check of the transport, not of the
funnel — the event names a device that does not exist, so ingestion rejects it as an unknown
device and nothing is written to the funnel. That is the intended outcome: the queue path is
exercised end to end and the demo data is left untouched.

A real Ring event exercises the rest: arm a camera position, trigger in the Playground, and
watch it arrive through the same queue.
"""

import argparse
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import config, db, pipeline   # noqa: E402


def synthetic_event():
    """A Ring Event History record in the documented shape, from a device that does not exist."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "type": "history-events",
        "id": f"queue-check-{now_ms}",
        "attributes": {"event_type": "motion", "start": now_ms, "end": now_ms + 25000,
                       "is_third_party_reviewed": True},
        "relationships": {"source": {"data": {"type": "devices", "id": "queue.check.not.a.real.device"}}},
        "meta": {"riid": None},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30, help="seconds to wait for the message")
    args = parser.parse_args()

    print(f"EVENT_PIPELINE = {config.EVENT_PIPELINE!r}")
    print(f"SQS_QUEUE_URL  = {config.SQS_QUEUE_URL or '(not set)'}")
    print(f"SQS_DLQ_URL    = {config.SQS_DLQ_URL or '(not set)'}")
    print(f"resolved mode  = {pipeline.mode()}\n")

    if pipeline.mode() != "sqs":
        print("Not in sqs mode, so there is nothing to check.")
        print("Set EVENT_PIPELINE=sqs and SQS_QUEUE_URL in .env, then run this again.")
        return 1

    client = pipeline.sqs_client()

    before = pipeline.status(client)
    print(f"queue before : {before['queued']} queued, {before['in_flight']} in flight, "
          f"{before['dead_letter']} dead-lettered")
    if before.get("error"):
        print(f"\nCould not read the main queue: {before['error']}")
        print("Check the queue URL, the region, and that the IAM policy allows sqs:GetQueueAttributes.")
        return 1
    if before.get("dead_letter_error"):
        print("note         : dead-letter depth unreadable "
              f"({before['dead_letter_error'].split(':')[0]}) - reporting only, "
              "the event path is unaffected")

    event = synthetic_event()
    message_id = pipeline.publish(event, client)
    print(f"sent         : message {message_id} carrying ring event {event['id']}")

    worker = pipeline.Worker(client)
    deadline = time.monotonic() + args.timeout
    results = []
    with db.connect() as conn:
        while time.monotonic() < deadline and not results:
            results = worker.drain_once(conn, client)
            if not results and worker.status["handled"]:
                break   # handled, but produced no ingest result

    if not worker.status["handled"]:
        print(f"\nNo message came back within {args.timeout:.0f}s.")
        print("If the application is running with EVENT_PIPELINE=sqs, its worker probably consumed it")
        print("first — check the server log, or stop the server and run this again.")
        return 1

    status = results[0]["status"] if results else "handled"
    print(f"received     : {worker.status['handled']} message(s)")
    print(f"handled      : ingest status {status!r}")
    print(f"deleted      : {worker.status['handled']} message(s), after the database transaction committed")

    after = pipeline.status(client)
    print(f"queue after  : {after['queued']} queued, {after['in_flight']} in flight, "
          f"{after['dead_letter']} dead-lettered")

    print("\nThe queue round trip works: published, received, handled and deleted.")
    if status == "invalid":
        print("The ingest status is 'invalid' because the synthetic event names a device that does not")
        print("exist. That is expected, and it is why this check leaves the funnel untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
