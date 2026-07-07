"""Sensor Sentinel — a performance-safe, event-driven unavailable-entity watchdog."""

from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DATA_MANAGER, DOMAIN
from .coordinator import SentinelCoordinator
from .notify import SentinelNotifier
from .services import async_register_services, async_unregister_services
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

CARD_FILENAME = "sensor-sentinel-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve and auto-load the bundled Lovelace card (no manual resource setup)."""
    if hass.data.get(f"{DOMAIN}_card_registered"):
        return
    card_path = os.path.join(os.path.dirname(__file__), "www", CARD_FILENAME)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, card_path, cache_headers=False)]
    )
    add_extra_js_url(hass, CARD_URL)
    hass.data[f"{DOMAIN}_card_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sensor Sentinel from a config entry."""
    coordinator = SentinelCoordinator(hass, entry)
    await coordinator.async_start()

    notifier = SentinelNotifier(hass, entry)
    notifier.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_MANAGER: coordinator,
        "notifier": notifier,
    }

    await async_register_services(hass)
    async_register_websocket(hass)
    # The bundled card is a convenience, not core function. Never let a static-
    # path or frontend hiccup fail setup and leave the integration in a
    # "Configuration Error" state — log and carry on.
    try:
        await _async_register_card(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Could not register the Sensor Sentinel dashboard card; "
            "the integration will run without it",
            exc_info=True,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data[DATA_MANAGER].async_shutdown()
        data["notifier"].async_stop()
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply edits from the exclusions UI without a full reload."""
    data = hass.data[DOMAIN][entry.entry_id]
    await data[DATA_MANAGER].async_options_updated()
    data["notifier"].async_reload_options()
