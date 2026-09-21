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


def test_repeated_polls_ingest_each_event_once(conn):
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


def test_later_polls_stop_at_first_known_event(conn):
    now = ingest.utcnow()
    ingest.start_session(conn, now=now - timedelta(hours=2))
    ingest.ensure_device(conn, RING_DEVICE, "Playground Device", mode="demo")
    pages = [[history_event(f"e{p}{i}", now - timedelta(minutes=p * 10 + i)) for i in range(3)] for p in range(3)]
    client = FakeRingClient(pages)
    poller = RingPoller(client=client)

    poller.poll_once(conn)  # initial sync walks all pages
    assert client.page_requests == 3

    client.page_requests = 0
    poller.poll_once(conn)  # first event is already known: one page, then stop
    assert client.page_requests == 1
