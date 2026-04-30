#!/usr/bin/env python3
"""
DataCore SANsymphony -> InfluxDB collector.

Reads ``collector.ini`` (location overridable via $DATACORE_CONFIG) and
periodically pulls performance counters from the DataCore REST API for
every enabled resource category, then writes them to InfluxDB as one
measurement per category.

Tags:    category, resource_id, resource_name, plus a few descriptive
         attributes copied from the resource (caption, hostname, etc.)
Fields:  every numeric counter returned by /performance for that object.
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from lib.datacore_client import (
    PARAMETERIZED_RESOURCES,
    PERFORMANCE_CATEGORIES,
    DataCoreClient,
    DataCoreError,
    parse_datacore_date_to_ms,
)
from lib.filters import CategoryFilter
from lib.influx_writer import InfluxWriter, LineProtocolPoint


LOGGER = logging.getLogger("datacore_collector")

STATUS_PATH = Path(os.environ.get("DATACORE_STATUS_FILE", "/app/status.json"))

# Resource keys we want as descriptive tags (copied verbatim from the
# resource object). Anything numeric in the perf response becomes a field.
RESOURCE_TAG_KEYS = (
    "Caption",
    "ExtendedCaption",
    "Alias",
    "HostName",
    "ServerName",
    "GroupName",
    "PoolName",
)

# Keys we never emit as fields (metadata or values handled separately).
PERF_SKIP_KEYS = frozenset({"__type", "ExtendedCaption", "Caption", "Id"})


# ---------------------------------------------------------------------- #
# Configuration loading
# ---------------------------------------------------------------------- #
def _expand_env(value: Optional[str]) -> str:
    return os.path.expandvars(value) if value else ""


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def build_client(cfg: configparser.ConfigParser) -> DataCoreClient:
    section = cfg["datacore"]
    api_version = section.get("api_version", fallback="").strip() or None
    return DataCoreClient(
        rest_host=_expand_env(section.get("rest_host")),
        server_host=_expand_env(section.get("server_host")),
        username=_expand_env(section.get("username")),
        password=_expand_env(section.get("password")),
        scheme=section.get("scheme", fallback="https"),
        verify_tls=section.getboolean("verify_tls", fallback=False),
        timeout=section.getfloat("timeout", fallback=30.0),
        api_version=api_version,
    )


def build_writer(cfg: configparser.ConfigParser) -> InfluxWriter:
    section = cfg["influxdb"]
    writer = InfluxWriter(
        url=_expand_env(section.get("url")),
        database=_expand_env(section.get("database", fallback="datacore")),
        username=_expand_env(section.get("username", fallback="")) or None,
        password=_expand_env(section.get("password", fallback="")) or None,
        retention_policy=section.get("retention_policy", fallback=None) or None,
        timeout=section.getfloat("timeout", fallback=30.0),
        batch_size=section.getint("batch_size", fallback=500),
    )
    if section.getboolean("create_database", fallback=False):
        writer.ensure_database()
    return writer


def build_filters(cfg: configparser.ConfigParser) -> Dict[str, CategoryFilter]:
    """Build a filter for each known performance category.

    Categories without a section default to disabled.
    """
    filters: Dict[str, CategoryFilter] = {}
    for category in PERFORMANCE_CATEGORIES:
        if cfg.has_section(category):
            filters[category] = CategoryFilter.from_section(cfg[category])
        else:
            filters[category] = CategoryFilter(enabled=False)
    return filters


# ---------------------------------------------------------------------- #
# Resource -> point conversion
# ---------------------------------------------------------------------- #
def _resource_display_name(resource: Dict[str, Any]) -> str:
    for key in ("Caption", "Alias", "ExtendedCaption", "HostName", "Name"):
        value = resource.get(key)
        if value:
            return str(value)
    return resource.get("Id", "")


def _build_tags(category: str, resource: Dict[str, Any]) -> Dict[str, str]:
    tags: Dict[str, str] = {
        "category": category,
        "resource_id": str(resource.get("Id", "")),
        "resource_name": _resource_display_name(resource),
    }
    for key in RESOURCE_TAG_KEYS:
        value = resource.get(key)
        if value:
            tags[key.lower()] = str(value)
    for key in ("HostId", "ServerId", "ServerHostId"):
        value = resource.get(key)
        if value:
            tags[key.lower()] = str(value)
    return tags


def _build_fields(
    perf: Dict[str, Any],
    counter_allowed: Callable[[str], bool],
) -> Tuple[Dict[str, Any], Optional[int]]:
    fields: Dict[str, Any] = {}
    timestamp_ms: Optional[int] = None

    for key, value in perf.items():
        if key == "CollectionTime":
            timestamp_ms = parse_datacore_date_to_ms(value)
            continue
        if key in PERF_SKIP_KEYS:
            continue
        # ``bool`` is a subclass of ``int`` — exclude it explicitly so we
        # don't store noisy boolean fields. The user can still opt them
        # in by adjusting the filter rules.
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and counter_allowed(key):
            fields[key] = value

    return fields, timestamp_ms


# ---------------------------------------------------------------------- #
# Per-category collection
# ---------------------------------------------------------------------- #
def _empty_stats(enabled: bool) -> Dict[str, Any]:
    return {
        "enabled": enabled,
        "resources_seen": 0,
        "resources_kept": 0,
        "points_written": 0,
        "error": None,
    }


def _list_resources_for(
    client: DataCoreClient,
    category: str,
    pool_ids: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """List resources, including special-cased categories needing parameters."""
    if category in PARAMETERIZED_RESOURCES:
        param_name, source_cat, real_endpoint = PARAMETERIZED_RESOURCES[category]
        if source_cat == "pools":
            if not pool_ids:
                raise DataCoreError(
                    f"/{category} requires {param_name}=<id> "
                    "but no pool ids are available"
                )
            merged: List[Dict[str, Any]] = []
            for pool_id in pool_ids:
                try:
                    merged.extend(
                        client.list_resources(
                            real_endpoint, params={param_name: pool_id}
                        )
                    )
                except DataCoreError as exc:
                    LOGGER.warning(
                        "Could not list /%s for %s=%s: %s",
                        real_endpoint,
                        param_name,
                        pool_id,
                        exc,
                    )
            return merged
        # Other parameterized resources would go here.
    return client.list_resources(category)


def collect_category(
    client: DataCoreClient,
    writer: InfluxWriter,
    category: str,
    cat_filter: CategoryFilter,
    pool_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Collect one category. Returns a stats dict the runner aggregates."""
    stats = _empty_stats(cat_filter.enabled)
    if not cat_filter.enabled:
        return stats

    LOGGER.info("Collecting category=%s", category)
    try:
        resources = _list_resources_for(client, category, pool_ids)
    except DataCoreError as exc:
        LOGGER.warning("Could not list /%s: %s", category, exc)
        stats["error"] = str(exc)[:200]
        return stats

    stats["resources_seen"] = len(resources)

    selected = [
        r for r in resources if cat_filter.name_allowed(_resource_display_name(r))
    ]
    stats["resources_kept"] = len(selected)
    LOGGER.info(
        "Category %s: %d/%d resources after name filter",
        category,
        len(selected),
        len(resources),
    )
    if not selected:
        return stats

    measurement = f"datacore_{category}"
    counter_allowed = cat_filter.counter_allowed
    points: List[LineProtocolPoint] = []

    for resource, perf in client.iter_performance(category, selected):
        fields, ts_ms = _build_fields(perf, counter_allowed)
        if not fields:
            continue
        points.append(
            LineProtocolPoint(
                measurement=measurement,
                tags=_build_tags(category, resource),
                fields=fields,
                timestamp=ts_ms,
            )
        )

    if points:
        writer.write(points)
        stats["points_written"] = len(points)
    else:
        LOGGER.info("No points to write for category %s", category)
    return stats


# ---------------------------------------------------------------------- #
# Main loop
# ---------------------------------------------------------------------- #
def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Runner:
    def __init__(self, cfg: configparser.ConfigParser, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.client = build_client(cfg)
        self.writer = build_writer(cfg)
        self.filters = build_filters(cfg)
        self.interval = cfg["collector"].getint("interval_seconds", fallback=30)
        self._stop = False
        self._reload_requested = False
        self._started_at = _utc_iso()
        self._cycle_count = 0
        self._last_cycle: Optional[Dict[str, Any]] = None
        self._write_status(state="starting")

        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._handle_reload)

    # -- signal handlers ----------------------------------------------- #
    def _handle_stop(self, *_: Any) -> None:
        LOGGER.info("Stop signal received; finishing current cycle")
        self._stop = True

    def _handle_reload(self, *_: Any) -> None:
        LOGGER.info(
            "SIGHUP received; will reload configuration after current cycle"
        )
        self._reload_requested = True

    def _reload_config(self) -> None:
        try:
            new_cfg = load_config(self.config_path)
            self.cfg = new_cfg
            self.client = build_client(new_cfg)
            self.writer = build_writer(new_cfg)
            self.filters = build_filters(new_cfg)
            self.interval = new_cfg["collector"].getint(
                "interval_seconds", fallback=30
            )
            LOGGER.info(
                "Configuration reloaded (%d enabled categories, interval=%ss)",
                sum(1 for f in self.filters.values() if f.enabled),
                self.interval,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Configuration reload failed; keeping previous settings")
        finally:
            self._reload_requested = False

    # -- status export ------------------------------------------------- #
    def _write_status(
        self,
        *,
        state: str,
        next_cycle_at: Optional[str] = None,
    ) -> None:
        payload = {
            "state": state,
            "started_at": self._started_at,
            "updated_at": _utc_iso(),
            "interval_seconds": self.interval,
            "cycle_count": self._cycle_count,
            "next_cycle_at": next_cycle_at,
            "enabled_categories": sorted(
                k for k, f in self.filters.items() if f.enabled
            ),
            "last_cycle": self._last_cycle,
            "config_path": str(self.config_path),
        }
        try:
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Direct write — works with both Docker volumes and bind-mounts.
            # The reader (setup UI) tolerates partial writes by treating a
            # JSONDecodeError as "not yet ready".
            STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            # Warn once, then debug — avoids spamming logs if the volume
            # is misconfigured but lets the issue surface immediately.
            if not getattr(self, "_status_write_warned", False):
                LOGGER.warning(
                    "Could not write status file %s: %s "
                    "(check volume ownership; collector runs as UID 10001)",
                    STATUS_PATH,
                    exc,
                )
                self._status_write_warned = True
            else:
                LOGGER.debug("Could not write status file: %s", exc)

    # -- collection cycle --------------------------------------------- #
    def _ordered_categories(self) -> List[Tuple[str, CategoryFilter]]:
        """Categories sorted so that prerequisite ones (pools) run first."""
        prerequisites = {src for _, src, _ep in PARAMETERIZED_RESOURCES.values()}
        return sorted(
            self.filters.items(),
            key=lambda kv: 0 if kv[0] in prerequisites else 1,
        )

    def _maybe_fetch_pool_ids(self, pool_ids: List[str]) -> List[str]:
        """Get pool ids even if the 'pools' category is disabled.

        Some categories (poollogicaldisks) need a pool=<id> parameter to
        be listable.
        """
        if pool_ids:
            return pool_ids
        try:
            return [
                r["Id"]
                for r in self.client.list_resources("pools")
                if r.get("Id")
            ]
        except DataCoreError as exc:
            LOGGER.debug("Could not pre-fetch pool ids: %s", exc)
            return []

    def run_once(self) -> Dict[str, Any]:
        cycle_started = time.monotonic()
        cycle_started_iso = _utc_iso()
        per_category: Dict[str, Dict[str, Any]] = {}
        total_points = 0
        pool_ids: List[str] = []

        for category, cat_filter in self._ordered_categories():
            if self._stop:
                break
            if any(
                src == category for _, src, _ep in PARAMETERIZED_RESOURCES.values()
            ):
                # Prerequisite category: collect its pool ids regardless of
                # whether it's enabled, so we can feed dependent categories.
                pool_ids = self._maybe_fetch_pool_ids(pool_ids)

            try:
                stats = collect_category(
                    self.client,
                    self.writer,
                    category,
                    cat_filter,
                    pool_ids=pool_ids,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Unexpected error while collecting %s", category)
                stats = _empty_stats(cat_filter.enabled)
                stats["error"] = f"unexpected: {exc!r}"[:200]

            per_category[category] = stats
            total_points += stats["points_written"]

        cycle_summary = {
            "started_at": cycle_started_iso,
            "duration_seconds": round(time.monotonic() - cycle_started, 2),
            "total_points": total_points,
            "categories": per_category,
        }
        self._cycle_count += 1
        self._last_cycle = cycle_summary
        return cycle_summary

    def run_forever(self) -> None:
        LOGGER.info(
            "Starting collector (interval=%ss, %d enabled categories)",
            self.interval,
            sum(1 for f in self.filters.values() if f.enabled),
        )
        self._write_status(state="running")
        while not self._stop:
            cycle_start = time.monotonic()
            self.run_once()
            if self._stop:
                break
            if self._reload_requested:
                self._reload_config()

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, self.interval - elapsed)
            next_at = datetime.now(timezone.utc).timestamp() + sleep_for
            next_iso = datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            LOGGER.info("Cycle done in %.1fs, sleeping %.1fs", elapsed, sleep_for)
            self._write_status(state="idle", next_cycle_at=next_iso)

            # Sleep in small chunks so SIGTERM/SIGHUP are responsive.
            end = time.monotonic() + sleep_for
            while not self._stop and time.monotonic() < end:
                if self._reload_requested:
                    self._reload_config()
                time.sleep(min(1.0, end - time.monotonic()))
            self._write_status(state="running")
        self._write_status(state="stopped")
        LOGGER.info("Collector stopped")


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #
def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("DATACORE_CONFIG", "collector.ini")),
        help="Path to collector configuration (default: collector.ini)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single collection cycle then exit (useful for cron / tests).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Log level: DEBUG, INFO, WARNING, ERROR.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    cfg = load_config(args.config)
    runner = Runner(cfg, args.config)
    if args.once:
        runner.run_once()
    else:
        runner.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())