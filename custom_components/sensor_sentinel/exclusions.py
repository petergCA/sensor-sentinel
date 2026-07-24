"""Exclusion rule engine for Sensor Sentinel.

Ports the five exclusion mechanisms the jazzyisj template used (PRD §3) into a
self-documenting, per-entity, O(rules) evaluation with no fleet-wide scan:

    1. Domain              -- exact domain match
    2. entity_id glob      -- fnmatch-style patterns (``*_firmware``, ``roborock_*``)
    3. Whole integration   -- the entity's platform (config-entry domain)
    4. Explicit entity_id   -- an exact entity_id
    5. Temporary snooze     -- a per-entity mute until a wall-clock deadline

The engine answers two questions cheaply:

    * ``is_excluded(entity_id)`` -> bool, used on the hot path.
    * ``match(entity_id)``       -> ExclusionMatch | None, used by the UI to
      answer "why is this excluded?" (PRD §4a manageability).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Callable

from .const import (
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXCLUDED_PATTERNS,
)


@dataclass(frozen=True)
class ExclusionMatch:
    """Describes which rule (if any) is silencing an entity."""

    rule_type: str  # "domain" | "glob" | "integration" | "entity" | "snooze"
    value: str  # the specific rule that matched (the domain, pattern, etc.)


class ExclusionEngine:
    """Evaluates exclusion rules for a single entity.

    ``platform_of`` maps an entity_id to its integration/platform name (from the
    entity registry). It is injected so the engine never touches the registry
    directly and stays trivially unit-testable.
    """

    def __init__(
        self,
        options: dict,
        platform_of: Callable[[str], str | None],
    ) -> None:
        self._platform_of = platform_of
        self._snoozed: dict[str, float] = {}
        self.update(options)

    def update(self, options: dict) -> None:
        """Rebuild the rule sets from the config-entry options."""
        self._domains: frozenset[str] = frozenset(
            options.get(CONF_EXCLUDED_DOMAINS, [])
        )
        self._integrations: frozenset[str] = frozenset(
            options.get(CONF_EXCLUDED_INTEGRATIONS, [])
        )
        self._entities: frozenset[str] = frozenset(
            options.get(CONF_EXCLUDED_ENTITIES, [])
        )
        # Keep patterns as a tuple; fnmatch is cheap and the list is short.
        self._patterns: tuple[str, ...] = tuple(
            options.get(CONF_EXCLUDED_PATTERNS, [])
        )

    # -- Snooze (runtime, not persisted in options) -------------------------

    def snooze(self, entity_id: str, until_ts: float) -> None:
        """Temporarily mute an entity until ``until_ts`` (epoch seconds)."""
        self._snoozed[entity_id] = until_ts

    def unsnooze(self, entity_id: str) -> None:
        self._snoozed.pop(entity_id, None)

    def prune_snoozes(self, now_ts: float) -> list[str]:
        """Drop expired snoozes and return the affected entity_ids.

        Callers must re-evaluate the returned entities: a continuously-bad
        entity emits no state_changed events, so nothing else will notice it
        again once its snooze lapses.
        """
        expired = [eid for eid, until in self._snoozed.items() if until <= now_ts]
        for eid in expired:
            del self._snoozed[eid]
        return expired

    def snapshot_snoozes(self) -> dict[str, float]:
        """Export the active snooze map (for persistence)."""
        return dict(self._snoozed)

    def load_snoozes(self, snoozes: dict, now_ts: float) -> None:
        """Restore snoozes from persistence, dropping any already expired."""
        for eid, until in (snoozes or {}).items():
            try:
                until_f = float(until)
            except (TypeError, ValueError):
                continue
            if until_f > now_ts:
                self._snoozed[eid] = until_f

    # -- Evaluation ---------------------------------------------------------

    def match(self, entity_id: str, now_ts: float | None = None) -> ExclusionMatch | None:
        """Return the first matching rule, or None if the entity is not excluded.

        Order is cheapest-first: domain and explicit-id are set lookups; glob and
        integration are the only non-trivial checks.
        """
        domain = entity_id.partition(".")[0]
        if domain in self._domains:
            return ExclusionMatch("domain", domain)

        if entity_id in self._entities:
            return ExclusionMatch("entity", entity_id)

        if now_ts is not None:
            until = self._snoozed.get(entity_id)
            if until is not None and until > now_ts:
                return ExclusionMatch("snooze", entity_id)

        for pattern in self._patterns:
            if fnmatch(entity_id, pattern):
                return ExclusionMatch("glob", pattern)

        if self._integrations:
            platform = self._platform_of(entity_id)
            if platform is not None and platform in self._integrations:
                return ExclusionMatch("integration", platform)

        return None

    def is_excluded(self, entity_id: str, now_ts: float | None = None) -> bool:
        return self.match(entity_id, now_ts) is not None
