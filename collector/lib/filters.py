"""
Filtering helpers for resources and counters.

The configuration file describes, per category, a set of include/exclude
rules that select which resource instances and which counters should be
sent to InfluxDB.

Rules use shell-style globs (fnmatch) and are case-insensitive.
An empty include list means "everything"; exclude rules always win.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


def _parse_patterns(raw: str) -> List[str]:
    if not raw:
        return []
    parts = []
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _match_any(value: str, patterns: Sequence[str]) -> bool:
    if not value:
        return False
    lower = value.lower()
    return any(fnmatch.fnmatchcase(lower, p.lower()) for p in patterns)


@dataclass
class CategoryFilter:
    """Filtering rules for a single resource category."""

    enabled: bool = True
    include_names: List[str] = field(default_factory=list)
    exclude_names: List[str] = field(default_factory=list)
    include_counters: List[str] = field(default_factory=list)
    exclude_counters: List[str] = field(default_factory=list)

    @classmethod
    def from_section(cls, section) -> "CategoryFilter":
        return cls(
            enabled=section.getboolean("enabled", fallback=True),
            include_names=_parse_patterns(section.get("include_names", "")),
            exclude_names=_parse_patterns(section.get("exclude_names", "")),
            include_counters=_parse_patterns(section.get("include_counters", "")),
            exclude_counters=_parse_patterns(section.get("exclude_counters", "")),
        )

    # ------------------------------------------------------------------ #
    def name_allowed(self, name: str) -> bool:
        if self.exclude_names and _match_any(name, self.exclude_names):
            return False
        if self.include_names and not _match_any(name, self.include_names):
            return False
        return True

    def counter_allowed(self, counter: str) -> bool:
        if self.exclude_counters and _match_any(counter, self.exclude_counters):
            return False
        if self.include_counters and not _match_any(counter, self.include_counters):
            return False
        return True

    def filter_counters(self, counters: Iterable[str]) -> List[str]:
        return [c for c in counters if self.counter_allowed(c)]
