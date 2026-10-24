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
auto-recovery features, and the opt-in *silent-device* check: a device whose
last_seen timestamp has gone stale is flagged even though its entities still
look healthy (a dead battery sensor keeps reporting its last state forever).
A Z-Wave node the driver has marked *dead* is flagged too (its node_status
sensor reads "dead", a normal state to everything else), and an opt-in probe
pings quiet mains nodes so a dead one gets marked in the first place.
Incident ``since`` timestamps and snoozes survive a restart via the config
Store.
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
    CONF_PROBE_HOURS,
    CONF_RECOVERY_DELAY,
    CONF_REALERT_HOURS,
    CONF_SILENT_EXCLUDE_DEVICES,
    CONF_SILENT_HOURS,
    CONF_STALE_DAYS,
    CONF_STARTUP_GRACE,
    DEFAULT_AUTO_RECOVERY,
    DEFAULT_BAD_STATES,
    DEFAULT_GRACE_PERIOD,
    DEFAULT_PROBE_HOURS,
    DEFAULT_RECOVERY_DELAY,
    DEFAULT_REALERT_HOURS,
    DEFAULT_SILENT_HOURS,
    DEFAULT_STALE_DAYS,
    DEFAULT_STARTUP_GRACE,
    DOMAIN,
    EVENT_ENTITY_DOWN,
    EVENT_ENTITY_RECOVERED,
    EVENT_ENTITY_STILL_DOWN,
    HOUSEKEEPING_INTERVAL,
    NODE_STATUS_DEAD,
    PROBE_MAX_PER_TICK,
    RECOVERY_COOLDOWN,
    RECOVERY_MAX_ATTEMPTS,
    RECOVERY_MAX_ATTEMPTS_SHARED,
    REASON_DEAD,
    REASON_SILENT,
    REASON_STATE,
    SILENT_PLATFORMS,
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
# When picking the entity that represents a silent device, prefer the one a
# person actually cares about (the contact, the lock) over its diagnostics.
_PRIMARY_DOMAINS = (
    "binary_sensor", "lock", "cover", "switch", "light", "climate", "fan",
    "sensor", "event",
)


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
    device_id: str | None = None
    # "state" for an unavailable/unknown entity; "silent" for a device whose
    # last_seen went stale while its entities kept their last values; "dead"
    # for a Z-Wave node the driver has marked dead.
    reason: str = REASON_STATE
    last_seen: str | None = None  # ISO8601, silent incidents only
    silent_for_hours: int | None = None  # refreshed each housekeeping tick

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
        # True until the warmup seed runs. Boot churns thousands of entities
        # through unavailable/unknown; the seed decides what is really down.
        self._warming = False

        # Battery enrichment. Indexed once from the registry (and on registry
        # updates) so the hot path costs exactly one set lookup.
        self._battery_entities: set[str] = set()  # battery sensor entity_ids
        self._battery_by_device: dict[str, str] = {}  # device_id -> battery eid
        self._battery_cache: dict[str, int] = {}  # battery eid -> last good %

        # Silent-device check. Registry-only index, rebuilt with the battery one.
        self._last_seen_by_device: dict[str, str] = {}  # device_id -> last_seen eid
        self._last_seen_entities: dict[str, str] = {}  # last_seen eid -> device_id
        self._primary_by_device: dict[str, str] = {}  # device_id -> reported eid
        # device_id -> the entity its silent incident is keyed on. A map, not a
        # set, so a registry change that re-picks the primary entity can't
        # orphan a live incident.
        self._silent: dict[str, str] = {}

        # Dead-node check: Z-Wave JS node_status sensors, indexed with the rest.
        self._node_status: dict[str, str] = {}  # node_status eid -> device_id
        # Mains nodes the probe may ping: device_id -> (node_status, last_seen).
        self._mains_nodes: dict[str, tuple[str, str | None]] = {}
        self._probed_at: dict[str, float] = {}  # device_id -> monotonic

        # Persistence + insight/housekeeping state.
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._stored_since: dict[str, str] = {}
        self._recovered_today = 0
        self._recovered_date = dt_util.now().date().isoformat()
        self._realerted_at: dict[str, float] = {}  # entity_id -> monotonic
        self._recovery_attempts: dict[str, int] = {}
        self._recovery_last: dict[str, float] = {}  # entity_id -> monotonic
        self._recovery_exhausted: set[str] = set()  # logged "giving up" already
        # config_entry_id -> "owns something that works", per housekeeping pass.
        self._entry_health_cache: dict[str, bool] = {}
        # Config entries already reloaded this pass, so siblings don't pile on.
        self._reloaded_this_pass: set[str] = set()

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
        self._silent_hours = _num(CONF_SILENT_HOURS, DEFAULT_SILENT_HOURS)
        self._probe_hours = _num(CONF_PROBE_HOURS, DEFAULT_PROBE_HOURS)
        self._silent_excluded_devices = frozenset(
            d for d in (opts.get(CONF_SILENT_EXCLUDE_DEVICES) or []) if isinstance(d, str)
        )

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
        self._reevaluate(entity_id)

    def _reevaluate(self, entity_id: str) -> None:
        """Re-check a single entity's current state; no fleet scan."""
        state = self.hass.states.get(entity_id)
        if (
            state is not None
            and self._is_bad(entity_id, state.state)
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
                "reason": inc.reason,
                "state": state.state,
                "since": inc.since,
                "stale": inc.stale,
                "battery": inc.battery,
                "last_seen": inc.last_seen,
            }
        if entity_id in self._pending:
            return {"result": "pending_grace", "state": state.state}
        if self._is_bad(entity_id, state.state):
            return {"result": "bad_awaiting_grace", "state": state.state}
        return {"result": "ok", "state": state.state}

    def _is_bad(self, entity_id: str, value: str) -> bool:
        """Is this state value a reportable one for this entity?"""
        return value in self._bad_states or (
            value == NODE_STATUS_DEAD and entity_id in self._node_status
        )

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

    # -- Silent devices -------------------------------------------------------

    @staticmethod
    def _is_last_seen(entry) -> bool:
        """A device-health last_seen timestamp sensor from a platform we trust.

        Z-Wave JS: unique_id ``<home>.<node>.statistics_last_seen``.
        Zigbee2MQTT: ``<ieee>_last_seen_zigbee2mqtt`` (when last_seen is on).
        """
        return (
            entry.domain == "sensor"
            and entry.platform in SILENT_PLATFORMS
            and (entry.device_class or entry.original_device_class) == "timestamp"
            and "last_seen" in str(entry.unique_id)
        )

    def _index_last_seen(self) -> None:
        """Map devices to their last_seen sensor and to the entity that should
        stand for them in an incident. Registry-only — touches no states.

        Battery devices only. A mains device (fan, plug, repeater) is not
        polled and only talks when something changes, so its last_seen goes
        stale whenever nobody touches it. A dead mains node is the dead-node
        check's job instead (node_status, plus the optional probe).

        A disabled last_seen sensor is invisible here (it has no state), so a
        device whose sensor is disabled is simply not checked.

        The same pass indexes Z-Wave node_status sensors (the dead-node check)
        and which of those nodes are mains-powered (the probe's targets) —
        exactly the devices the silent check leaves out.
        """
        by_device: dict[str, str] = {}
        candidates: dict[str, list] = {}
        battery_devices: set[str] = set()
        node_status: dict[str, str] = {}
        for entry in er.async_get(self.hass).entities.values():
            if not entry.device_id or entry.platform not in SILENT_PLATFORMS:
                continue
            # Disabled battery entities still prove the device runs on one.
            if (entry.device_class or entry.original_device_class) == "battery":
                battery_devices.add(entry.device_id)
            if entry.disabled_by is not None:
                continue
            if self._is_node_status(entry):
                node_status[entry.entity_id] = entry.device_id
            elif self._is_last_seen(entry):
                by_device.setdefault(entry.device_id, entry.entity_id)
            elif entry.entity_category is None:
                candidates.setdefault(entry.device_id, []).append(entry.entity_id)

        def _rank(entity_id: str) -> tuple[int, int, str]:
            # Within a domain the shortest id is nearly always the device's
            # main entity: a Ring contact has ten binary_sensors, and the
            # contact itself is the bare `binary_sensor.contact_front_door`.
            domain = entity_id.partition(".")[0]
            order = (
                _PRIMARY_DOMAINS.index(domain)
                if domain in _PRIMARY_DOMAINS
                else len(_PRIMARY_DOMAINS)
            )
            return (order, len(entity_id), entity_id)

        self._node_status = node_status
        self._mains_nodes = {
            device_id: (status_eid, by_device.get(device_id))
            for status_eid, device_id in node_status.items()
            if device_id not in battery_devices
        }
        self._probed_at = {
            d: t for d, t in self._probed_at.items() if d in self._mains_nodes
        }

        by_device = {d: e for d, e in by_device.items() if d in battery_devices}
        primary: dict[str, str] = {}
        for device_id, last_seen_eid in by_device.items():
            options = candidates.get(device_id)
            primary[device_id] = min(options, key=_rank) if options else last_seen_eid
        self._last_seen_by_device = by_device
        self._last_seen_entities = {eid: dev for dev, eid in by_device.items()}
        self._primary_by_device = primary

    @staticmethod
    def _is_node_status(entry) -> bool:
        """Z-Wave JS's per-node status sensor (alive / asleep / awake / dead)."""
        return (
            entry.domain == "sensor"
            and entry.platform == "zwave_js"
            and str(entry.unique_id).endswith(".node_status")
        )

    @staticmethod
    def _parse_last_seen(state):
        """The last_seen value as an aware UTC datetime, or None if unreadable."""
        if state is None:
            return None
        parsed = dt_util.parse_datetime(state.state)
        if parsed is None:
            return None  # unavailable / unknown / garbage: no news either way
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return dt_util.as_utc(parsed)

    def _silent_excluded(self, device_id: str, primary: str, now_ts: float) -> bool:
        return device_id in self._silent_excluded_devices or self.exclusions.is_excluded(
            primary, now_ts, ignore_domains=True
        )

    def _scan_silent(self, announce: bool) -> bool:
        """Raise and refresh silent-device incidents. Returns True on change.

        Runs from housekeeping (every HOUSEKEEPING_INTERVAL) and the seed —
        never from the state_changed hot path — and walks only the devices that
        have a last_seen sensor.
        """
        if self._silent_hours <= 0:
            return False
        now = dt_util.utcnow()
        now_ts = time.time()
        threshold = self._silent_hours * 3600
        # A device that already has a real (state) incident is reported by that
        # incident; a second "silent" line for it would be noise.
        state_devices = {
            inc.device_id
            for inc in self._down.values()
            if inc.reason != REASON_SILENT and inc.device_id
        }
        changed = False
        for device_id, last_seen_eid in self._last_seen_by_device.items():
            if device_id in self._silent:
                primary = self._silent[device_id]
                if device_id in state_devices or self._silent_excluded(
                    device_id, primary, now_ts
                ):
                    # Superseded or newly excluded: drop it, but it did not
                    # recover, so don't bank a recovery.
                    self._clear_tracking(primary)
                    changed = True
                    continue
                changed |= self._check_silent_device(
                    device_id, self.hass.states.get(last_seen_eid), now
                )
                continue

            if device_id in state_devices:
                continue
            primary = self._primary_by_device.get(device_id, last_seen_eid)
            if primary in self._down or primary in self._pending:
                continue
            last_seen = self._parse_last_seen(self.hass.states.get(last_seen_eid))
            if last_seen is None:
                continue
            age = (now - last_seen).total_seconds()
            if age < threshold or self._silent_excluded(device_id, primary, now_ts):
                continue
            self._promote_silent(device_id, primary, last_seen, age, announce)
            changed = True
        return changed

    def _promote_silent(
        self, device_id: str, primary: str, last_seen, age: float, announce: bool
    ) -> None:
        last_seen_iso = last_seen.isoformat()
        # `since` is when the device went quiet, not when we noticed, so the
        # duration, longest-down and stale_days all mean what they say.
        incident = self._enrich(primary, self.hass.states.get(primary), since=last_seen_iso)
        # The device has gone quiet, not one entity, so name the device.
        incident.name = incident.device or incident.name
        incident.device_id = device_id
        incident.reason = REASON_SILENT
        incident.last_seen = last_seen_iso
        incident.silent_for_hours = int(age // 3600)
        self._down[primary] = incident
        self._silent[device_id] = primary
        if announce:
            self.hass.bus.async_fire(EVENT_ENTITY_DOWN, incident.as_event_data())
        _LOGGER.debug("Silent: %s (last seen %s)", primary, last_seen_iso)

    def _check_silent_device(self, device_id: str, last_seen_state, now=None) -> bool:
        """Clear a silent incident once last_seen moves forward; otherwise
        refresh its silent_for_hours. Returns True if anything changed."""
        primary = self._silent.get(device_id)
        incident = self._down.get(primary) if primary else None
        if incident is None or incident.reason != REASON_SILENT:
            self._silent.pop(device_id, None)
            return False
        last_seen = self._parse_last_seen(last_seen_state)
        if last_seen is None:
            return False
        previous = dt_util.parse_datetime(incident.last_seen or "")
        if previous is not None and last_seen > previous:
            self._promote_recovery(primary)
            return True
        hours = int(((now or dt_util.utcnow()) - last_seen).total_seconds() // 3600)
        if hours != incident.silent_for_hours:
            incident.silent_for_hours = hours
            return True
        return False

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

        incident = Incident(
            entity_id=entity_id,
            state=state.state if state else "unknown",
            name=name,
            integration=integration,
            device=device_name,
            area=area_name,
            since=since or dt_util.utcnow().isoformat(),
            battery=battery,
            flapping=flapping,
            device_id=entry.device_id if entry else None,
        )
        self._apply_node_status(incident)
        return incident

    def _apply_node_status(self, incident: Incident) -> None:
        """A dead node_status stands for its whole device, so name the device.

        Merely unavailable (Z-Wave JS restarting) is an ordinary state incident.
        """
        if incident.entity_id not in self._node_status:
            return
        if incident.state == NODE_STATUS_DEAD:
            incident.reason = REASON_DEAD
            incident.name = incident.device or incident.name
        else:
            incident.reason = REASON_STATE

    # -- Startup / teardown -------------------------------------------------

    async def async_start(self) -> None:
        """Load persisted state, then seed (after a warmup) and go event-driven."""
        await self._load_store()

        # Index batteries before the state listener starts so the very first
        # incident can be enriched.
        self._index_batteries()
        self._index_last_seen()
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
        # The listener above is already live, so the hot path must ignore
        # transitions until then — otherwise a boot-time blip outlasting the
        # (shorter) grace period is promoted to down and then banked as a
        # recovery, which is what inflated recovered_today on every restart.
        self._warming = self._startup_grace > 0
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
        self._index_last_seen()
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
            # Deleted or disabled for real, unlike the transient state-machine
            # removal a reload causes — so this is where the recovery budget is
            # genuinely dead and should not be left behind.
            self._forget_recovery(entity_id)

    def _is_registry_disabled(self, entity_id: str) -> bool:
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry is not None and entry.disabled_by is not None

    @callback
    def _warmup_seed(self, _now=None) -> None:
        """Seed after the warmup window: whatever is still bad is genuinely down."""
        self._warming = False
        self._reseed(initial=True)
        self._schedule_write()

    def _reseed(self, initial: bool = False) -> None:
        """One-time full scan. The ONLY place we iterate all states."""
        for cancel in self._pending.values():
            cancel()
        self._pending.clear()
        # Preserve known since-timestamps (current incidents, then persisted)
        # so durations survive an options-change reseed and a restart.
        known_since = {
            eid: inc.since
            for eid, inc in self._down.items()
            if inc.reason != REASON_SILENT
        }
        self._down.clear()
        self._silent.clear()

        now = time.time()
        for state in self.hass.states.async_all():
            if not self._is_bad(state.entity_id, state.state):
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

        # Silent devices are re-derived from last_seen, which survives a
        # restart, so there is nothing to persist. Seeding is quiet, like the
        # state seed above: these were already silent before we looked.
        self._scan_silent(announce=False)

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

        if self._warming:
            return  # the warmup seed will pick up whatever is still bad

        # A silent device's last_seen just moved: it has been heard from. Empty
        # unless a silent incident is live, so the hot path pays one truthiness
        # check in the normal case.
        if self._silent:
            device_id = self._last_seen_entities.get(entity_id)
            if device_id in self._silent and self._check_silent_device(
                device_id, new_state
            ):
                self._schedule_write()

        is_bad = new_state is not None and self._is_bad(entity_id, new_state.state)
        tracked = entity_id in self._down or entity_id in self._pending

        # The overwhelmingly common case: a healthy entity changing value.
        if not is_bad and not tracked:
            # ...with one exception. A recovery action that reloaded the owning
            # config entry pulled this entity out of the tracked set on the way
            # past (see the removal branch below), so its return to health
            # arrives here, untracked. That is the recovery having WORKED —
            # refund the budget so an action that fixes things can keep doing
            # so. The dict is empty unless auto-recovery is on and has fired,
            # which keeps this to one truthiness check on the hot path.
            if self._recovery_attempts and new_state is not None:
                self._forget_recovery(entity_id)
            return

        incident = self._down.get(entity_id)
        if incident is not None and incident.reason == REASON_SILENT:
            # A silent device's states are not what flagged it, so they can't
            # clear it either: only a fresher last_seen can (_check_silent_device),
            # or a real registry removal (_on_registry_updated). In particular a
            # reload's transient removal must not drop it, or every silent
            # device would be re-announced on the next housekeeping tick.
            if new_state is not None:
                incident.state = new_state.state
            return

        if new_state is None:
            # Gone from the state machine, not recovered — nearly always a
            # config entry unloading, and frequently OUR OWN recovery reload.
            # Drop it from the live set, but do NOT bank a recovery and do NOT
            # clear the recovery budget: that reset is what let a reload
            # resurrect the very counter meant to stop it reloading again, and
            # it inflated recovered_today with entities that never came back.
            self._clear_tracking(entity_id)
            return

        if is_bad and self.exclusions.is_excluded(entity_id, time.time()):
            # An excluded entity should never occupy the set.
            self._clear_tracking(entity_id)
            return

        if is_bad:
            if entity_id in self._down:
                # Already down; refresh the state label (unknown -> unavailable).
                self._down[entity_id].state = new_state.state
                self._apply_node_status(self._down[entity_id])
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
            if state is None or not self._is_bad(entity_id, state.state):
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
        if incident.reason == REASON_SILENT:
            self._silent.pop(incident.device_id, None)
        self._recovered_at[entity_id] = time.monotonic()
        self._realerted_at.pop(entity_id, None)
        self._forget_recovery(entity_id)
        self._note_recovery()
        self.hass.bus.async_fire(
            EVENT_ENTITY_RECOVERED,
            {
                "entity_id": entity_id,
                "name": incident.name,
                "reason": incident.reason,
                "device_id": incident.device_id,
            },
        )
        _LOGGER.debug("Recovered: %s", entity_id)
        self._schedule_write()

    def _forget_recovery(self, entity_id: str) -> None:
        """Wipe the recovery budget, re-arming auto-recovery for this entity."""
        self._recovery_attempts.pop(entity_id, None)
        self._recovery_last.pop(entity_id, None)
        self._recovery_exhausted.discard(entity_id)

    def _clear_tracking(self, entity_id: str) -> None:
        cancel = self._pending.pop(entity_id, None)
        if cancel:
            cancel()
        incident = self._down.pop(entity_id, None)
        if incident is not None:
            if incident.reason == REASON_SILENT:
                self._silent.pop(incident.device_id, None)
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
        # Wake entities whose snooze has lapsed. A continuously-bad entity
        # fires no state_changed events, so this tick is the only thing that
        # can surface it again after its snooze deadline passes. Must run
        # before the empty-set early-return: snoozing removes entities from
        # the down-set, so "everything snoozed" is exactly the empty case.
        expired = self.exclusions.prune_snoozes(time.time())
        for entity_id in expired:
            self._reevaluate(entity_id)
        if expired:
            self._schedule_write()

        if not self._warming and self._scan_silent(announce=True):
            self._schedule_write()

        if not self._warming and self._probe_hours > 0:
            self._probe_quiet_nodes()

        if not self._down:
            return
        # Both are per-pass state — entry health is only stable within a tick,
        # and a reload dispatched last tick shouldn't suppress this one.
        self._entry_health_cache.clear()
        self._reloaded_this_pass.clear()
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

            # Opt-in auto-recovery. Not for silent devices: a reload or ping
            # cannot put fresh batteries in a door sensor. A dead node does
            # get pinged — a successful ping is exactly what revives it.
            if (
                self._auto_recovery
                and inc.reason != REASON_SILENT
                and age >= self._recovery_delay
            ):
                self._maybe_recover(entity_id)

        if changed:
            self._schedule_write()

    def _probe_quiet_nodes(self) -> None:
        """Ping mains Z-Wave nodes nobody has heard from lately.

        The driver marks a node dead only when a send to it fails, and a
        repeater or an idle plug is never sent anything — so it can sit dead
        while reporting "alive" for days. A ping is the cheapest send there
        is: if it fails, node_status flips to dead and the ordinary state path
        takes it from there (grace, incident, recovery). Each node is pinged at
        most once per probe interval, a few per tick.
        """
        interval = self._probe_hours * 3600
        now = dt_util.utcnow()
        now_mono = time.monotonic()
        now_ts = time.time()
        sent = 0
        for device_id, (status_eid, last_seen_eid) in self._mains_nodes.items():
            if sent >= PROBE_MAX_PER_TICK:
                break
            last = self._probed_at.get(device_id)
            if last is not None and now_mono - last < interval:
                continue
            if status_eid in self._down or status_eid in self._pending:
                continue  # already flagged; auto-recovery does the pinging
            status = self.hass.states.get(status_eid)
            if status is None or status.state != "alive":
                continue  # dead / unavailable is already a state we see
            if last_seen_eid:
                last_seen = self._parse_last_seen(self.hass.states.get(last_seen_eid))
                if last_seen is not None and (now - last_seen).total_seconds() < interval:
                    continue  # heard from recently, so it isn't dead
            if self.exclusions.is_excluded(status_eid, now_ts):
                continue
            self._probed_at[device_id] = now_mono
            sent += 1
            _LOGGER.debug("Probe: pinging quiet Z-Wave node %s", status_eid)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "zwave_js", "ping", {"entity_id": status_eid}, blocking=False
                )
            )

    def _incident_age(self, inc: Incident, now_dt) -> float | None:
        """Seconds an incident has been down, or None if the timestamp is bad."""
        since = dt_util.parse_datetime(inc.since)
        if since is None:
            return None
        return max(0.0, (now_dt - since).total_seconds())

    def _maybe_recover(self, entity_id: str) -> None:
        """Attempt one recovery action, within budget.

        Resolve the action fully BEFORE spending any budget — an attempt we
        decline to make (no config entry, our own entry, out of budget) must
        not consume one of the few we get.
        """
        attempts = self._recovery_attempts.get(entity_id, 0)
        last = self._recovery_last.get(entity_id, 0)
        if attempts and time.monotonic() - last < RECOVERY_COOLDOWN:
            return

        platform = self._platform_of(entity_id)

        if platform == "zwave_js":
            # Pinging a node touches nothing else, so it gets the full budget.
            if not self._spend_recovery(entity_id, attempts, RECOVERY_MAX_ATTEMPTS):
                return
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

        # A reload restarts every entity the entry owns. If some of them are
        # working right now, that is real collateral damage inflicted on a
        # guess — so allow a token attempt and then stop, instead of cycling a
        # whole integration forever chasing a device that is simply switched
        # off. When nothing in the entry is working there is nothing to break,
        # so the full budget applies.
        shared = self._entry_has_available_entities(config_entry_id, entity_id)
        budget = RECOVERY_MAX_ATTEMPTS_SHARED if shared else RECOVERY_MAX_ATTEMPTS
        if not self._spend_recovery(entity_id, attempts, budget):
            return

        # One reload restarts every entity the entry owns, so a single offline
        # device with fourteen entities must not dispatch fourteen reloads of
        # the same entry in the same tick. Note the budget above is spent by
        # *every* sibling that wanted this, not just the one that dispatches:
        # charging only the dispatcher would leave the others to take turns
        # reloading the entry one per tick, for hours.
        if config_entry_id in self._reloaded_this_pass:
            _LOGGER.debug(
                "Auto-recovery: %s also wanted config entry %s reloaded; "
                "already dispatched this pass",
                entity_id,
                config_entry_id,
            )
            return
        self._reloaded_this_pass.add(config_entry_id)

        _LOGGER.info(
            "Auto-recovery: reloading config entry %s for %s (attempt %d/%d%s)",
            config_entry_id,
            entity_id,
            attempts + 1,
            budget,
            ", entry has working entities" if shared else "",
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(config_entry_id)
        )

    def _spend_recovery(self, entity_id: str, attempts: int, budget: int) -> bool:
        """Consume one attempt, or report that the budget is exhausted.

        Says so exactly once per entity: without this, "gave up" and "never
        tried" look identical in the log, which is precisely the ambiguity that
        made a runaway reload loop take a live investigation to spot.
        """
        if attempts >= budget:
            if entity_id not in self._recovery_exhausted:
                self._recovery_exhausted.add(entity_id)
                _LOGGER.info(
                    "Auto-recovery: giving up on %s after %d attempt(s) — it did "
                    "not come back. No further attempts until it recovers.",
                    entity_id,
                    attempts,
                )
            return False
        self._recovery_attempts[entity_id] = attempts + 1
        self._recovery_last[entity_id] = time.monotonic()
        return True

    def _entry_has_available_entities(
        self, config_entry_id: str, exclude: str
    ) -> bool:
        """True if the config entry owns any entity that is currently fine.

        Memoised for the duration of one housekeeping pass: several down
        entities usually share an entry, and this walks the registry.
        """
        cached = self._entry_health_cache.get(config_entry_id)
        if cached is not None:
            return cached
        registry = er.async_get(self.hass)
        healthy = False
        for entry in er.async_entries_for_config_entry(registry, config_entry_id):
            if entry.entity_id == exclude or entry.disabled_by is not None:
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is not None and state.state not in self._bad_states:
                healthy = True
                break
        self._entry_health_cache[config_entry_id] = healthy
        return healthy

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
