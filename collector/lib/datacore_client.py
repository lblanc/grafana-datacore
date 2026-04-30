"""
DataCore SANsymphony REST API client.

Reference: https://docs.datacore.com/RESTSupport-WebHelp/

Authentication uses two custom headers (DataCore non-standard):
  - ServerHost: name or IP of the DataCore server in the server group
  - Authorization: "Basic <username> <password>"   (NOT base64-encoded)

REST URL conventions vary across DataCore REST Support versions:
  - 1.06 and earlier: /RestService/rest.svc/<resource>
  - 2.0 / 2.01:       /RestService/rest.svc/<api_version>/<resource>
                      (only versions starting with "1.0")
  - 2.1+:             /RestService/rest.svc/<resource>      (versionless again)
                      with /performance always on the unversioned root.

To avoid hardcoding which variant the user has, the client detects the
correct base URL on demand: it tries the configured base first and falls
back to the alternative on a 404. The result is cached per endpoint so
the cost is paid once.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger(__name__)

# Resource categories that expose a /performance endpoint.
# Source: docs.datacore.com -> "Object Categories for Performance Counters"
PERFORMANCE_CATEGORIES: Dict[str, str] = {
    "servers": "DataCoreServer",
    "pools": "DiskPool",
    "hostgroups": "HostGroup",
    "hosts": "Host",
    "logicaldisks": "PassThroughLogicalDisk",
    "physicaldisks": "PhysicalDisk",
    "poolmembers": "PoolMember",
    "poollogicaldisks": "PoolLogicalDisk",
    "rollbackgroups": "RollbackGroup",
    "servergroups": "ServerGroup",
    "scsiports": "ScsiPort",
    "sharedpools": "SharedPool",
    "sharedphysicaldisks": "SharedPhysicalDisk",
    "snapshotgroups": "SnapshotGroup",
    "snapshots": "Snapshot",
    "virtualdiskgroups": "VirtualDiskGroup",
    "virtualdisks": "VirtualDisk",
    "virtuallogicalunits": "VirtualLogicalUnit",
    "targetdevices": "TargetDevice",
    "targetdomains": "TargetDomain",
}

# Resources that need extra query parameters and cannot be listed naked.
# Mapping: resource -> (param name, source category whose Ids feed it)
PARAMETERIZED_RESOURCES: Dict[str, Tuple[str, str]] = {
    "poollogicaldisks": ("pool", "pools"),
}

# DataCore returns timestamps like "/Date(1486402608775)/" or
# "/Date(1486402608775-0500)/".
_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/")


def parse_datacore_date_to_ms(value: Any) -> Optional[int]:
    """Convert a DataCore ``/Date(...)/`` string to epoch milliseconds."""
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value)
    if not match:
        return None
    return int(match.group(1))


class DataCoreError(Exception):
    """Raised when the DataCore REST API returns an unexpected response."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DataCoreClient:
    """Synchronous client for the DataCore SANsymphony REST API."""

    def __init__(
        self,
        rest_host: str,
        server_host: str,
        username: str,
        password: str,
        *,
        scheme: str = "https",
        verify_tls: bool = False,
        timeout: float = 30.0,
        api_version: Optional[str] = None,
        max_retries: int = 3,
    ) -> None:
        if not rest_host:
            raise ValueError("rest_host is required")
        if scheme not in ("http", "https"):
            raise ValueError("scheme must be 'http' or 'https'")

        self.rest_host = rest_host
        self.server_host = server_host or rest_host
        self.timeout = timeout
        self.scheme = scheme
        self.verify_tls = verify_tls

        # Both candidate base URLs. We try the user-configured one first
        # then fall back to the alternative if the endpoint returns 404.
        # The fallback is always available (in both directions) so the UI
        # can leave api_version empty and still discover REST Support
        # 2.0/2.01 builds that require the /1.0/ prefix.
        self._root_url = f"{scheme}://{rest_host}/RestService/rest.svc"
        # Default versioned URL used as the alternative: /1.0/ has been
        # the canonical version since REST 2.0.
        default_version = api_version.lstrip("/") if api_version else "1.0"
        self._versioned_url = f"{self._root_url}/{default_version}"
        if api_version:
            self._preferred_first = self._versioned_url
            self._fallback = self._root_url
        else:
            self._preferred_first = self._root_url
            self._fallback = self._versioned_url

        # Cache of resolved base URL per endpoint name.
        self._endpoint_base: Dict[str, str] = {}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "ServerHost": self.server_host,
                # DataCore "Basic Username Password" is non-standard (no base64).
                "Authorization": f"Basic {username} {password}",
                "Accept": "application/json",
            }
        )
        self.session.verify = verify_tls

        retry = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------ #
    # Low-level GET with base auto-detection
    # ------------------------------------------------------------------ #
    def _do_get(
        self, base: str, path: str, params: Optional[Dict[str, Any]]
    ) -> requests.Response:
        url = f"{base}/{path.lstrip('/')}"
        LOGGER.debug("GET %s params=%s", url, params)
        try:
            return self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DataCoreError(f"HTTP error calling {url}: {exc}") from exc

    def _resolve_base(
        self, endpoint: str, path: str, params: Optional[Dict[str, Any]]
    ) -> Tuple[str, requests.Response]:
        """Issue the request, falling back to the alternative base on 404.

        Returns ``(base_used, response)``. On any non-404 status, the first
        base wins — switching base would not help fix a 401 or a 400.
        """
        cached = self._endpoint_base.get(endpoint)
        if cached is not None:
            return cached, self._do_get(cached, path, params)

        response = self._do_get(self._preferred_first, path, params)
        if response.status_code == 404 and self._fallback:
            LOGGER.debug(
                "Endpoint /%s returned 404 on %s; trying %s",
                endpoint,
                self._preferred_first,
                self._fallback,
            )
            response = self._do_get(self._fallback, path, params)
            if response.status_code != 404:
                self._endpoint_base[endpoint] = self._fallback
                return self._fallback, response
            return self._fallback, response

        self._endpoint_base[endpoint] = self._preferred_first
        return self._preferred_first, response

    def _get_json(
        self,
        endpoint: str,
        path: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        path = path if path is not None else endpoint
        _base, response = self._resolve_base(endpoint, path, params)
        if response.status_code >= 400:
            raise DataCoreError(
                f"DataCore returned {response.status_code} for {response.url}: "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise DataCoreError(
                f"Could not decode JSON response from {response.url}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # High-level operations
    # ------------------------------------------------------------------ #
    def list_resources(
        self,
        category: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """List all instances of a resource category (e.g. ``virtualdisks``)."""
        data = self._get_json(category, params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("value"), list):
            return data["value"]
        raise DataCoreError(
            f"Unexpected response shape for /{category}: {type(data).__name__}"
        )

    def get_performance(self, object_id: str) -> List[Dict[str, Any]]:
        """Get performance counters for a single object id."""
        # Encode the id explicitly: DataCore docs warn some clients must do
        # it themselves, and it keeps the logged URL deterministic.
        encoded = quote(object_id, safe="")
        result = self._get_json("performance", path=f"performance?id={encoded}")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        raise DataCoreError(
            f"Unexpected performance response shape: {type(result).__name__}"
        )

    def iter_performance(
        self,
        category: str,
        resources: List[Dict[str, Any]],
    ) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Yield ``(resource, perf_entry)`` pairs.

        Errors on individual objects are logged and skipped instead of
        raising so a single bad object does not break the cycle.
        """
        for resource in resources:
            obj_id = resource.get("Id")
            if not obj_id:
                LOGGER.warning(
                    "Skipping %s entry without 'Id': %r", category, resource
                )
                continue
            try:
                perf_list = self.get_performance(obj_id)
            except DataCoreError as exc:
                LOGGER.warning(
                    "Failed to fetch performance for %s id=%s: %s",
                    category,
                    obj_id,
                    exc,
                )
                continue
            for entry in perf_list:
                yield resource, entry

    # ------------------------------------------------------------------ #
    # Connectivity helpers
    # ------------------------------------------------------------------ #
    def probe(self) -> Tuple[bool, str]:
        """Lightweight credentials/connectivity test.

        Used by the setup UI's "Test connection" button. Returns
        ``(ok, message)``.
        """
        try:
            servers = self.list_resources("servers")
        except DataCoreError as exc:
            if exc.status_code in (401, 403):
                return False, f"Authentication rejected ({exc.status_code})."
            return False, str(exc)
        # Mention which base resolved so the user can sanity-check.
        base = self._endpoint_base.get("servers", self._preferred_first)
        return True, f"OK — {len(servers)} server(s) reachable via {base}"