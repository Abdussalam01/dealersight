"""Event delivery between the Ring poller and ingestion.

    EVENT_PIPELINE=local   the poller ingests directly. The default, and the only mode a
                           reviewer needs: it requires no AWS account.
    EVENT_PIPELINE=sqs     the poller publishes each Ring event to Amazon SQS and a worker
                           consumes the queue. Polling and processing are separated, so a
                           slow or failing ingest cannot stall the Ring poll, and a crash
                           redelivers the event instead of losing it.

Three properties make this safe, and each has a test:

* **At-least-once is fine.** SQS may deliver a message more than once. Ingestion is keyed on
  the Ring event id under a unique constraint, so a second delivery is recorded as a duplicate
  and changes no metric.
* **Delete only after commit.** A message is deleted from the queue only once the database
  transaction has committed. If the process dies mid-ingest, the message becomes visible again
  after the visibility timeout and is retried.
* **Poison messages leave the pipeline.** A message that keeps failing is moved to the
  dead-letter queue by the queue's redrive policy, so one bad event cannot block the rest.
  DealerSight reports the dead-letter depth rather than silently ignoring it.
"""

import json
import logging
import threading

from backend import config, correlation, db, ingest

log = logging.getLogger("dealersight.pipeline")


def mode():
    """`sqs` only when it is both asked for and configured; otherwise `local`."""
    if config.EVENT_PIPELINE == "sqs" and config.SQS_QUEUE_URL:
        return "sqs"
    return "local"


def sqs_client():
    import boto3   # imported lazily so local mode never needs botocore configured

    session = boto3.Session(profile_name=config.AWS_PROFILE or None, region_name=config.AWS_REGION)
    return session.client("sqs")


def publish(event, client=None):
    """Put one Ring Event History record on the queue. Returns the SQS message id."""
    client = client or sqs_client()
    response = client.send_message(QueueUrl=config.SQS_QUEUE_URL, MessageBody=json.dumps(event))
    return response.get("MessageId")


def status(client=None):
    """Queue depth for the status panel. Never raises: this is reporting, not the event path."""
    if mode() != "sqs":
        return {"mode": "local", "queued": None, "in_flight": None, "dead_letter": None}
    result = {"mode": "sqs", "queued": None, "in_flight": None, "dead_letter": None,
              "error": None, "dead_letter_error": None}
    try:
        client = client or sqs_client()
        attributes = client.get_queue_attributes(
            QueueUrl=config.SQS_QUEUE_URL,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        result["queued"] = int(attributes.get("ApproximateNumberOfMessages", 0))
        result["in_flight"] = int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    # The dead-letter depth is reporting, not the event path: failing to read it must not
    # hide the main queue's numbers or stop anything.
    if config.SQS_DLQ_URL:
        try:
            dead = (client or sqs_client()).get_queue_attributes(
                QueueUrl=config.SQS_DLQ_URL, AttributeNames=["ApproximateNumberOfMessages"],
            )["Attributes"]
            result["dead_letter"] = int(dead.get("ApproximateNumberOfMessages", 0))
        except Exception as exc:
            result["dead_letter_error"] = f"{type(exc).__name__}: {exc}"
    return result


def handle_message(conn, message):
    """Ingest one queued event. Returns the ingest result, or None if the body is unreadable.

    The caller deletes the message only when this returns without raising, so anything that
    raises is redelivered by SQS and eventually dead-lettered.
    """
    try:
        payload = json.loads(message["Body"])
    except (ValueError, KeyError) as exc:
        # Unparseable: deleting it is correct, since redelivery cannot help.
        log.warning("dropping unreadable queue message: %s", exc)
        return None
    with conn.transaction():
        return ingest.ingest(conn, payload)


class Worker:
    """Consumes the queue in a background thread while the application runs."""

    def __init__(self, client=None):
        self.client = client
        self.status = {"running": False, "handled": 0, "failed": 0, "last_error": None}
        self._stop = threading.Event()
        self._thread = None

    def drain_once(self, conn, client=None):
        """One long-poll receive. Returns the ingest results for the batch."""
        client = client or self.client or sqs_client()
        response = client.receive_message(
            QueueUrl=config.SQS_QUEUE_URL,
            MaxNumberOfMessages=config.SQS_BATCH,
            WaitTimeSeconds=config.SQS_WAIT_SECONDS,
        )
        messages = response.get("Messages", [])
        results = []
        for message in messages:
            try:
                result = handle_message(conn, message)
            except Exception as exc:
                # Leave the message on the queue: it becomes visible again after the
                # visibility timeout, and the redrive policy dead-letters it eventually.
                self.status["failed"] += 1
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                log.exception("queued event failed; leaving it for redelivery")
                continue
            client.delete_message(QueueUrl=config.SQS_QUEUE_URL, ReceiptHandle=message["ReceiptHandle"])
            self.status["handled"] += 1
            if result is not None:
                results.append(result)
        if results:
            correlation.rebuild(conn)
        return results

    def _run(self):
        self.status["running"] = True
        with db.connect() as conn:
            while not self._stop.is_set():
                try:
                    for result in self.drain_once(conn):
                        log.info("queued ring event %s", result)
                except Exception as exc:   # keep consuming through transient failures
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                    log.exception("queue receive failed")
                    self._stop.wait(5)
        self.status["running"] = False

    def start(self):
        self._thread = threading.Thread(target=self._run, name="sqs-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
