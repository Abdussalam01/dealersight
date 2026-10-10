"""Polls Ring Event History (the documented "API polling alternative" to webhooks).

Initial sync follows `links.next` back to the demo watermark. Later polls rescan a
bounded overlap window rather than stopping at the first event already stored, so an
entry that becomes visible late is still ingested. Events are deduplicated by their
Ring event id, so rescanning costs nothing but a page fetch.

Maximum supported lateness is OVERLAP_MINUTES: an event that appears in history more
than that long after it started is not picked up, and the scan is reported incomplete
if the page budget runs out first.
"""

import logging
import threading
from datetime import timedelta

from backend import config, correlation, db, ingest, pipeline
from backend.ring_client import RingAuthError, RingClient

log = logging.getLogger("dealersight.poller")
MAX_PAGES = 20
OVERLAP_MINUTES = 30


class RingPoller:
    def __init__(self, client=None, interval=None, sqs=None):
        self.client = client or RingClient()
        self.interval = interval or config.RING_POLL_SECONDS
        self.sqs = sqs   # injected in tests; built lazily in sqs mode otherwise
        self.status = {"ok": None, "error": None, "last_poll_at": None, "devices": 0,
                       "scan_complete": None, "overlap_minutes": OVERLAP_MINUTES,
                       "pipeline": pipeline.mode()}
        self._stop = threading.Event()
        self._thread = None

    def poll_once(self, conn):
        watermark = ingest.current_session(conn)["watermark"]
        devices = self.client.list_devices()
        results = []
        for device in devices:
            name = device.get("attributes", {}).get("name", "Ring device")
            ingest.ensure_device(conn, device["id"], name)
            results += self._poll_device(conn, device["id"], watermark)
        correlation.rebuild(conn)  # every poll, so departures also expire on time
        self.status.update(ok=True, error=None, last_poll_at=ingest.utcnow().isoformat(), devices=len(devices),
                           pipeline=pipeline.mode())
        return results

    def _poll_device(self, conn, ring_device_id, watermark):
        """Scan back to the overlap cutoff, ingesting anything not already stored."""
        cutoff = max(watermark, ingest.utcnow() - timedelta(minutes=OVERLAP_MINUTES))
        results, next_link, complete = [], None, False
        for _ in range(MAX_PAGES):
            events, next_link = self.client.event_history_page(ring_device_id, next_link)
            reached_old = False
            for event in events:
                start = event.get("attributes", {}).get("start")
                if start and ingest._from_epoch_ms(start) < cutoff:
                    reached_old = True      # older than we re-check; stop after this page
                    continue
                if ingest.is_known_event(conn, event.get("id")):
                    continue                # already stored: keep scanning for late arrivals
                results.append(self._deliver(conn, event))
            if reached_old or not next_link:
                complete = True
                break
        self.status["scan_complete"] = complete
        return results

    def _deliver(self, conn, event):
        """Hand the event to ingestion, directly or through the queue."""
        if pipeline.mode() != "sqs":
            return ingest.ingest(conn, event)
        message_id = pipeline.publish(event, self.sqs)
        return {"status": "queued", "message_id": message_id, "ring_event_id": event.get("id")}

    def _run(self):
        with db.connect() as conn:
            while not self._stop.is_set():
                try:
                    for result in self.poll_once(conn):
                        log.info("ring event %s", result)
                except RingAuthError as exc:
                    self.status.update(ok=False, error=str(exc))
                    correlation.rebuild(conn)  # keep expiring departures while the token is refreshed
                except Exception as exc:  # keep polling through transient failures
                    log.exception("poll failed")
                    self.status.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="ring-poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
