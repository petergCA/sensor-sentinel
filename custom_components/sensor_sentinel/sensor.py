"""Sensors for Sensor Sentinel."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_MANAGER, DOMAIN, MAX_ATTR_ENTITIES
from .entity import SentinelEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_MANAGER]
    async_add_entities(
        [
            SentinelCountSensor(coordinator),
            SentinelRecoveredTodaySensor(coordinator),
            SentinelLongestDownSensor(coordinator),
        ]
    )


class SentinelCountSensor(SentinelEntity, SensorEntity):
    """Number of entities currently down (past the grace window).

    Attributes are deliberately capped (PRD §4.7): rollups plus at most
    ``MAX_ATTR_ENTITIES`` sample rows. The full list lives in the events/Store,
    never in a giant attribute payload that would get re-broadcast on every
    state change — the exact failure mode that took Core down (PRD §1).
    """

    _attr_name = "Unavailable count"
    _attr_icon = "mdi:radar"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "entities"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_unavailable_count"
        # Force a clean, un-prefixed object_id (sensor.sentinel_unavailable_count)
        # rather than the device-name-derived sensor.sensor_sentinel_* default.
        self.entity_id = "sensor.sentinel_unavailable_count"

    @property
    def native_value(self) -> int:
        return self.coordinator.data.count

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        incidents = [inc for inc in data.incidents.values() if not inc.stale]
        sample = [
            {
                "entity_id": inc.entity_id,
                "name": inc.name,
                "integration": inc.integration,
                "area": inc.area,
                "state": inc.state,
                "since": inc.since,
                "flapping": inc.flapping,
            }
            for inc in incidents[:MAX_ATTR_ENTITIES]
        ]
        return {
            "by_integration": data.by_integration,
            "by_area": data.by_area,
            "entities": sample,
            "truncated": len(incidents) > MAX_ATTR_ENTITIES,
            "recovered_today": data.recovered_today,
            "longest_down": data.longest_down,
            "stale_count": data.stale_count,
        }


class SentinelRecoveredTodaySensor(SentinelEntity, SensorEntity):
    """Count of entities that recovered so far today (resets at local midnight)."""

    _attr_name = "Recovered today"
    _attr_icon = "mdi:backup-restore"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "entities"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_recovered_today"
        self.entity_id = "sensor.sentinel_recovered_today"

    @property
    def native_value(self) -> int:
        return self.coordinator.data.recovered_today


class SentinelLongestDownSensor(SentinelEntity, SensorEntity):
    """The entity that has been down the longest (name; details in attributes)."""

    _attr_name = "Longest down"
    _attr_icon = "mdi:timer-alert-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_longest_down"
        self.entity_id = "sensor.sentinel_longest_down"

    @property
    def native_value(self) -> str:
        longest = self.coordinator.data.longest_down
        # Cap to HA's 255-char state limit.
        return (longest["name"] if longest else "none")[:255]

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.data.longest_down or {}
