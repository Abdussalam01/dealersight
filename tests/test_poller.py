from datetime import timedelta

from backend import ingest, metrics
from backend.ring_poller import RingPoller
from tests.conftest import RING_DEVICE, history_event


class FakeRingClient:
    """Serves pages of history events, newest first, like the Ring API."""

    def __init__(self, pages):
        self.pages = pages
        self.page_requests = 0

    def list_devices(self):
        return [{"id": RING_DEVICE, "attributes": {"name": "Playground Device"}}]

    def event_history_page(self, device_id, next_link=None):
        self.page_requests += 1
        index = int(next_link) if next_link else 0
        next_index = index + 1 if index + 1 < len(self.pages) else None
        return self.pages[index], str(next_index) if next_index is not None else None


def test_repeated_polls_ingest_each_event_once(conn, demo_dealer):
    now = ingest.utcnow()
    ingest.start_session(conn, now=now - timedelta(minutes=30))
    device_id = ingest.ensure_device(conn, RING_DEVICE, "Playground Device", mode="demo")
    ingest.arm(conn, device_id, "entrance", now=now - timedelta(minutes=5))
    client = FakeRingClient([[history_event("new", now - timedelta(minutes=1)),
                              history_event("older", now - timedelta(minutes=2))]])
    poller = RingPoller(client=client)

    first = poller.poll_once(conn)
    second = poller.poll_once(conn)

    assert [r["status"] for r in first] == ["accepted", "accepted"]
    assert second == []
    assert metrics.visit_count(conn) == 2


def test_later_polls_stop_at_the_overlap_cutoff(conn, demo_dealer):
    """Rescanning is bounded: pages older than the overlap window are not fetched again."""
    now = ingest.utcnow()
    ingest.start_session(conn, now=now - timedelta(hours=4))
    ingest.ensure_device(conn, RING_DEVICE, "Playground Device", mode="demo")
    # page 0 is inside the overlap window, pages 1 and 2 are well outside it
    pages = [[history_event(f"e0{i}", now - timedelta(minutes=i)) for i in range(3)],
             [history_event(f"e1{i}", now - timedelta(minutes=90 + i)) for i in range(3)],
             [history_event(f"e2{i}", now - timedelta(minutes=180 + i)) for i in range(3)]]
    client = FakeRingClient(pages)
    poller = RingPoller(client=client)

    poller.poll_once(conn)                       # initial sync walks back to the cutoff
    assert poller.status["scan_complete"] is True

    client.page_requests = 0
    poller.poll_once(conn)                       # later poll only rescans the overlap window
    assert client.page_requests <= 2


def test_an_event_that_appears_late_is_still_ingested(conn, demo_dealer):
    """CR-07: a known event must not stop the scan, or an entry visible late is lost forever."""
    now = ingest.utcnow()
    ingest.start_session(conn, now=now - timedelta(hours=2))
    device = ingest.ensure_device(conn, RING_DEVICE, "Playground Device", mode="demo")
    ingest.arm(conn, device, "entrance", now=now - timedelta(minutes=20), minutes=30)
    newer = history_event("newer", now - timedelta(minutes=2))
    later_arrival = history_event("older-but-late", now - timedelta(minutes=10))

    client = FakeRingClient([[newer]])
    poller = RingPoller(client=client)
    poller.poll_once(conn)
    assert metrics.visit_count(conn) == 1

    client.pages = [[newer, later_arrival]]      # Ring now shows an older event it had not returned
    poller.poll_once(conn)

    assert metrics.visit_count(conn) == 2
    assert conn.execute("SELECT count(*) AS n FROM raw_event").fetchone()["n"] == 2
