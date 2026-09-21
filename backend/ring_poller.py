"""Polls Ring Event History (the documented "API polling alternative" to webhooks).

Initial sync follows `links.next` back to the demo watermark. Later polls stop
as soon as they reach an event that is already stored.
"""

import logging
import threading

from backend import config, correlation, db, ingest
from backend.ring_client import RingAuthError, RingClient

log = logging.getLogger("dealersight.poller")
MAX_PAGES = 20


class RingPoller:
    def __init__(self, client=None, interval=None):
        self.client = client or RingClient()
        self.interval = interval or config.RING_POLL_SECONDS
        self.status = {"ok": None, "error": None, "last_poll_at": None, "devices": 0}
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
        self.status.update(ok=True, error=None, last_poll_at=ingest.utcnow().isoformat(), devices=len(devices))
        return results

    def _poll_device(self, conn, ring_device_id, watermark):
        results, next_link = [], None
        for _ in range(MAX_PAGES):
            events, next_link = self.client.event_history_page(ring_device_id, next_link)
            reached_old = False
            for event in events:
                if ingest.is_known_event(conn, event.get("id")):
                    return results  # everything older was processed on an earlier poll
                results.append(ingest.ingest(conn, event))
                start = event.get("attributes", {}).get("start")
                if start and ingest._from_epoch_ms(start) < watermark:
                    reached_old = True
            if reached_old or not next_link:
                break
        return results

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
