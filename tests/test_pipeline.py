"""Event delivery: local ingestion, and the Amazon SQS path with its dead-letter behaviour.

These tests never touch AWS. A fake queue stands in for SQS and records exactly what the
application asked it to do, which is the part worth pinning: when a message is deleted, what
happens on a duplicate delivery, and what happens when ingestion fails.
"""

import json

import pytest

from backend import config, correlation, ingest, pipeline
from tests.conftest import RING_DEVICE, T0, history_event
from tests.test_correlation import at


class FakeSQS:
    """Records calls the way SQS would behave: a message stays until it is deleted."""

    def __init__(self):
        self.sent, self.deleted, self.queue = [], [], []
        self.receive_calls = 0

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(MessageBody)
        message = {"MessageId": f"m{len(self.sent)}", "ReceiptHandle": f"r{len(self.sent)}",
                   "Body": MessageBody}
        self.queue.append(message)
        return {"MessageId": message["MessageId"]}

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds):
        self.receive_calls += 1
        batch, self.queue = self.queue[:MaxNumberOfMessages], self.queue[MaxNumberOfMessages:]
        return {"Messages": batch} if batch else {}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


@pytest.fixture
def sqs_mode(monkeypatch):
    monkeypatch.setattr(config, "EVENT_PIPELINE", "sqs")
    monkeypatch.setattr(config, "SQS_QUEUE_URL", "https://sqs.test/queue")
    monkeypatch.setattr(config, "SQS_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "SQS_BATCH", 10)
    return FakeSQS()


def test_local_is_the_default_so_a_reviewer_needs_no_aws(monkeypatch):
    monkeypatch.setattr(config, "EVENT_PIPELINE", "local")
    assert pipeline.mode() == "local"


def test_sqs_asked_for_but_not_configured_falls_back_to_local(monkeypatch):
    """A half-configured queue must not silently swallow the live event path."""
    monkeypatch.setattr(config, "EVENT_PIPELINE", "sqs")
    monkeypatch.setattr(config, "SQS_QUEUE_URL", "")
    assert pipeline.mode() == "local"


def test_worker_ingests_a_queued_event_and_then_deletes_it(conn, device_id, sqs_mode):
    ingest.arm(conn, device_id, "entrance", now=at(0), minutes=10)
    pipeline.publish(history_event("q1", at(1)), sqs_mode)
    assert len(sqs_mode.sent) == 1
    assert not sqs_mode.deleted, "nothing may be deleted before it is handled"

    results = pipeline.Worker().drain_once(conn, sqs_mode)

    assert [r["status"] for r in results] == ["accepted"]
    assert sqs_mode.deleted == ["r1"], "the message is deleted once the ingest has committed"
    assert ingest.is_known_event(conn, "q1")


def test_a_failed_ingest_leaves_the_message_for_redelivery(conn, device_id, sqs_mode, monkeypatch):
    """SQS can only retry what was not deleted, so a failure must not delete."""
    ingest.arm(conn, device_id, "entrance", now=at(0), minutes=10)
    pipeline.publish(history_event("q2", at(1)), sqs_mode)

    def explode(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ingest, "ingest", explode)
    worker = pipeline.Worker()
    results = worker.drain_once(conn, sqs_mode)

    assert results == []
    assert sqs_mode.deleted == [], "a message that failed must stay on the queue"
    assert worker.status["failed"] == 1
    assert "database is down" in worker.status["last_error"]


def test_duplicate_delivery_changes_no_metric(conn, device_id, sqs_mode):
    """SQS is at-least-once. The Ring event id is unique, so a second delivery is a no-op."""
    ingest.arm(conn, device_id, "entrance", now=at(0), minutes=10)
    event = history_event("q3", at(1))

    pipeline.publish(event, sqs_mode)
    pipeline.Worker().drain_once(conn, sqs_mode)
    correlation.rebuild(conn, now=at(minutes=120))
    visits_once = conn.execute("SELECT count(*) c FROM derived_event WHERE type = 'visit'").fetchone()["c"]

    pipeline.publish(event, sqs_mode)          # the same event delivered again
    second = pipeline.Worker().drain_once(conn, sqs_mode)
    correlation.rebuild(conn, now=at(minutes=120))
    visits_twice = conn.execute("SELECT count(*) c FROM derived_event WHERE type = 'visit'").fetchone()["c"]

    assert [r["status"] for r in second] == ["duplicate"]
    assert visits_twice == visits_once == 1
    assert sqs_mode.deleted == ["r1", "r2"], "a duplicate is still deleted; redelivering it would not help"


def test_an_unreadable_message_is_deleted_rather_than_redelivered_forever(conn, sqs_mode):
    """Redelivery cannot fix malformed JSON, so it is dropped with a log line, not retried."""
    sqs_mode.send_message(QueueUrl="q", MessageBody="{not json")

    results = pipeline.Worker().drain_once(conn, sqs_mode)

    assert results == []
    assert sqs_mode.deleted == ["r1"]


def test_poller_publishes_instead_of_ingesting_when_the_queue_is_on(conn, device_id, sqs_mode):
    """In sqs mode the poll must not write events itself: that is the worker's job."""
    from backend.ring_poller import RingPoller

    class StubRing:
        def list_devices(self):
            return [{"id": RING_DEVICE, "attributes": {"name": "Playground Device"}}]

        def event_history_page(self, device_id, next_link=None):
            return [history_event("q4", ingest.utcnow())], None

    ingest.arm(conn, device_id, "entrance", now=ingest.utcnow(), minutes=10)
    poller = RingPoller(client=StubRing(), sqs=sqs_mode)
    results = poller.poll_once(conn)

    assert [r["status"] for r in results] == ["queued"]
    assert len(sqs_mode.sent) == 1
    assert not ingest.is_known_event(conn, "q4"), "the poller must not have stored it"
    assert poller.status["pipeline"] == "sqs"

    pipeline.Worker().drain_once(conn, sqs_mode)
    assert ingest.is_known_event(conn, "q4"), "the worker stores it"


def test_published_body_is_the_ring_event_unchanged(sqs_mode):
    """What goes on the queue is the Ring record, so the worker ingests exactly what Ring sent."""
    event = history_event("q5", T0)
    pipeline.publish(event, sqs_mode)
    assert json.loads(sqs_mode.sent[0]) == event


def test_status_reports_local_without_touching_aws(monkeypatch):
    monkeypatch.setattr(config, "EVENT_PIPELINE", "local")
    assert pipeline.status() == {"mode": "local", "queued": None, "in_flight": None, "dead_letter": None}
