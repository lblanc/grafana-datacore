"""
Connectivity tests for DataCore and InfluxDB.

These helpers make a few HTTP requests and return ``(ok, message)``.
They are intentionally tolerant: anything not 2xx is reported back so
the user can diagnose without digging through logs.

For DataCore we re-use the production client (with auto-detection of
the API base) so the test mirrors the real behaviour 1:1.
"""

from __future__ import annotations

import logging
from typing import Tuple

import requests

from .settings_store import Settings

LOGGER = logging.getLogger(__name__)


def test_datacore(s: Settings, timeout: float = 10.0) -> Tuple[bool, str]:
    if not s.dcs_rest_host:
        return False, "DataCore REST host is required."
    if not s.dcs_username:
        return False, "DataCore username is required."

    # Lazy-import so the setup container does not need the collector path.
    try:
        from .datacore_client import DataCoreClient
    except ImportError:
        # Fallback: setup ships its own copy.
        from lib.datacore_client import DataCoreClient  # type: ignore

    try:
        client = DataCoreClient(
            rest_host=s.dcs_rest_host,
            server_host=s.dcs_server_host or s.dcs_rest_host,
            username=s.dcs_username,
            password=s.dcs_password,
            scheme=s.dcs_scheme,
            verify_tls=s.dcs_verify_tls,
            timeout=timeout,
            api_version=(s.dcs_api_version or None),
            max_retries=0,
        )
    except ValueError as exc:
        return False, str(exc)

    return client.probe()


def test_influx(s: Settings, timeout: float = 10.0) -> Tuple[bool, str]:
    if not s.influx_url:
        return False, "InfluxDB URL is required."

    base = s.influx_url.rstrip("/")
    ping_url = f"{base}/ping"
    try:
        response = requests.get(ping_url, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"Network error reaching {ping_url}: {exc}"

    if response.status_code not in (200, 204):
        return False, f"HTTP {response.status_code} on /ping."

    version = response.headers.get("X-Influxdb-Version", "?")

    # If credentials are set, validate them with a lightweight query.
    if s.influx_user:
        try:
            r = requests.get(
                f"{base}/query",
                params={"q": "SHOW DATABASES"},
                auth=(s.influx_user, s.influx_password),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return False, f"Auth check failed: {exc}"
        if r.status_code in (401, 403):
            return False, f"Authentication rejected ({r.status_code})."
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"

    return True, f"OK — InfluxDB {version} reachable."
