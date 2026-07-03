"""Shared base for Sensor Sentinel entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import SentinelCoordinator


class SentinelEntity(CoordinatorEntity[SentinelCoordinator]):
    """Base class: attaches every entity to one 'Sensor Sentinel' device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SentinelCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Sensor Sentinel",
            model="Watchdog",
            entry_type=DeviceEntryType.SERVICE,
        )
