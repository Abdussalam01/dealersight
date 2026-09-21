#!/usr/bin/env python3
"""Phase 0 spike: list Ring devices and save raw event history payloads.

Reads RING_ACCESS_TOKEN from .env (Developer Playground token) so the token
never appears in shell history. Saves one JSON file per call to
docs/ring-sample-payloads/ for designing the DealerSight event schema.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "ring-sample-payloads"

load_dotenv(ROOT / ".env")
API_BASE = os.getenv("RING_API_BASE", "https://api.amazonvision.com")
TOKEN = os.getenv("RING_ACCESS_TOKEN")


def get(path, params=None):
    response = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        params=params,
        timeout=15,
    )
    if response.status_code == 401:
        sys.exit("401 Unauthorized: Playground token expired or invalid. Generate a new one and update .env.")
    response.raise_for_status()
    return response.json()


def save(name, data):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"{stamp}_{name}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"  saved {path.relative_to(ROOT)}")


def main():
    if not TOKEN:
        sys.exit("RING_ACCESS_TOKEN is not set. Copy .env.example to .env and paste a Playground token.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    devices = get("/v1/devices")
    save("devices", devices)
    print(f"\n{len(devices.get('data', []))} device(s):")

    for device in devices.get("data", []):
        device_id = device["id"]
        name = device.get("attributes", {}).get("name", "Unknown")
        print(f"\n• {name}")

        save(f"capabilities_{name}", get(f"/v1/devices/{device_id}/capabilities"))

        history = get(f"/v1/history/devices/{device_id}/events")
        save(f"events_{name}", history)
        for event in history.get("data", []):
            attrs = event.get("attributes", {})
            print(f"    {attrs.get('event_type', '?'):<20} start={attrs.get('start', '')}  id={event.get('id')}")

    print("\nReview saved files before committing: remove any account IDs or personal data.")


if __name__ == "__main__":
    main()
