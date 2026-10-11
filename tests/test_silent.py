"""Silent-device check.

The blind spot: a battery Z-Wave sensor that dies never becomes unavailable.
Z-Wave JS only marks a node dead after a failed send, and it never sends to a
sleeping node, so the contact keeps reading "closed / 100%" forever. Found in
the field on a Ring contact that was silent for eleven months unflagged. The
only trace is its last_seen timestamp going stale.
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
    CONF_AUTO_RECOVERY,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_GRACE_PERIOD,
    CONF_RECOVERY_DELAY,
    CONF_SILENT_EXCLUDE_DEVICES,
    CONF_SILENT_HOURS,
    CONF_STARTUP_GRACE,
    DOMAIN,
    EVENT_ENTITY_DOWN,
    EVENT_ENTITY_RECOVERED,
    REASON_SILENT,
    REASON_STATE,
)
from custom_components.sensor_sentinel.coordinator import SentinelCoordinator
from custom_components.sensor_sentinel.notify import SentinelNotifier

_BASE = {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0, CONF_SILENT_HOURS: 48}


def _ago(hours: float) -> str:
    return (dt_util.utcnow() - timedelta(hours=hours)).isoformat()


@pytest.fixture
def zwave_entry(hass):
    entry = MockConfigEntry(domain="zwave_js")
    entry.add_to_hass(hass)
    return entry


def _contact(hass, zwave_entry, slug: str, node: int, last_seen: str, battery=100):
    """A Z-Wave contact sensor device, shaped like the real Ring one."""
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

    # Non-diagnostic siblings, like the Ring's "battery is disconnected" sensor.
    _make("binary_sensor", "-113-0-batt-disc", f"{slug}_battery_is_disconnected")
    _make("binary_sensor", "-113-0-aa", f"{slug}_aa")  # sorts before the contact
    contact = _make("binary_sensor", "-113-0-door", slug, original_device_class="door")
    tamper = _make(
        "binary_sensor",
        "-113-0-tamper",
        f"{slug}_tampering",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    seen = _make(
        "sensor",
        ".statistics_last_seen",
        f"{slug}_last_seen",
        original_device_class="timestamp",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    batt = _make(
        "sensor",
        "-128-0-level",
        f"{slug}_battery",
        original_device_class="battery",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    hass.states.async_set(contact, "off")
    hass.states.async_set(tamper, "off")
    hass.states.async_set(seen, last_seen)
    hass.states.async_set(batt, str(battery))
    return device.id, contact, seen


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


async def test_silent_device_raises_one_incident_and_clears(hass, zwave_entry):
    device_id, contact, seen = _contact(hass, zwave_entry, "front_door", 21, _ago(72))
    _, fresh, fresh_seen = _contact(hass, zwave_entry, "back_door", 22, _ago(47))
    downs = async_capture_events(hass, EVENT_ENTITY_DOWN)
    recoveries = async_capture_events(hass, EVENT_ENTITY_RECOVERED)

    coordinator = await _start(hass, _BASE)

    # One incident for the device, keyed on the contact — not on its tamper,
    # last_seen or battery entities.
    assert list(coordinator._down) == [contact]
    inc = coordinator._down[contact]
    assert inc.reason == REASON_SILENT
    assert inc.device_id == device_id
    assert inc.state == "off"  # still "closed": that is the whole problem
    assert inc.name == "Front Door"  # the device, not one of its entities
    assert inc.battery == 100
    assert inc.silent_for_hours == 72
    assert inc.since == inc.last_seen  # duration counts from going quiet
    assert coordinator._build_snapshot().count == 1
    assert downs == []  # already silent before we looked: seeded quietly

    # The other device crosses the threshold later; housekeeping announces it.
    hass.states.async_set(fresh_seen, _ago(49))
    await hass.async_block_till_done()
    assert fresh not in coordinator._down  # the hot path does not scan
    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert fresh in coordinator._down
    assert [e.data["entity_id"] for e in downs] == [fresh]
    assert downs[0].data["reason"] == REASON_SILENT

    # The front door is heard from again: cleared at once, via the hot path.
    hass.states.async_set(seen, dt_util.utcnow().isoformat())
    await hass.async_block_till_done()
    assert contact not in coordinator._down
    assert device_id not in coordinator._silent
    assert [e.data["entity_id"] for e in recoveries] == [contact]
    assert recoveries[0].data["reason"] == REASON_SILENT
    assert coordinator._recovered_today == 1

    # ...and it stays clear on the next tick.
    coordinator._housekeeping()
    assert contact not in coordinator._down
    await _stop(hass, coordinator)


async def test_contact_state_change_does_not_clear_silent(hass, zwave_entry):
    """Only last_seen moving clears it. A state write without fresh contact
    (a restore, an attribute refresh) says nothing about the device's health."""
    _, contact, _ = _contact(hass, zwave_entry, "dining_slider", 30, _ago(100))
    coordinator = await _start(hass, _BASE)
    assert contact in coordinator._down

    hass.states.async_set(contact, "on")
    await hass.async_block_till_done()
    assert coordinator._down[contact].state == "on"
    assert coordinator._recovered_today == 0
    await _stop(hass, coordinator)


async def test_off_by_default(hass, zwave_entry):
    _, contact, _ = _contact(hass, zwave_entry, "front_door", 21, _ago(24 * 300))
    coordinator = await _start(hass, {CONF_STARTUP_GRACE: 0, CONF_GRACE_PERIOD: 0})
    coordinator._housekeeping()
    assert coordinator._down == {}
    assert coordinator._silent == {}
    await _stop(hass, coordinator)


async def test_device_and_entity_exclusions(hass, zwave_entry):
    remote_id, remote, _ = _contact(hass, zwave_entry, "nanomote", 40, _ago(500))
    _, ignored, _ = _contact(hass, zwave_entry, "garage", 41, _ago(500))
    _, flagged, _ = _contact(hass, zwave_entry, "window", 42, _ago(500))
    coordinator = await _start(
        hass,
        {
            **_BASE,
            CONF_SILENT_EXCLUDE_DEVICES: [remote_id],
            CONF_EXCLUDED_ENTITIES: [ignored],
            # Domain rules are about which *states* are normal; they must not
            # hide a dead device.
            CONF_EXCLUDED_DOMAINS: ["binary_sensor"],
        },
    )
    assert list(coordinator._down) == [flagged]
    await _stop(hass, coordinator)


async def test_integration_exclusion(hass, zwave_entry):
    _contact(hass, zwave_entry, "front_door", 21, _ago(500))
    coordinator = await _start(hass, {**_BASE, CONF_EXCLUDED_INTEGRATIONS: ["zwave_js"]})
    assert coordinator._down == {}
    await _stop(hass, coordinator)


async def test_no_double_report_when_device_already_down(hass, zwave_entry):
    _, contact, _ = _contact(hass, zwave_entry, "front_door", 21, _ago(72))
    _, other, _ = _contact(hass, zwave_entry, "stairwell", 23, _ago(72))
    _, guest, _ = _contact(hass, zwave_entry, "guest_window", 24, _ago(72))
    hass.states.async_set(contact, "unavailable")
    # Only a sibling (not the entity we'd key on) is down for this one.
    hass.states.async_set("binary_sensor.guest_window_tampering", "unavailable")
    coordinator = await _start(hass, _BASE)

    assert coordinator._down[contact].reason == REASON_STATE
    assert coordinator._down[other].reason == REASON_SILENT
    assert guest not in coordinator._down

    # A silent device that then also goes unavailable on a sibling entity is
    # reported by the state incident alone — and that is not a recovery.
    tamper = "binary_sensor.stairwell_tampering"
    hass.states.async_set(tamper, "unavailable")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert coordinator._down[tamper].reason == REASON_STATE
    coordinator._housekeeping()
    assert other not in coordinator._down
    assert coordinator._recovered_today == 0
    await _stop(hass, coordinator)


async def test_reload_removal_keeps_incident_without_reannouncing(hass, zwave_entry):
    """A Z-Wave JS reload removes every entity for a moment. Dropping silent
    incidents on that would re-announce every one of them on the next tick."""
    _, contact, _ = _contact(hass, zwave_entry, "front_door", 21, _ago(72))
    coordinator = await _start(hass, _BASE)
    downs = async_capture_events(hass, EVENT_ENTITY_DOWN)

    hass.states.async_remove(contact)
    await hass.async_block_till_done()
    hass.states.async_set(contact, "off")
    await hass.async_block_till_done()
    coordinator._housekeeping()
    await hass.async_block_till_done()

    assert coordinator._down[contact].reason == REASON_SILENT
    assert downs == []
    await _stop(hass, coordinator)


async def test_auto_recovery_leaves_silent_devices_alone(hass, zwave_entry, monkeypatch):
    calls: list = []

    async def _record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(hass.config_entries, "async_reload", _record)
    pings = async_mock_service(hass, "zwave_js", "ping")
    _contact(hass, zwave_entry, "front_door", 21, _ago(72))
    coordinator = await _start(
        hass, {**_BASE, CONF_AUTO_RECOVERY: True, CONF_RECOVERY_DELAY: 30}
    )
    coordinator._housekeeping()
    await hass.async_block_till_done()
    assert calls == []
    assert pings == []
    await _stop(hass, coordinator)


async def test_notification_wording(hass):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    notifier = SentinelNotifier(hass, entry)
    message = notifier._compose(
        [
            {
                "entity_id": "binary_sensor.front_door",
                "name": "Front Door",
                "reason": REASON_SILENT,
                "silent_for_hours": 24 * 336,
                "battery": 100,
            },
            {"entity_id": "light.lamp", "name": "Lamp", "integration": "hue"},
        ],
        [{"entity_id": "binary_sensor.window", "name": "Window", "reason": REASON_SILENT}],
        [],
    )
    assert "**Silent**" in message
    assert "Front Door — not heard from in 336d, last reported battery 100%" in message
    assert "**Down**" in message and "- Lamp" in message
    assert "- Window (heard from again)" in message
