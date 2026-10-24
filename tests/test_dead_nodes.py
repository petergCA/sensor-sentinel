"""Dead Z-Wave nodes, and the probe that finds them.

Found in the field on a Z-Wave range extender: it stopped answering pings on
10/19, but nothing ever sends a repeater anything, so the driver kept calling it
"alive" for four days. Its only other entity (a firmware update) stayed
available, so nothing was flagged. node_status flipped to "dead" only when
something finally tried to talk to it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.sensor_sentinel.const import (
    CONF_EXCLUDED_ENTITIES,
    CONF_GRACE_PERIOD,
    CONF_PROBE_HOURS,
    CONF_STARTUP_GRACE,
    DOMAIN,
    EVENT_ENTITY_DOWN,
    EVENT_ENTITY_RECOVERED,
    PROBE_MAX_PER_TICK,
    REASON_DEAD,
)
from custom_components.sensor_sentinel.coordinator import SentinelCoordinator

_BASE = {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0}


def _ago(hours: float) -> str:
    return (dt_util.utcnow() - timedelta(hours=hours)).isoformat()


@pytest.fixture
def zwave_entry(hass):
    entry = MockConfigEntry(domain="zwave_js")
    entry.add_to_hass(hass)
    return entry


def _node(
    hass, zwave_entry, slug: str, node: int, status="alive", last_seen=None,
    battery=False,
):
    """A Z-Wave device with node_status and last_seen diagnostics, shaped like
    the real extender (a firmware update entity and nothing else)."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=zwave_entry.entry_id,
        identifiers={("zwave_js", f"4033396685-{node}")},
        name=slug.replace("_", " ").title(),
    )
    reg = er.async_get(hass)

    def _make(domain, uid, object_id, **kw):
        return reg.async_get_or_create(
            domain,
            "zwave_js",
            f"4033396685.{node}{uid}",
            device_id=device.id,
            config_entry=zwave_entry,
            suggested_object_id=object_id,
            **kw,
        ).entity_id

    firmware = _make("update", ".firmware_update", f"{slug}_firmware")
    status_eid = _make(
        "sensor", ".node_status", f"{slug}_node_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    seen = _make(
        "sensor",
        ".statistics_last_seen",
        f"{slug}_last_seen",
        original_device_class="timestamp",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    if battery:
        batt = _make(
            "sensor", "-128-0-level", f"{slug}_battery",
            original_device_class="battery",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        hass.states.async_set(batt, "90")
    hass.states.async_set(firmware, "off")
    hass.states.async_set(status_eid, status)
    hass.states.async_set(seen, last_seen or _ago(1))
    return device.id, status_eid, seen


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


async def test_dead_node_is_flagged_and_recovers(hass, zwave_entry):
    device_id, status, _ = _node(hass, zwave_entry, "zwave_extender_guest", 40)
    downs = async_capture_events(hass, EVENT_ENTITY_DOWN)
    recoveries = async_capture_events(hass, EVENT_ENTITY_RECOVERED)
    coordinator = await _start(hass, _BASE)
    assert coordinator._down == {}

    hass.states.async_set(status, "dead")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    inc = coordinator._down[status]
    assert inc.reason == REASON_DEAD
    assert inc.state == "dead"
    assert inc.name == "Zwave Extender Guest"  # the device, not "Node status"
    assert inc.device_id == device_id
    assert [e.data["entity_id"] for e in downs] == [status]
    assert coordinator.explain(status)["reason"] == REASON_DEAD

    hass.states.async_set(status, "alive")
    await hass.async_block_till_done()
    assert status not in coordinator._down
    assert [e.data["reason"] for e in recoveries] == [REASON_DEAD]
    await _stop(hass, coordinator)


async def test_dead_node_is_seeded_and_excludable(hass, zwave_entry):
    _, dead, _ = _node(hass, zwave_entry, "garage_plug", 41, status="dead")
    _, asleep, _ = _node(hass, zwave_entry, "door", 42, status="asleep", battery=True)
    coordinator = await _start(hass, _BASE)
    assert list(coordinator._down) == [dead]
    assert coordinator._down[dead].reason == REASON_DEAD
    await _stop(hass, coordinator)

    coordinator = await _start(hass, {**_BASE, CONF_EXCLUDED_ENTITIES: [dead]})
    assert coordinator._down == {}
    await _stop(hass, coordinator)


async def test_only_node_status_dead_counts(hass, zwave_entry):
    """'dead' is just a word for any other entity."""
    coordinator = await _start(hass, _BASE)
    hass.states.async_set("sensor.some_label", "dead")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert coordinator._down == {}
    await _stop(hass, coordinator)


async def test_probe_pings_quiet_mains_nodes_once(hass, zwave_entry):
    pings = async_mock_service(hass, "zwave_js", "ping")
    _, quiet, _ = _node(hass, zwave_entry, "extender", 40, last_seen=_ago(30))
    _node(hass, zwave_entry, "busy_plug", 41, last_seen=_ago(1))  # heard from lately
    _node(hass, zwave_entry, "door", 42, status="asleep", last_seen=_ago(30), battery=True)
    _node(hass, zwave_entry, "sleepy_contact", 43, last_seen=_ago(30), battery=True)
    _node(hass, zwave_entry, "broken", 44, status="dead", last_seen=_ago(30))
    coordinator = await _start(hass, {**_BASE, CONF_PROBE_HOURS: 24})

    coordinator._housekeeping()
    await hass.async_block_till_done()
    # Only the quiet mains node that still claims to be alive: battery nodes
    # are the silent check's job, and a dead one is already flagged.
    assert [c.data["entity_id"] for c in pings] == [quiet]

    # Not again within the interval, however often housekeeping runs.
    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert len(pings) == 1
    await _stop(hass, coordinator)


async def test_probe_off_by_default_and_respects_exclusions(hass, zwave_entry):
    pings = async_mock_service(hass, "zwave_js", "ping")
    _, quiet, _ = _node(hass, zwave_entry, "extender", 40, last_seen=_ago(30))
    coordinator = await _start(hass, _BASE)
    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert pings == []
    await _stop(hass, coordinator)

    coordinator = await _start(
        hass, {**_BASE, CONF_PROBE_HOURS: 24, CONF_EXCLUDED_ENTITIES: [quiet]}
    )
    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert pings == []
    await _stop(hass, coordinator)


async def test_probe_is_rate_limited(hass, zwave_entry):
    pings = async_mock_service(hass, "zwave_js", "ping")
    for n in range(PROBE_MAX_PER_TICK + 3):
        _node(hass, zwave_entry, f"repeater_{n}", 50 + n, last_seen=_ago(30))
    coordinator = await _start(hass, {**_BASE, CONF_PROBE_HOURS: 24})

    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert len(pings) == PROBE_MAX_PER_TICK
    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert len(pings) == PROBE_MAX_PER_TICK + 3
    await _stop(hass, coordinator)


async def test_unavailable_node_that_turns_out_dead_is_relabelled(hass, zwave_entry):
    """Z-Wave JS restarting makes node_status unavailable first; if the node
    then comes back as dead, the incident must say so."""
    _, status, _ = _node(hass, zwave_entry, "extender", 40)
    coordinator = await _start(hass, _BASE)
    hass.states.async_set(status, "unavailable")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert coordinator._down[status].reason != REASON_DEAD

    hass.states.async_set(status, "dead")
    await hass.async_block_till_done()
    assert coordinator._down[status].reason == REASON_DEAD
    assert coordinator._down[status].name == "Extender"
    await _stop(hass, coordinator)
