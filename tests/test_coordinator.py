"""Coordinator behavior tests: snooze expiry and stale-incident visibility.

These target the two failure modes a review flagged:

1. A snoozed, continuously-unavailable entity fires no state_changed events,
   so only the housekeeping tick can surface it again once the snooze lapses.
2. Stale incidents leave the count but must remain in the full incident list.
"""

from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sensor_sentinel.const import (
    CONF_GRACE_PERIOD,
    CONF_STALE_DAYS,
    CONF_STARTUP_GRACE,
    DOMAIN,
)
from custom_components.sensor_sentinel.coordinator import SentinelCoordinator


async def _started_coordinator(hass, options: dict) -> SentinelCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    coordinator = SentinelCoordinator(hass, entry)
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator


async def _settle(hass, seconds: float = 2.0) -> None:
    """Advance HA's clock so debounced writes / zero-grace timers fire."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_expired_snooze_resurfaces_still_down_entity(hass):
    hass.states.async_set("sensor.dead", "unavailable")
    coordinator = await _started_coordinator(
        hass, {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0}
    )
    assert "sensor.dead" in coordinator._down

    # Snooze empties the down-set entirely — the historical bug was that
    # housekeeping early-returned on an empty set and never pruned, so an
    # expired snooze hid a still-down entity until restart.
    coordinator.async_snooze("sensor.dead", until_ts=time.time() - 1)
    assert "sensor.dead" not in coordinator._down
    assert "sensor.dead" not in coordinator._pending

    coordinator._housekeeping()
    await _settle(hass)

    assert "sensor.dead" in coordinator._down
    assert coordinator.exclusions.snapshot_snoozes() == {}
    await _settle(hass, 20)  # let debounced store/state writes flush
    await coordinator.async_shutdown()


async def test_active_snooze_stays_hidden(hass):
    hass.states.async_set("sensor.muted", "unavailable")
    coordinator = await _started_coordinator(
        hass, {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0}
    )
    coordinator.async_snooze("sensor.muted", until_ts=time.time() + 3600)

    coordinator._housekeeping()
    await _settle(hass)

    assert "sensor.muted" not in coordinator._down
    assert "sensor.muted" not in coordinator._pending
    await _settle(hass, 20)
    await coordinator.async_shutdown()


async def test_unsnooze_reevaluates_immediately(hass):
    hass.states.async_set("sensor.dead", "unavailable")
    coordinator = await _started_coordinator(
        hass, {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0}
    )
    coordinator.async_snooze("sensor.dead", until_ts=time.time() + 3600)
    coordinator.async_unsnooze("sensor.dead")
    await _settle(hass)

    assert "sensor.dead" in coordinator._down
    await _settle(hass, 20)
    await coordinator.async_shutdown()


async def test_stale_incident_leaves_count_but_stays_listed(hass):
    hass.states.async_set("sensor.retired", "unavailable")
    coordinator = await _started_coordinator(
        hass, {CONF_STARTUP_GRACE: 0, CONF_STALE_DAYS: 1}
    )
    incident = coordinator._down["sensor.retired"]
    incident.since = (dt_util.utcnow() - timedelta(days=2)).isoformat()

    coordinator._housekeeping()

    assert incident.stale
    snapshot = coordinator._build_snapshot()
    assert snapshot.count == 0
    assert snapshot.stale_count == 1
    # The full list (backing the card's websocket fetch) still carries it.
    listed = [inc["entity_id"] for inc in coordinator.all_incidents()]
    assert listed == ["sensor.retired"]
    await _settle(hass, 20)
    await coordinator.async_shutdown()
