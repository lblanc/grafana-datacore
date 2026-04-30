"""
Minimal InfluxDB 1.x writer using line protocol over HTTP.

We deliberately avoid the deprecated ``influxdb`` Python client and stick
with ``requests`` to keep the dependency footprint small.

The point timestamp's precision is whatever the writer is configured to
send to the InfluxDB ``/write`` endpoint (defaults to ``ms``). The
collector currently passes timestamps in milliseconds.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Union

import requests


LOGGER = logging.getLogger(__name__)

Number = Union[int, float, bool]


# ---- escaping helpers (per InfluxDB line protocol) -------------------- #
def _escape_tag(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(" ", "\\ ")
        .replace("=", "\\=")
    )


def _escape_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _format_field(value: object) -> Optional[str]:
    """Render a Python value to its line-protocol field representation."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool must be checked before int (bool is a subclass of int).
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    # Strings are skipped: rarely useful as fields, and the user can put
    # descriptive text in tags instead.
    return None


# ---- point + writer --------------------------------------------------- #
class LineProtocolPoint:
    __slots__ = ("measurement", "tags", "fields", "timestamp")

    def __init__(
        self,
        measurement: str,
        tags: Dict[str, str],
        fields: Dict[str, Number],
        timestamp: Optional[int] = None,
    ) -> None:
        self.measurement = measurement
        self.tags = tags
        self.fields = fields
        self.timestamp = timestamp

    def to_line(self) -> Optional[str]:
        encoded_fields: List[str] = []
        for key, value in self.fields.items():
            formatted = _format_field(value)
            if formatted is None:
                continue
            encoded_fields.append(f"{_escape_tag(key)}={formatted}")
        if not encoded_fields:
            return None

        head = _escape_measurement(self.measurement)
        if self.tags:
            tag_pairs = ",".join(
                f"{_escape_tag(str(k))}={_escape_tag(str(v))}"
                for k, v in sorted(self.tags.items())
                if v not in (None, "")
            )
            if tag_pairs:
                head = f"{head},{tag_pairs}"
        line = f"{head} {','.join(encoded_fields)}"
        if self.timestamp is not None:
            line = f"{line} {self.timestamp}"
        return line


class InfluxWriter:
    """Buffered writer for InfluxDB 1.x using HTTP line protocol."""

    def __init__(
        self,
        url: str,
        database: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        retention_policy: Optional[str] = None,
        precision: str = "ms",
        timeout: float = 30.0,
        batch_size: int = 500,
    ) -> None:
        if not url:
            raise ValueError("InfluxDB url is required")
        if not database:
            raise ValueError("InfluxDB database is required")

        base = url.rstrip("/")
        self.write_url = f"{base}/write"
        self.query_url = f"{base}/query"
        self.database = database
        self.retention_policy = retention_policy
        self.precision = precision
        self.timeout = timeout
        self.batch_size = batch_size

        self._auth = (username, password) if username else None
        self._session = requests.Session()

    def ensure_database(self) -> None:
        """Create the target database if it does not already exist."""
        params = {"q": f'CREATE DATABASE "{self.database}"'}
        try:
            response = self._session.post(
                self.query_url,
                params=params,
                auth=self._auth,
                timeout=self.timeout,
            )
            response.raise_for_status()
            LOGGER.info("Ensured InfluxDB database '%s' exists", self.database)
        except requests.RequestException as exc:
            LOGGER.warning(
                "Could not ensure database '%s': %s", self.database, exc
            )

    def write(self, points: Iterable[LineProtocolPoint]) -> int:
        """Write points in batches; return the number of lines actually sent."""
        buffer: List[str] = []
        sent = 0
        for point in points:
            line = point.to_line()
            if line is None:
                continue
            buffer.append(line)
            if len(buffer) >= self.batch_size:
                if self._flush(buffer):
                    sent += len(buffer)
                buffer = []
        if buffer and self._flush(buffer):
            sent += len(buffer)
        if sent:
            LOGGER.info(
                "Wrote %d points to InfluxDB database '%s'", sent, self.database
            )
        return sent

    def _flush(self, lines: List[str]) -> bool:
        body = "\n".join(lines).encode("utf-8")
        params = {"db": self.database, "precision": self.precision}
        if self.retention_policy:
            params["rp"] = self.retention_policy
        try:
            response = self._session.post(
                self.write_url,
                params=params,
                data=body,
                auth=self._auth,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            LOGGER.error("InfluxDB write failed: %s", exc)
            return False

        if response.status_code >= 400:
            LOGGER.error(
                "InfluxDB rejected batch (%s): %s",
                response.status_code,
                response.text[:300],
            )
            return False
        return True
