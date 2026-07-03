"""The event-driven core of Sensor Sentinel.

This is where the whole performance thesis lives (PRD §4.1, §6): a single bus
listener maintains an **incremental in-memory set** of down entities. There is
exactly one full scan of ``hass.states`` — at startup, to seed the set — and
never again. Every subsequent update is O(1) work on a single ``state_changed``
event.

Lifecycle of an entity:

    good ──bad──▶ pending (grace timer running)
    pending ──still bad after grace──▶ down   (fire entity_down, notify)
    pending ──good before grace──▶ good       (flap suppressed, no incident)
    down ──good──▶ good                        (fire entity_recovered, notify)
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BAD_STATES,
    CONF_GRACE_PERIOD,
    DEFAULT_BAD_STATES,
    DEFAULT_GRACE_PERIOD,
    DOMAIN,
    EVENT_ENTITY_DOWN,
    EVENT_ENTITY_RECOVERED,
    STATE_WRITE_DEBOUNCE,
)
from .exclusions import ExclusionEngine

_LOGGER = logging.getLogger(__name__)

# An entity that recovers and drops again within this window is "flapping".
_FLAP_WINDOW = 300.0


@dataclass
class Incident:
    """A single down entity. Kept deliberately small — this is the payload."""

    entity_id: str
    state: str
    name: str
    integration: str | None
    device: str | None
    area: str | None
    since: str  # ISO8601 UTC
    flapping: bool = False

    def as_event_data(self) -> dict:
        return asdict(self)


@dataclass
class _Snapshot:
    """Immutable-ish view the entities render from (coordinator.data)."""

    count: int = 0
    incidents: dict[str, Incident] = field(default_factory=dict)
    by_integration: dict[str, int] = field(default_factory=dict)
    by_area: dict[str, int] = field(default_factory=dict)


class SentinelCoordinator(DataUpdateCoordinator[_Snapshot]):
    """Owns the in-memory down-set and drives the entities/events."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # event-driven; we never poll.
        )
        self.entry = entry
        self._down: dict[str, Incident] = {}
        self._pending: dict[str, callable] = {}  # entity_id -> cancel callback
        self._recovered_at: dict[str, float] = {}  # for flap detection
        self._unsub: list[callable] = []
        self._platform_cache: dict[str, str | None] = {}
        self._write_scheduled = False

        self.exclusions = ExclusionEngine(dict(entry.options), self._platform_of)
        self._load_tunables()

    # -- Tunables from options ---------------------------------------------

    def _load_tunables(self) -> None:
        opts = self.entry.options
        self._bad_states = frozenset(opts.get(CONF_BAD_STATES, DEFAULT_BAD_STATES))
        self._grace = float(opts.get(CONF_GRACE_PERIOD, DEFAULT_GRACE_PERIOD))

    async def async_options_updated(self) -> None:
        """Re-read options after the user edits the exclusions UI."""
        self.exclusions.update(dict(self.entry.options))
        self._load_tunables()
        # A rule change can silence or surface entities; re-seed from scratch.
        self._reseed()
        self._schedule_write()

    # -- Actions backing the card / services --------------------------------

    def async_snooze(self, entity_id: str, until_ts: float) -> None:
        """Mute an entity until ``until_ts``; drop it from the current set."""
        self.exclusions.snooze(entity_id, until_ts)
        self._clear_tracking(entity_id)
        self._schedule_write()

    def async_unsnooze(self, entity_id: str) -> None:
        self.exclusions.unsnooze(entity_id)
        # Re-evaluate just this entity; no fleet scan.
        state = self.hass.states.get(entity_id)
        if (
            state is not None
            and state.state in self._bad_states
            and not self.exclusions.is_excluded(entity_id, time.time())
            and entity_id not in self._down
            and entity_id not in self._pending
        ):
            self._start_grace(entity_id)

    def explain(self, entity_id: str) -> dict:
        """Answer 'why is this flagged / excluded?' for any entity (PRD §4a)."""
        match = self.exclusions.match(entity_id, time.time())
        if match is not None:
            return {
                "result": "excluded",
                "rule_type": match.rule_type,
                "value": match.value,
            }
        state = self.hass.states.get(entity_id)
        if state is None:
            return {"result": "missing"}
        if entity_id in self._down:
            return {
                "result": "down",
                "state": state.state,
                "since": self._down[entity_id].since,
            }
        if entity_id in self._pending:
            return {"result": "pending_grace", "state": state.state}
        if state.state in self._bad_states:
            return {"result": "bad_awaiting_grace", "state": state.state}
        return {"result": "ok", "state": state.state}

    # -- Registry helpers (cached; refreshed on registry-updated events) ----

    def _platform_of(self, entity_id: str) -> str | None:
        if entity_id in self._platform_cache:
            return self._platform_cache[entity_id]
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        platform = entry.platform if entry else None
        self._platform_cache[entity_id] = platform
        return platform

    def _enrich(self, entity_id: str, state) -> Incident:
        """Resolve name/integration/device/area. Runs per *incident*, not per event."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)

        entry = ent_reg.async_get(entity_id)
        name = (
            (state.attributes.get("friendly_name") if state else None)
            or (entry.name or entry.original_name if entry else None)
            or entity_id
        )
        integration = entry.platform if entry else None

        device_name: str | None = None
        area_name: str | None = None
        area_id = entry.area_id if entry else None
        if entry and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            if device:
                device_name = device.name_by_user or device.name
                area_id = area_id or device.area_id
        if area_id:
            area = area_reg.async_get_area(area_id)
            area_name = area.name if area else None

        flapping = (time.monotonic() - self._recovered_at.get(entity_id, 0)) < _FLAP_WINDOW

        return Incident(
            entity_id=entity_id,
            state=state.state if state else "unknown",
            name=name,
            integration=integration,
            device=device_name,
            area=area_name,
            since=dt_util.utcnow().isoformat(),
            flapping=flapping,
        )

    # -- Startup / teardown -------------------------------------------------

    async def async_start(self) -> None:
        """Seed the set with one scan, then go fully event-driven."""
        self._reseed(initial=True)
        # Publish an initial snapshot synchronously so entities never read None.
        self.data = self._build_snapshot()

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed)
        )
        # Invalidate the platform cache whenever the entity registry changes.
        self._unsub.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._invalidate_registry_cache
            )
        )
        self._schedule_write()

    async def async_shutdown(self) -> None:
        for cancel in self._pending.values():
            cancel()
        self._pending.clear()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _invalidate_registry_cache(self, _event: Event) -> None:
        self._platform_cache.clear()

    def _reseed(self, initial: bool = False) -> None:
        """One-time full scan. The ONLY place we iterate all states."""
        for cancel in self._pending.values():
            cancel()
        self._pending.clear()
        self._down.clear()

        now = time.time()
        for state in self.hass.states.async_all():
            if state.state not in self._bad_states:
                continue
            if self.exclusions.is_excluded(state.entity_id, now):
                continue
            # Entities already bad at startup are recorded as down immediately
            # (no grace, no notification storm on boot).
            self._down[state.entity_id] = self._enrich(state.entity_id, state)

        if initial:
            _LOGGER.debug("Seeded with %d down entities", len(self._down))

    # -- The hot path -------------------------------------------------------

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")

        is_bad = new_state is not None and new_state.state in self._bad_states
        tracked = entity_id in self._down or entity_id in self._pending

        # The overwhelmingly common case: a healthy entity changing value.
        if not is_bad and not tracked:
            return

        if is_bad and self.exclusions.is_excluded(entity_id, time.time()):
            # An excluded entity should never occupy the set.
            self._clear_tracking(entity_id)
            return

        if is_bad:
            if entity_id in self._down:
                # Already down; refresh the state label (unknown -> unavailable).
                self._down[entity_id].state = new_state.state
                return
            if entity_id in self._pending:
                return  # grace timer already running
            self._start_grace(entity_id)
        else:
            # Recovered.
            if entity_id in self._pending:
                self._pending.pop(entity_id)()  # cancel timer, flap suppressed
                return
            if entity_id in self._down:
                self._promote_recovery(entity_id)

    def _start_grace(self, entity_id: str) -> None:
        @callback
        def _fire(_now) -> None:
            self._pending.pop(entity_id, None)
            state = self.hass.states.get(entity_id)
            if state is None or state.state not in self._bad_states:
                return  # recovered during grace
            if self.exclusions.is_excluded(entity_id, time.time()):
                return
            self._promote_down(entity_id, state)

        self._pending[entity_id] = async_call_later(self.hass, self._grace, _fire)

    def _promote_down(self, entity_id: str, state) -> None:
        incident = self._enrich(entity_id, state)
        self._down[entity_id] = incident
        self.hass.bus.async_fire(EVENT_ENTITY_DOWN, incident.as_event_data())
        _LOGGER.debug("Down: %s", entity_id)
        self._schedule_write()

    def _promote_recovery(self, entity_id: str) -> None:
        incident = self._down.pop(entity_id)
        self._recovered_at[entity_id] = time.monotonic()
        self.hass.bus.async_fire(
            EVENT_ENTITY_RECOVERED, {"entity_id": entity_id, "name": incident.name}
        )
        _LOGGER.debug("Recovered: %s", entity_id)
        self._schedule_write()

    def _clear_tracking(self, entity_id: str) -> None:
        cancel = self._pending.pop(entity_id, None)
        if cancel:
            cancel()
        if self._down.pop(entity_id, None) is not None:
            self._schedule_write()

    # -- Coalesced snapshot publishing --------------------------------------

    def _schedule_write(self) -> None:
        """Debounce sensor writes so a flapping storm can't thrash the state machine."""
        if self._write_scheduled:
            return
        self._write_scheduled = True

        @callback
        def _flush(_now) -> None:
            self._write_scheduled = False
            self.async_set_updated_data(self._build_snapshot())

        async_call_later(self.hass, STATE_WRITE_DEBOUNCE, _flush)

    def _build_snapshot(self) -> _Snapshot:
        by_integration: dict[str, int] = {}
        by_area: dict[str, int] = {}
        for inc in self._down.values():
            key = inc.integration or "unknown"
            by_integration[key] = by_integration.get(key, 0) + 1
            akey = inc.area or "unassigned"
            by_area[akey] = by_area.get(akey, 0) + 1
        return _Snapshot(
            count=len(self._down),
            incidents=dict(self._down),
            by_integration=by_integration,
            by_area=by_area,
        )

    async def _async_update_data(self) -> _Snapshot:
        # Never polled (update_interval=None); return the current snapshot.
        return self._build_snapshot()
