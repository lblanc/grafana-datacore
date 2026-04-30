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
# Characters that InfluxDB chokes on inside tag values even when escaped.
# DataCore IDs of the form "serverGUID:{poolGUID}" trip the parser because
# of the colon and braces; we replace them with underscores. We also
# strip control characters and CR/LF that can sneak into resource names.
_TAG_INCOMPATIBLE = str.maketrans({":": "_", "{": "_", "}": "_"})


def _sanitize_tag_value(value: str) -> str:
    """Clean a string for safe use as a line-protocol tag value.

    Removes anything that isn't a printable ASCII char, replaces
    DataCore separators (``:`` ``{`` ``}``) with underscores, condenses
    runs of whitespace, and trims the result.
    """
    if not value:
        return ""
    # Drop control characters and non-printable bytes outright.
    cleaned = "".join(ch for ch in value if 32 <= ord(ch) < 127 or ch in "\t")
    cleaned = cleaned.translate(_TAG_INCOMPATIBLE)
    # Tabs and runs of spaces -> single space; trim.
    cleaned = " ".join(cleaned.split())
    return cleaned


def _escape_tag(value: str) -> str:
    safe = _sanitize_tag_value(value)
    return (
        safe.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(" ", "\\ ")
        .replace("=", "\\=")
    )


def _escape_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


# InfluxDB 1.x stores integers as signed int64. DataCore sometimes
# returns uint64 sentinel values (e.g. EstimatedDepletionTime = 2^64-1
# meaning "never"). Anything strictly outside the signed-int64 range
# would be rejected as "value out of range", so we fall back to a float
# representation for those.
_INT64_MAX = (1 << 63) - 1
_INT64_MIN = -(1 << 63)


def _format_field(value: object) -> Optional[str]:
    """Render a Python value to its line-protocol field representation."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool must be checked before int (bool is a subclass of int).
        return "true" if value else "false"
    if isinstance(value, int):
        if _INT64_MIN <= value <= _INT64_MAX:
            return f"{value}i"
        # Out of int64 range: skip rather than fall back to float.
        # Mixing int and float for the same field name across writes
        # makes InfluxDB reject subsequent batches with a type-mismatch
        # error. DataCore uses uint64 sentinels (2^64-1 = "never") for
        # counters like EstimatedDepletionTime; dropping these is fine
        # for charting purposes.
        return None
    if isinstance(value, float):
        # Avoid Python's "1.23e+18" form: line protocol rejects the '+'
        # sign in the exponent. Skip NaN/inf which are not valid values.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return repr(value).replace("e+", "e")
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
            rendered: List[str] = []
            for k, v in sorted(self.tags.items()):
                if v in (None, ""):
                    continue
                escaped_value = _escape_tag(str(v))
                if not escaped_value:
                    # Sanitization stripped everything; skip.
                    continue
                escaped_key = _escape_tag(str(k))
                if not escaped_key:
                    continue
                rendered.append(f"{escaped_key}={escaped_value}")
            if rendered:
                head = f"{head}," + ",".join(rendered)
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
            sample = lines[0] if lines else ""
            LOGGER.error(
                "InfluxDB rejected batch (%s) - first 500 chars of error: %s",
                response.status_code,
                response.text[:500],
            )
            # Dump the full request body and full Influx error to a file
            # so we can diagnose without the docker log line truncation.
            try:
                from pathlib import Path
                import os
                dump_dir = Path(os.environ.get("DATACORE_DUMP_DIR", "/app/dumps"))
                dump_dir.mkdir(parents=True, exist_ok=True)
                from datetime import datetime, timezone
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dump = dump_dir / f"rejected-{stamp}.txt"
                dump.write_text(
                    "=== InfluxDB error ===\n"
                    + response.text
                    + "\n\n=== Request body ===\n"
                    + body.decode("utf-8", errors="replace"),
                    encoding="utf-8",
                )
                LOGGER.error("Full rejected payload written to %s", dump)
            except OSError as exc:
                LOGGER.debug("Could not write rejected dump: %s", exc)
            return False
        return True