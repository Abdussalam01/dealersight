#!/usr/bin/env python3
"""Replay a deterministic scenario into the local database (for rehearsal, never shown as live).

    python scripts/replay.py scenario_a

Starts a new counting session three hours in the past, so current live counts restart.
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, ingest, metrics  # noqa: E402
from backend.replay import SCENARIOS, replay  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "scenario_a"
scenario = SCENARIOS[name]
now = ingest.utcnow().replace(second=0, microsecond=0)
base = now - timedelta(hours=3)

with db.connect() as conn:
    db.init_schema(conn)
    results, counts = replay(conn, scenario, base, run_id=base.strftime("%Y%m%dT%H%M"), now=now)
    summary = metrics.summary(conn)

print(f"{name}: {scenario['description']}")
print(f"ingested: {sum(r['status'] == 'accepted' for r in results)} accepted, "
      f"{sum(r['status'] == 'duplicate' for r in results)} duplicates")
print(f"derived:  {counts}")
print(f"expected: {scenario['expected']}")
print(f"open departures: {summary['open_departures']}")
