"""Problem binary sensor for Sensor Sentinel."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_MANAGER, DOMAIN
from .entity import SentinelEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_MANAGER]
    async_add_entities([SentinelProblemBinarySensor(coordinator)])


class SentinelProblemBinarySensor(SentinelEntity, BinarySensorEntity):
    """On when at least one entity is currently down."""

    _attr_name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_problem"
        # Force a clean, un-prefixed object_id (binary_sensor.sentinel_problem)
        # rather than the device-name-derived binary_sensor.sensor_sentinel_* default.
        self.entity_id = "binary_sensor.sentinel_problem"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.count > 0
