"""Services that back the card's one-click actions (PRD §4a)."""

from __future__ import annotations

import time
import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv

from .const import CONF_EXCLUDED_ENTITIES, DATA_MANAGER, DOMAIN

SERVICE_SNOOZE = "snooze"
SERVICE_UNSNOOZE = "unsnooze"
SERVICE_EXCLUDE = "exclude"
SERVICE_EXPLAIN = "explain"

_SNOOZE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("minutes", default=60): vol.All(int, vol.Range(min=1, max=10080)),
    }
)
_ENTITY_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id})


def _coordinator(hass: HomeAssistant):
    """Return the single instance's coordinator (or None)."""
    for data in hass.data.get(DOMAIN, {}).values():
        return data[DATA_MANAGER]
    return None


async def async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once."""

    async def snooze(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        if coordinator:
            until = time.time() + call.data["minutes"] * 60
            coordinator.async_snooze(call.data["entity_id"], until)

    async def unsnooze(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        if coordinator:
            coordinator.async_unsnooze(call.data["entity_id"])

    async def exclude(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        if not coordinator:
            return
        entity_id = call.data["entity_id"]
        current = list(coordinator.entry.options.get(CONF_EXCLUDED_ENTITIES, []))
        if entity_id not in current:
            current.append(entity_id)
            hass.config_entries.async_update_entry(
                coordinator.entry,
                options={**coordinator.entry.options, CONF_EXCLUDED_ENTITIES: current},
            )

    async def explain(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        if not coordinator:
            return {"result": "unavailable", "detail": "integration not loaded"}
        return coordinator.explain(call.data["entity_id"])

    hass.services.async_register(DOMAIN, SERVICE_SNOOZE, snooze, _SNOOZE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNSNOOZE, unsnooze, _ENTITY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_EXCLUDE, exclude, _ENTITY_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPLAIN,
        explain,
        _ENTITY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (SERVICE_SNOOZE, SERVICE_UNSNOOZE, SERVICE_EXCLUDE, SERVICE_EXPLAIN):
        hass.services.async_remove(DOMAIN, service)
