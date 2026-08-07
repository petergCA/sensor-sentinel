"""Auto-recovery guardrail tests.

These target the runaway found in the field: Sensor Sentinel reloaded the Sonos
config entry every ~15 minutes all night, cycling nine healthy speakers to chase
two battery portables that were simply switched off. RECOVERY_MAX_ATTEMPTS was
supposed to stop that after three tries and never did, because the reload itself
removed the entities from the state machine, the coordinator read the removal as
a recovery, and a recovery refunds the attempt budget.

The tension to hold on to: a reload genuinely DOES fix some integrations (GE
Home), so the budget must survive a failed reload while still being refunded
after a successful one.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sensor_sentinel.const import (
    CONF_AUTO_RECOVERY,
    CONF_GRACE_PERIOD,
    CONF_RECOVERY_DELAY,
    CONF_STARTUP_GRACE,
    DOMAIN,
    RECOVERY_COOLDOWN,
    RECOVERY_MAX_ATTEMPTS,
    RECOVERY_MAX_ATTEMPTS_SHARED,
)
from custom_components.sensor_sentinel.coordinator import SentinelCoordinator


class _Clock:
    """Controllable stand-in for the coordinator's ``time`` module.

    Recovery attempts are spaced by RECOVERY_COOLDOWN (15 min). A test loop
    runs in microseconds, so without moving the clock the cooldown — not the
    attempt budget — is what stops the second attempt, and the budget logic
    under test never runs at all.
    """

    def __init__(self) -> None:
        self._monotonic = 10_000.0
        self._wall = 1_700_000_000.0

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._wall += seconds


@pytest.fixture
def clock(monkeypatch):
    """Swap the name ``time`` inside the coordinator module only."""
    from custom_components.sensor_sentinel import coordinator as coordinator_mod

    fake = _Clock()
    monkeypatch.setattr(coordinator_mod, "time", fake)
    return fake


@pytest.fixture
def reloads(hass, monkeypatch):
    """Record config-entry reloads instead of performing them."""
    called: list[str] = []

    async def _fake_reload(entry_id):
        called.append(entry_id)
        return True

    monkeypatch.setattr(hass.config_entries, "async_reload", _fake_reload)
    return called


def _owned_entity(hass, entity_id: str, owner: MockConfigEntry) -> None:
    """Register an entity against a config entry, then give it a state.

    Recovery resolves the entry to reload through the entity registry, so a
    bare ``states.async_set`` entity is invisible to it.
    """
    domain, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(
        domain,
        owner.domain,
        object_id,
        config_entry=owner,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entity_id, "unavailable")


def _owner_entry(hass, domain="sonos") -> MockConfigEntry:
    owner = MockConfigEntry(domain=domain)
    owner.add_to_hass(hass)
    return owner


async def _coordinator(hass, **options) -> SentinelCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_STARTUP_GRACE: 0,
            CONF_GRACE_PERIOD: 0,
            CONF_AUTO_RECOVERY: True,
            CONF_RECOVERY_DELAY: 0,
            **options,
        },
    )
    entry.add_to_hass(hass)
    coordinator = SentinelCoordinator(hass, entry)
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator


def _age(coordinator, entity_id, seconds=3600) -> None:
    """Make an incident old enough to be eligible for recovery."""
    coordinator._down[entity_id].since = (
        dt_util.utcnow() - timedelta(seconds=seconds)
    ).isoformat()


async def _goes_down(hass, entity_id: str) -> None:
    """Take an entity unavailable and let the grace timer promote it."""
    hass.states.async_set(entity_id, "unavailable")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()


async def _quiesce(hass, coordinator) -> None:
    """Shut down cleanly.

    Incident writes are debounced through the Store, so tearing down straight
    after a state change leaves that timer pending and HA's test harness fails
    the test for it. Let the clock run out first.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    await coordinator.async_shutdown()


async def _reload_cycle(hass, coordinator, entity_ids, returns_healthy) -> None:
    """Simulate what async_reload actually does to an entry's entities.

    Unload removes every entity from the state machine; setup puts them back.
    This is the step that used to launder a failed reload into a "recovery".
    """
    for entity_id in entity_ids:
        hass.states.async_remove(entity_id)
    await hass.async_block_till_done()
    for entity_id in entity_ids:
        hass.states.async_set(
            entity_id, "on" if returns_healthy.get(entity_id) else "unavailable"
        )
    await hass.async_block_till_done()
    # Promotion to the down-set runs off a grace timer (zero-length here, but
    # still a timer), so the clock has to move for it to land.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()


async def test_failed_reload_does_not_refund_its_own_budget(hass, reloads, clock):
    """The Sonos runaway: a reload that fixes nothing must stop.

    The whole entry is unhealthy here, so this exercises the budget accounting
    itself rather than the shared-entry cap. Pre-fix this never terminates,
    because the reload's own entity removal refunds the attempt.
    """
    _owned_entity(hass, "switch.portable", _owner_entry(hass))
    coordinator = await _coordinator(hass)
    _age(coordinator, "switch.portable")

    for _ in range(10):
        coordinator._housekeeping()
        await hass.async_block_till_done()
        await _reload_cycle(
            hass, coordinator, ["switch.portable"], {"switch.portable": False}
        )
        _age(coordinator, "switch.portable")
        clock.advance(RECOVERY_COOLDOWN + 1)

    assert len(reloads) == RECOVERY_MAX_ATTEMPTS
    assert "switch.portable" in coordinator._recovery_exhausted
    await _quiesce(hass, coordinator)


async def test_reload_that_works_is_refunded_and_can_run_again(hass, reloads, clock):
    """The GE Home case: a reload that fixes it stays available next time."""
    _owned_entity(hass, "switch.ge_oven", _owner_entry(hass, "ge_home"))
    coordinator = await _coordinator(hass)
    _age(coordinator, "switch.ge_oven")

    for _ in range(4):
        coordinator._housekeeping()
        await hass.async_block_till_done()
        # The reload works: the entity comes back healthy — and, crucially,
        # comes back UNTRACKED, because the removal already dropped it.
        await _reload_cycle(
            hass, coordinator, ["switch.ge_oven"], {"switch.ge_oven": True}
        )
        assert not coordinator._recovery_attempts, "a working reload must refund"
        # It fails again later; a fresh incident should get a fresh attempt.
        await _goes_down(hass, "switch.ge_oven")
        _age(coordinator, "switch.ge_oven")
        clock.advance(RECOVERY_COOLDOWN + 1)

    assert len(reloads) == 4, "a reload that works should never be given up on"
    assert not coordinator._recovery_exhausted
    await _quiesce(hass, coordinator)


async def test_removal_is_not_counted_as_a_recovery(hass):
    """Entities vanishing on unload must not inflate recovered_today."""
    hass.states.async_set("switch.portable", "unavailable")
    coordinator = await _coordinator(hass, **{CONF_AUTO_RECOVERY: False})
    before = coordinator._recovered_today

    hass.states.async_remove("switch.portable")
    await hass.async_block_till_done()

    assert "switch.portable" not in coordinator._down
    assert coordinator._recovered_today == before
    await _quiesce(hass, coordinator)


async def test_genuine_recovery_still_counts(hass):
    """The guard above must not suppress real recoveries."""
    hass.states.async_set("switch.lamp", "unavailable")
    coordinator = await _coordinator(hass, **{CONF_AUTO_RECOVERY: False})
    before = coordinator._recovered_today

    hass.states.async_set("switch.lamp", "on")
    await hass.async_block_till_done()

    assert "switch.lamp" not in coordinator._down
    assert coordinator._recovered_today == before + 1
    await _quiesce(hass, coordinator)


async def test_shared_entry_gets_a_smaller_budget(hass, reloads, clock, monkeypatch):
    """Healthy siblings mean a reload is expensive — spend less before quitting."""
    monkeypatch.setattr(
        SentinelCoordinator,
        "_entry_has_available_entities",
        lambda self, entry_id, exclude: True,
    )
    _owned_entity(hass, "switch.portable", _owner_entry(hass))
    coordinator = await _coordinator(hass)
    _age(coordinator, "switch.portable")

    for _ in range(6):
        coordinator._housekeeping()
        await hass.async_block_till_done()
        await _reload_cycle(
            hass, coordinator, ["switch.portable"], {"switch.portable": False}
        )
        _age(coordinator, "switch.portable")
        clock.advance(RECOVERY_COOLDOWN + 1)

    assert len(reloads) == RECOVERY_MAX_ATTEMPTS_SHARED
    assert RECOVERY_MAX_ATTEMPTS_SHARED < RECOVERY_MAX_ATTEMPTS
    await _quiesce(hass, coordinator)


async def test_one_reload_per_entry_even_with_many_down_entities(hass, reloads, clock):
    """The real Sonos shape: one offline speaker owning fourteen entities.

    The field log showed fourteen reload dispatches of the same config entry
    inside ten milliseconds — the attempt budget is per entity, but a reload
    is per entry. Every sibling must still SPEND its attempt, or they simply
    take turns reloading the entry one per tick instead.
    """
    owner = _owner_entry(hass)
    entity_ids = [f"switch.sonos_portable_{i}" for i in range(14)]
    for entity_id in entity_ids:
        _owned_entity(hass, entity_id, owner)
    coordinator = await _coordinator(hass)
    for entity_id in entity_ids:
        _age(coordinator, entity_id)

    coordinator._housekeeping()
    await hass.async_block_till_done()

    assert len(reloads) == 1, "one entry, one reload — not one per entity"
    assert all(coordinator._recovery_attempts[e] == 1 for e in entity_ids), (
        "every sibling must record the attempt the shared reload made for it"
    )

    # A second pass must not reload again: the whole entry is down, so the
    # budget is 3, but the cooldown holds them all together.
    clock.advance(RECOVERY_COOLDOWN + 1)
    await _reload_cycle(
        hass, coordinator, entity_ids, dict.fromkeys(entity_ids, False)
    )
    for entity_id in entity_ids:
        _age(coordinator, entity_id)
    coordinator._housekeeping()
    await hass.async_block_till_done()

    assert len(reloads) == 2, "second pass dispatches exactly one more reload"
    await _quiesce(hass, coordinator)
