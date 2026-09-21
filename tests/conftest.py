import os
from datetime import datetime, timezone

import psycopg
import pytest

from backend import db, ingest

ADMIN_URL = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://dealersight:dealersight@localhost:5433/postgres")
TEST_URL = os.getenv("TEST_DATABASE_URL", "postgresql://dealersight:dealersight@localhost:5433/dealersight_test")

# A fixed "now" so tests never depend on the wall clock.
T0 = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
RING_DEVICE = "ava1.ring.device.TEST"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = 'dealersight_test'").fetchone():
            admin.execute("CREATE DATABASE dealersight_test")


@pytest.fixture
def conn():
    with db.connect(TEST_URL) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.init_schema(conn)
        ingest.start_session(conn, now=T0)
        yield conn


@pytest.fixture
def device_id(conn):
    return ingest.ensure_device(conn, RING_DEVICE, "Playground Device", mode="demo")


def history_event(event_id, start, seconds=30, event_type="on_demand", device=RING_DEVICE):
    """A Ring Event History record in the shape the Playground returns."""
    start_ms = int(start.timestamp() * 1000)
    return {
        "type": "history-events",
        "id": event_id,
        "attributes": {"event_type": event_type, "start": start_ms, "end": start_ms + seconds * 1000,
                       "is_third_party_reviewed": True},
        "relationships": {"source": {"data": {"type": "devices", "id": device}}, "cv_detections": {"data": []}},
        "meta": {"riid": None},
    }
