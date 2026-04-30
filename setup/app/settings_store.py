"""
Persistence helpers for the setup UI.

The setup service owns two files:
  - /config/collector.ini   (full collector configuration)
  - /config/.env            (only the secrets/values consumed by docker-compose)

We treat collector.ini as the single source of truth for everything the
collector needs at runtime. The .env file is updated in lockstep so a
``docker compose up -d`` keeps producing the same configuration.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


# All resource categories supported by the collector. Kept here as a single
# source of truth for the UI (so we do not need to import the collector code).
PERFORMANCE_CATEGORIES: List[str] = [
    "servers",
    "servergroups",
    "pools",
    "poolmembers",
    "poollogicaldisks",
    "physicaldisks",
    "sharedpools",
    "sharedphysicaldisks",
    "virtualdisks",
    "virtualdiskgroups",
    "virtuallogicalunits",
    "logicaldisks",
    "hosts",
    "hostgroups",
    "scsiports",
    "targetdevices",
    "targetdomains",
    "snapshots",
    "snapshotgroups",
    "rollbackgroups",
]


@dataclass
class CategoryConfig:
    enabled: bool = False
    include_names: str = ""
    exclude_names: str = ""
    include_counters: str = ""
    exclude_counters: str = ""


@dataclass
class Settings:
    # DataCore
    dcs_rest_host: str = ""
    dcs_server_host: str = ""
    dcs_username: str = ""
    dcs_password: str = ""
    dcs_scheme: str = "https"
    dcs_verify_tls: bool = False
    dcs_api_version: str = "1.0"

    # InfluxDB (collector side)
    influx_url: str = "http://influxdb:8086"
    influx_db: str = "datacore"
    influx_user: str = "datacore"
    influx_password: str = ""
    influx_create_db: bool = False

    # Collector
    interval_seconds: int = 30
    log_level: str = "INFO"

    # Categories
    categories: Dict[str, CategoryConfig] = field(default_factory=dict)


# ----------------------------------------------------------------------
# .env helpers
# ----------------------------------------------------------------------
_ENV_LINE_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def read_env(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if match:
            key, value = match.group(1), match.group(2)
            # Strip wrapping quotes if present.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            out[key] = value
    return out


def write_env(path: Path, values: Dict[str, str]) -> None:
    """Update only the keys we own; preserve unknown keys already present."""
    existing = read_env(path) if path.exists() else {}
    existing.update(values)
    lines = ["# Managed by setup UI — keys are merged, comments not preserved."]
    for key in sorted(existing):
        val = existing[key]
        # Quote the value if it contains spaces or special characters.
        if any(c in val for c in " #\"'$"):
            val_escaped = val.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{val_escaped}"')
        else:
            lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------
# collector.ini helpers
# ----------------------------------------------------------------------
def load_settings(ini_path: Path) -> Settings:
    """Load settings from ``collector.ini``; missing keys fall back to defaults."""
    cfg = configparser.ConfigParser(interpolation=None)
    if ini_path.exists():
        cfg.read(ini_path, encoding="utf-8")

    s = Settings()

    if cfg.has_section("datacore"):
        d = cfg["datacore"]
        s.dcs_rest_host = d.get("rest_host", s.dcs_rest_host)
        s.dcs_server_host = d.get("server_host", s.dcs_server_host)
        s.dcs_username = d.get("username", s.dcs_username)
        s.dcs_password = d.get("password", s.dcs_password)
        s.dcs_scheme = d.get("scheme", s.dcs_scheme)
        s.dcs_verify_tls = d.getboolean("verify_tls", fallback=s.dcs_verify_tls)
        s.dcs_api_version = d.get("api_version", s.dcs_api_version)

    if cfg.has_section("influxdb"):
        i = cfg["influxdb"]
        s.influx_url = i.get("url", s.influx_url)
        s.influx_db = i.get("database", s.influx_db)
        s.influx_user = i.get("username", s.influx_user)
        s.influx_password = i.get("password", s.influx_password)
        s.influx_create_db = i.getboolean(
            "create_database", fallback=s.influx_create_db
        )

    if cfg.has_section("collector"):
        c = cfg["collector"]
        s.interval_seconds = c.getint("interval_seconds", fallback=s.interval_seconds)

    s.log_level = os.environ.get("LOG_LEVEL", s.log_level)

    # Categories: if the section is missing, default to disabled.
    for cat in PERFORMANCE_CATEGORIES:
        if cfg.has_section(cat):
            section = cfg[cat]
            s.categories[cat] = CategoryConfig(
                enabled=section.getboolean("enabled", fallback=False),
                include_names=section.get("include_names", ""),
                exclude_names=section.get("exclude_names", ""),
                include_counters=section.get("include_counters", ""),
                exclude_counters=section.get("exclude_counters", ""),
            )
        else:
            s.categories[cat] = CategoryConfig(enabled=False)

    return s


def _resolve_envvar_refs(settings: Settings) -> Dict[str, str]:
    """Build the ``.env`` payload from the settings.

    The collector.ini contains literal values directly (no env-var
    interpolation needed at runtime), but docker-compose.yml still passes
    a few values via environment variables (DCS*, INFLUX_*, LOG_LEVEL),
    so we keep them in sync.
    """
    return {
        "DCSREST": settings.dcs_rest_host,
        "DCSSVR": settings.dcs_server_host,
        "DCSUNAME": settings.dcs_username,
        "DCSPWORD": settings.dcs_password,
        "INFLUX_DB": settings.influx_db,
        "INFLUX_USER": settings.influx_user,
        "INFLUX_PASSWORD": settings.influx_password,
        "LOG_LEVEL": settings.log_level,
    }


def save_settings(settings: Settings, ini_path: Path, env_path: Path) -> None:
    """Write the settings atomically to ``collector.ini`` and ``.env``."""
    cfg = configparser.ConfigParser(interpolation=None)

    cfg["datacore"] = {
        "rest_host": settings.dcs_rest_host,
        "server_host": settings.dcs_server_host,
        "username": settings.dcs_username,
        "password": settings.dcs_password,
        "scheme": settings.dcs_scheme,
        "verify_tls": "true" if settings.dcs_verify_tls else "false",
        "timeout": "30",
        "api_version": settings.dcs_api_version,
    }
    cfg["influxdb"] = {
        "url": settings.influx_url,
        "database": settings.influx_db,
        "username": settings.influx_user,
        "password": settings.influx_password,
        "create_database": "true" if settings.influx_create_db else "false",
        "batch_size": "500",
        "timeout": "30",
    }
    cfg["collector"] = {
        "interval_seconds": str(settings.interval_seconds),
    }

    for cat in PERFORMANCE_CATEGORIES:
        c = settings.categories.get(cat) or CategoryConfig()
        section = {"enabled": "true" if c.enabled else "false"}
        if c.include_names:
            section["include_names"] = c.include_names
        if c.exclude_names:
            section["exclude_names"] = c.exclude_names
        if c.include_counters:
            section["include_counters"] = c.include_counters
        if c.exclude_counters:
            section["exclude_counters"] = c.exclude_counters
        cfg[cat] = section

    # Atomic write: temp file + rename.
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ini_path.with_suffix(ini_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("; Managed by the setup UI. Manual edits will be preserved\n")
        fh.write("; for unknown keys but standard keys may be overwritten.\n\n")
        cfg.write(fh)
    os.replace(tmp, ini_path)

    write_env(env_path, _resolve_envvar_refs(settings))
