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

Startup is softened by a *warmup window* (``startup_grace``): entities already
bad at boot are only counted once they survive the window, so transient
boot-time unknowns (MQTT retained, Z-Wave interview) never spike the count.
A periodic housekeeping tick drives the opt-in re-alert / stale-retire /
auto-recovery features. Incident ``since`` timestamps and snoozes survive a
restart via the config Store.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_RECOVERY,
    CONF_BAD_STATES,
    CONF_GRACE_PERIOD,
    CONF_RECOVERY_DELAY,
    CONF_REALERT_HOURS,
    CONF_STALE_DAYS,
    CONF_STARTUP_GRACE,
    DEFAULT_AUTO_RECOVERY,
    DEFAULT_BAD_STATES,
    DEFAULT_GRACE_PERIOD,
    DEFAULT_RECOVERY_DELAY,
    DEFAULT_REALERT_HOURS,
    DEFAULT_STALE_DAYS,
    DEFAULT_STARTUP_GRACE,
    DOMAIN,
    EVENT_ENTITY_DOWN,
    EVENT_ENTITY_RECOVERED,
    EVENT_ENTITY_STILL_DOWN,
    HOUSEKEEPING_INTERVAL,
    RECOVERY_COOLDOWN,
    RECOVERY_MAX_ATTEMPTS,
    STATE_WRITE_DEBOUNCE,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .exclusions import ExclusionEngine

_LOGGER = logging.getLogger(__name__)

# An entity that recovers and drops again within this window is "flapping".
_FLAP_WINDOW = 300.0
# Debounce window for persisting state to the Store.
_STORE_DEBOUNCE = 15


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
    # Last-known-good battery % of the incident's device, or None if it has no
    # battery sensor. Deliberately the *cached* value, not a live read: when a
    # battery device drops off, its battery sensor goes unavailable in the same
    # breath, so reading it here would always yield nothing. See _battery_for.
    battery: int | None = None
    flapping: bool = False
    stale: bool = False

    def as_event_data(self) -> dict:
        return asdict(self)


@dataclass
class _Snapshot:
    """Immutable-ish view the entities render from (coordinator.data)."""

    count: int = 0
    incidents: dict[str, Incident] = field(default_factory=dict)
    by_integration: dict[str, int] = field(default_factory=dict)
    by_area: dict[str, int] = field(default_factory=dict)
    recovered_today: int = 0
    longest_down: dict | None = None
    stale_count: int = 0


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

        # Battery enrichment. Indexed once from the registry (and on registry
        # updates) so the hot path costs exactly one set lookup.
        self._battery_entities: set[str] = set()  # battery sensor entity_ids
        self._battery_by_device: dict[str, str] = {}  # device_id -> battery eid
        self._battery_cache: dict[str, int] = {}  # battery eid -> last good %

        # Persistence + insight/housekeeping state.
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored_since: dict[str, str] = {}
        self._recovered_today = 0
        self._recovered_date = dt_util.now().date().isoformat()
        self._realerted_at: dict[str, float] = {}  # entity_id -> monotonic
        self._recovery_attempts: dict[str, int] = {}
        self._recovery_last: dict[str, float] = {}  # entity_id -> monotonic

        self.exclusions = ExclusionEngine(dict(entry.options), self._platform_of)
        self._load_tunables()

    # -- Tunables from options ---------------------------------------------

    def _load_tunables(self) -> None:
        opts = self.entry.options

        def _num(key: str, default: float) -> float:
            """Read a numeric option, falling back to the default if it is
            missing, None, or otherwise un-coercible. Setup must never die on a
            corrupted option value."""
            try:
                value = opts.get(key)
                return float(default if value is None else value)
            except (TypeError, ValueError):
                return float(default)

        raw_states = opts.get(CONF_BAD_STATES) or DEFAULT_BAD_STATES
        self._bad_states = frozenset(
            s for s in raw_states if isinstance(s, str) and s
        ) or frozenset(DEFAULT_BAD_STATES)
        self._grace = _num(CONF_GRACE_PERIOD, DEFAULT_GRACE_PERIOD)
        self._startup_grace = _num(CONF_STARTUP_GRACE, DEFAULT_STARTUP_GRACE)
        self._realert_hours = _num(CONF_REALERT_HOURS, DEFAULT_REALERT_HOURS)
        self._stale_days = _num(CONF_STALE_DAYS, DEFAULT_STALE_DAYS)
        self._auto_recovery = bool(
            opts.get(CONF_AUTO_RECOVERY, DEFAULT_AUTO_RECOVERY)
        )
        self._recovery_delay = _num(CONF_RECOVERY_DELAY, DEFAULT_RECOVERY_DELAY)

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

    def all_incidents(self) -> list[dict]:
        """The full current down-set as plain dicts.

        Backs the websocket ``sensor_sentinel/list`` command so the card can
        show every incident on demand, without the count sensor ever having to
        carry an uncapped attribute payload (PRD §4.7 / §6).
        """
        return [inc.as_event_data() for inc in self._down.values()]

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
            inc = self._down[entity_id]
            return {
                "result": "down",
                "state": state.state,
                "since": inc.since,
                "stale": inc.stale,
                "battery": inc.battery,
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

    # -- Battery enrichment -------------------------------------------------

    def _index_batteries(self) -> None:
        """Map devices to their battery sensor. Registry-only — touches no states.

        Rebuilt on registry changes, so the hot path can gate on a set lookup
        instead of resolving device_class per event.
        """
        battery_entities: set[str] = set()
        by_device: dict[str, str] = {}
        for entry in er.async_get(self.hass).entities.values():
            if entry.domain != "sensor" or entry.disabled_by is not None:
                continue
            if (entry.device_class or entry.original_device_class) != "battery":
                continue
            battery_entities.add(entry.entity_id)
            # First battery sensor wins; devices rarely have more than one.
            if entry.device_id and entry.device_id not in by_device:
                by_device[entry.device_id] = entry.entity_id
        self._battery_entities = battery_entities
        self._battery_by_device = by_device
        # Drop cached values for entities that no longer exist.
        self._battery_cache = {
            eid: pct
            for eid, pct in self._battery_cache.items()
            if eid in battery_entities
        }

    def _seed_battery_cache(self) -> None:
        """Prime the cache from current states, so an incident in the first
        minutes after a restart still carries a battery reading."""
        for entity_id in self._battery_entities:
            state = self.hass.states.get(entity_id)
            if state is not None:
                self._remember_battery(entity_id, state.state)

    @callback
    def _remember_battery(self, entity_id: str, value) -> None:
        """Cache a battery reading, ignoring unavailable/unknown/non-numeric.

        Keeping the last good value is the whole point: it must survive the
        device going offline.
        """
        try:
            pct = int(float(value))
        except (TypeError, ValueError):
            return
        if 0 <= pct <= 100:
            self._battery_cache[entity_id] = pct

    def _battery_for(self, device_id: str | None) -> int | None:
        if not device_id:
            return None
        battery_eid = self._battery_by_device.get(device_id)
        if battery_eid is None:
            return None
        return self._battery_cache.get(battery_eid)

    def _enrich(self, entity_id: str, state, since: str | None = None) -> Incident:
        """Resolve name/integration/device/area/battery. Runs per *incident*, not per event."""
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

        battery = self._battery_for(entry.device_id if entry else None)

        flapping = (time.monotonic() - self._recovered_at.get(entity_id, 0)) < _FLAP_WINDOW

        return Incident(
            entity_id=entity_id,
            state=state.state if state else "unknown",
            name=name,
            integration=integration,
            device=device_name,
            area=area_name,
            since=since or dt_util.utcnow().isoformat(),
            battery=battery,
            flapping=flapping,
        )

    # -- Startup / teardown -------------------------------------------------

    async def async_start(self) -> None:
        """Load persisted state, then seed (after a warmup) and go event-driven."""
        await self._load_store()

        # Index batteries before the state listener starts so the very first
        # incident can be enriched.
        self._index_batteries()
        self._seed_battery_cache()

        # Publish an empty snapshot so entities never read None before warmup.
        self.data = self._build_snapshot()

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed)
        )
        # React to entity-registry changes (cache invalidation + drop entities
        # that were removed or disabled).
        self._unsub.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated
            )
        )
        # Periodic housekeeping (re-alert / stale / auto-recovery).
        self._unsub.append(
            async_track_time_interval(
                self.hass,
                self._housekeeping,
                timedelta(seconds=HOUSEKEEPING_INTERVAL),
            )
        )

        # Warmup: hold off the initial seed so boot-time transients don't count.
        if self._startup_grace <= 0:
            self._reseed(initial=True)
        elif self.hass.state == CoreState.running:
            self._unsub.append(
                async_call_later(self.hass, self._startup_grace, self._warmup_seed)
            )
        else:
            # Wait for HA to finish starting, then run the warmup timer.
            @callback
            def _on_started(_event: Event) -> None:
                # A once-listener removes itself when it fires. Drop our unsub
                # so shutdown doesn't try to remove it a second time, which HA
                # logs as "Unable to remove unknown job listener".
                try:
                    self._unsub.remove(unsub_started)
                except ValueError:
                    pass
                self._unsub.append(
                    async_call_later(self.hass, self._startup_grace, self._warmup_seed)
                )

            unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _on_started
            )
            self._unsub.append(unsub_started)

        self._schedule_write()

    async def async_shutdown(self) -> None:
        for cancel in self._pending.values():
            cancel()
        self._pending.clear()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        # Best-effort final persist; never let a store error block unload.
        try:
            await self._store.async_save(self._store_data())
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Final store save failed", exc_info=True)

    @callback
    def _on_registry_updated(self, event: Event) -> None:
        self._platform_cache.clear()
        self._index_batteries()
        entity_id = event.data.get("entity_id")
        # Only tracked entities are worth a registry lookup (keep this cheap).
        if not entity_id or (
            entity_id not in self._down and entity_id not in self._pending
        ):
            return
        # A disabled entity can linger in the state machine as `unavailable`
        # until its integration reloads; it should not be reported meanwhile.
        # Likewise drop anything removed from the registry.
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.disabled_by is not None:
            self._clear_tracking(entity_id)

    def _is_registry_disabled(self, entity_id: str) -> bool:
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry is not None and entry.disabled_by is not None

    @callback
    def _warmup_seed(self, _now=None) -> None:
        """Seed after the warmup window: whatever is still bad is genuinely down."""
        self._reseed(initial=True)
        self._schedule_write()

    def _reseed(self, initial: bool = False) -> None:
        """One-time full scan. The ONLY place we iterate all states."""
        for cancel in self._pending.values():
            cancel()
        self._pending.clear()
        # Preserve known since-timestamps (current incidents, then persisted)
        # so durations survive an options-change reseed and a restart.
        known_since = {eid: inc.since for eid, inc in self._down.items()}
        self._down.clear()

        now = time.time()
        for state in self.hass.states.async_all():
            if state.state not in self._bad_states:
                continue
            if self.exclusions.is_excluded(state.entity_id, now):
                continue
            if self._is_registry_disabled(state.entity_id):
                continue  # disabled entities lingering pending a reload
            # Entities still bad after warmup are recorded as down immediately
            # (no grace, no notification storm on boot).
            since = known_since.get(state.entity_id) or self._stored_since.get(
                state.entity_id
            )
            self._down[state.entity_id] = self._enrich(
                state.entity_id, state, since=since
            )

        if initial:
            _LOGGER.debug("Seeded with %d down entities", len(self._down))

    # -- The hot path -------------------------------------------------------

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")

        # Battery sensors report while the device is still alive, which is the
        # only time we can capture the value — one set lookup, then out.
        if entity_id in self._battery_entities:
            if new_state is not None:
                self._remember_battery(entity_id, new_state.state)

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
        self._realerted_at.pop(entity_id, None)
        self._recovery_attempts.pop(entity_id, None)
        self._recovery_last.pop(entity_id, None)
        self._note_recovery()
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

    # -- Insight / persistence helpers --------------------------------------

    def _note_recovery(self) -> None:
        today = dt_util.now().date().isoformat()
        if today != self._recovered_date:
            self._recovered_date = today
            self._recovered_today = 0
        self._recovered_today += 1

    async def _load_store(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - never let a bad store block setup
            _LOGGER.warning("Could not load stored state; starting fresh", exc_info=True)
            stored = None
        if not stored:
            return
        self._stored_since = dict(stored.get("since", {}))
        self.exclusions.load_snoozes(stored.get("snoozes", {}), time.time())
        recovered = stored.get("recovered", {})
        if recovered.get("date") == self._recovered_date:
            self._recovered_today = int(recovered.get("count", 0))
        for entity_id, pct in (stored.get("battery") or {}).items():
            self._remember_battery(entity_id, pct)

    def _store_data(self) -> dict:
        return {
            "since": {eid: inc.since for eid, inc in self._down.items()},
            "snoozes": self.exclusions.snapshot_snoozes(),
            "recovered": {
                "date": self._recovered_date,
                "count": self._recovered_today,
            },
            # Persisted so a last-known battery survives a restart. Without
            # this, a device that died before the restart comes back with no
            # reading — the exact case you most want the number for.
            "battery": self._battery_cache,
        }

    # -- Periodic housekeeping (re-alert / stale / recovery) ----------------

    @callback
    def _housekeeping(self, _now=None) -> None:
        if not self._down:
            return
        now_iso_dt = dt_util.utcnow()
        changed = False
        for entity_id, inc in list(self._down.items()):
            age = self._incident_age(inc, now_iso_dt)
            if age is None:
                continue

            # Stale-retire: flag long-down incidents out of the count.
            if self._stale_days > 0:
                should_be_stale = age >= self._stale_days * 86400
                if should_be_stale != inc.stale:
                    inc.stale = should_be_stale
                    changed = True
            if inc.stale:
                continue  # stale incidents skip re-alert + recovery

            # Re-alert on prolonged downtime.
            if self._realert_hours > 0 and age >= self._realert_hours * 3600:
                last = self._realerted_at.get(entity_id, 0)
                if time.monotonic() - last >= self._realert_hours * 3600:
                    self._realerted_at[entity_id] = time.monotonic()
                    self.hass.bus.async_fire(
                        EVENT_ENTITY_STILL_DOWN,
                        {**inc.as_event_data(), "down_seconds": int(age)},
                    )

            # Opt-in auto-recovery.
            if self._auto_recovery and age >= self._recovery_delay:
                self._maybe_recover(entity_id)

        if changed:
            self._schedule_write()

    def _incident_age(self, inc: Incident, now_dt) -> float | None:
        """Seconds an incident has been down, or None if the timestamp is bad."""
        since = dt_util.parse_datetime(inc.since)
        if since is None:
            return None
        return max(0.0, (now_dt - since).total_seconds())

    def _maybe_recover(self, entity_id: str) -> None:
        attempts = self._recovery_attempts.get(entity_id, 0)
        if attempts >= RECOVERY_MAX_ATTEMPTS:
            return
        last = self._recovery_last.get(entity_id, 0)
        if attempts and time.monotonic() - last < RECOVERY_COOLDOWN:
            return
        self._recovery_attempts[entity_id] = attempts + 1
        self._recovery_last[entity_id] = time.monotonic()

        platform = self._platform_of(entity_id)
        if platform == "zwave_js":
            _LOGGER.info("Auto-recovery: pinging Z-Wave node for %s", entity_id)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "zwave_js", "ping", {"entity_id": entity_id}, blocking=False
                )
            )
            return

        # Generic fallback: reload the owning config entry (never our own).
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        config_entry_id = entry.config_entry_id if entry else None
        if not config_entry_id or config_entry_id == self.entry.entry_id:
            return
        _LOGGER.info(
            "Auto-recovery: reloading config entry %s for %s",
            config_entry_id,
            entity_id,
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(config_entry_id)
        )

    # -- Coalesced snapshot publishing --------------------------------------

    def _schedule_write(self) -> None:
        """Debounce sensor writes so a flapping storm can't thrash the state machine."""
        # Persist (debounced by the Store itself).
        self._store.async_delay_save(self._store_data, _STORE_DEBOUNCE)
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
        active = 0
        stale = 0
        oldest_inc: Incident | None = None
        for inc in self._down.values():
            if inc.stale:
                stale += 1
                continue
            active += 1
            key = inc.integration or "unknown"
            by_integration[key] = by_integration.get(key, 0) + 1
            akey = inc.area or "unassigned"
            by_area[akey] = by_area.get(akey, 0) + 1
            if oldest_inc is None or inc.since < oldest_inc.since:
                oldest_inc = inc

        longest = None
        if oldest_inc is not None:
            longest = {
                "entity_id": oldest_inc.entity_id,
                "name": oldest_inc.name,
                "since": oldest_inc.since,
            }
        return _Snapshot(
            count=active,
            incidents=dict(self._down),
            by_integration=by_integration,
            by_area=by_area,
            recovered_today=self._recovered_today,
            longest_down=longest,
            stale_count=stale,
        )

    async def _async_update_data(self) -> _Snapshot:
        # Never polled (update_interval=None); return the current snapshot.
        return self._build_snapshot()
