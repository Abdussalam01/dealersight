#!/usr/bin/env python3
"""Friction-log evidence (FL-02): does the Event History `event_types` filter change results?

Uses the documented syntax (comma-separated, dot-delimited subtypes) plus control values.
Writes a sanitized result (no token, no device ID) to docs/evidence/.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
API_BASE = os.getenv("RING_API_BASE", "https://api.amazonvision.com")
HEADERS = {"Authorization": f"Bearer {os.getenv('RING_ACCESS_TOKEN')}"}

FILTERS = [
    None,                           # baseline
    "motion.human,motion.vehicle",  # documented composite example
    "motion",
    "ding",                         # control: no doorbell presses were made
    "on_demand",
    "not_a_real_type",              # control: invalid value
]

devices = requests.get(f"{API_BASE}/v1/devices", headers=HEADERS, timeout=15)
if devices.status_code == 401:
    sys.exit("401 Unauthorized: Playground token expired. Generate a new one and update .env.")
devices.raise_for_status()
device_id = devices.json()["data"][0]["id"]

results = []
for value in FILTERS:
    params = {"event_types": value} if value else {}
    response = requests.get(f"{API_BASE}/v1/history/devices/{device_id}/events", headers=HEADERS, params=params, timeout=15)
    body = response.json() if response.ok else {}
    events = body.get("data", [])
    results.append({
        "query": f"event_types={value}" if value else "(no filter)",
        "http_status": response.status_code,
        "events_returned": len(events),
        "event_types_in_response": sorted({e["attributes"]["event_type"] for e in events}),
    })
    print(results[-1])

out = ROOT / "docs" / "evidence" / "FL-02_event_types_filter_test.json"
out.write_text(json.dumps({
    "_note": "FL-02 controlled test. GET /v1/history/devices/{device_id}/events with a Ring Developer Playground token. Token and device ID omitted.",
    "tested_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "results": results,
}, indent=2))
print(f"\nsaved {out.relative_to(ROOT)}")
