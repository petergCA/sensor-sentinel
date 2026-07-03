"""Count sensor for Sensor Sentinel."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
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
    async_add_entities([SentinelCountSensor(coordinator)])


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

    @property
    def native_value(self) -> int:
        return self.coordinator.data.count

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        incidents = list(data.incidents.values())
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
        }
