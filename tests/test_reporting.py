"""What the numbers mean: recoveries, drops, and the warmup.

Three ways the reported numbers went wrong in the field:
- One flapping Sonos speaker (13 entities, every ~45 s) put recovered_today
  past 9,000 in a day.
- An integration Peter disabled vanished from the count, which read exactly
  like it had recovered.
- An options change during the startup warmup seeded every boot-time unknown
  as down (269 of them for ~90 s).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.sensor_sentinel import coordinator as coord_mod
from custom_components.sensor_sentinel.const import (
    CONF_GRACE_PERIOD,
    CONF_STARTUP_GRACE,
    DOMAIN,
    DROP_INTEGRATION_DISABLED,
    DROP_REMOVED,
    EVENT_ENTITY_DROPPED,
)
from custom_components.sensor_sentinel.coordinator import SentinelCoordinator

_BASE = {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0}


@pytest.fixture
def sonos_entry(hass):
    entry = MockConfigEntry(domain="sonos")
    entry.add_to_hass(hass)
    return entry


def _speaker(hass, entry, slug: str, n: int = 3) -> list[str]:
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("sonos", slug)},
        name=slug.title(),
    )
    reg = er.async_get(hass)
    ids = []
    for i in range(n):
        eid = reg.async_get_or_create(
            "switch", "sonos", f"{slug}-{i}", device_id=device.id,
            config_entry=entry, suggested_object_id=f"{slug}_{i}",
        ).entity_id
        hass.states.async_set(eid, "on")
        ids.append(eid)
    return ids


async def _start(hass, options: dict) -> SentinelCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    coordinator = SentinelCoordinator(hass, entry)
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator


async def _stop(hass, coordinator) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    await coordinator.async_shutdown()


async def _flip(hass, ids: list[str], state: str) -> None:
    for eid in ids:
        hass.states.async_set(eid, state)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()


async def test_a_flapping_device_counts_once_per_flap_window(hass, sonos_entry):
    speaker = _speaker(hass, sonos_entry, "portable")
    other = _speaker(hass, sonos_entry, "garage", n=1)
    coordinator = await _start(hass, _BASE)
    clock = [1000.0]

    with patch.object(coord_mod.time, "monotonic", lambda: clock[0]):
        await _flip(hass, speaker + other, "unavailable")
        await _flip(hass, speaker + other, "on")
        # Three entities of one device are one recovery; the other device is one.
        assert coordinator._recovered_today == 2

        for _ in range(5):  # flapping every 45 s
            clock[0] += 45
            await _flip(hass, speaker, "unavailable")
            await _flip(hass, speaker, "on")
        assert coordinator._recovered_today == 2

        # Quiet for a full window, then a genuine new drop and recovery.
        clock[0] += 301
        await _flip(hass, speaker, "unavailable")
        await _flip(hass, speaker, "on")
        assert coordinator._recovered_today == 3
    await _stop(hass, coordinator)


async def test_disabled_integration_is_dropped_not_recovered(hass, sonos_entry):
    ids = _speaker(hass, sonos_entry, "portable")
    dropped = async_capture_events(hass, EVENT_ENTITY_DROPPED)
    coordinator = await _start(hass, _BASE)
    await _flip(hass, ids, "unavailable")
    assert len(coordinator._down) == 3

    reg = er.async_get(hass)
    for eid in ids:
        reg.async_update_entity(eid, disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY)
    await hass.async_block_till_done()

    assert coordinator._down == {}
    assert coordinator._recovered_today == 0
    assert [e.data["reason"] for e in dropped] == [DROP_INTEGRATION_DISABLED] * 3
    assert coordinator._build_snapshot().dropped == [
        {
            "integration": "sonos",
            "reason": DROP_INTEGRATION_DISABLED,
            "count": 3,
            "at": coordinator._build_snapshot().dropped[0]["at"],
        }
    ]
    await _stop(hass, coordinator)


async def test_removed_entity_is_dropped_and_note_expires(hass, sonos_entry):
    (eid,) = _speaker(hass, sonos_entry, "portable", n=1)
    coordinator = await _start(hass, _BASE)
    await _flip(hass, [eid], "unavailable")
    clock = [5000.0]
    with patch.object(coord_mod.time, "monotonic", lambda: clock[0]):
        er.async_get(hass).async_remove(eid)
        await hass.async_block_till_done()
        assert [d["reason"] for d in coordinator._build_snapshot().dropped] == [DROP_REMOVED]
        clock[0] += 86401
        assert coordinator._build_snapshot().dropped == []
    assert coordinator._recovered_today == 0
    await _stop(hass, coordinator)


async def test_options_change_during_warmup_does_not_seed(hass, sonos_entry):
    (eid,) = _speaker(hass, sonos_entry, "portable", n=1)
    hass.states.async_set(eid, "unknown")  # a boot-time unknown
    coordinator = await _start(hass, {CONF_STARTUP_GRACE: 90, CONF_GRACE_PERIOD: 0})
    assert coordinator._warming

    await coordinator.async_options_updated()
    assert coordinator._down == {}  # the warmup seed decides, not the edit

    hass.states.async_set(eid, "on")  # settles inside the warmup
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=91))
    await hass.async_block_till_done()
    assert coordinator._down == {}
    assert coordinator._recovered_today == 0
    await _stop(hass, coordinator)
