"""Ring Partner API client (https://api.amazonvision.com).

All calls are server-side: Ring blocks browser requests with CORS.
DealerSight never requests live view, clips, or snapshots, because those
create `on_demand` events that would pollute the demo's event history.
"""

import time

import requests

from backend import config


class RingAuthError(Exception):
    """Token missing, expired, or rejected (401). Playground tokens last ~30 minutes."""


class RingClient:
    def __init__(self, token=None, api_base=None, session=None):
        self.token = token
        self.api_base = api_base or config.RING_API_BASE
        self.session = session or requests.Session()

    def _get(self, path_or_url, params=None):
        token = self.token or config.ring_access_token()
        if not token:
            raise RingAuthError("RING_ACCESS_TOKEN is not set in .env")
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_base}{path_or_url}"
        for attempt in range(3):
            response = self.session.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
            if response.status_code == 401:
                raise RingAuthError("Ring token expired or invalid: generate a new Playground token")
            if response.status_code == 429 and attempt < 2:
                time.sleep(int(response.headers.get("Retry-After", 1)))
                continue
            if response.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        response.raise_for_status()

    def list_devices(self):
        """GET /v1/devices. Returns JSON:API device resources."""
        return self._get("/v1/devices", params={"include": "status"}).get("data", [])

    def event_history_page(self, device_id, next_link=None):
        """GET /v1/history/devices/{id}/events, newest first. Returns (events, next_link)."""
        if next_link:
            body = self._get(next_link)
        else:
            body = self._get(f"/v1/history/devices/{device_id}/events")
        return body.get("data", []), body.get("links", {}).get("next")
