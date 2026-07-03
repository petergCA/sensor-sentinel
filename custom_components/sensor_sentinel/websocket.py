"""Websocket API: the full unavailable-entity list on demand.

The count sensor's attribute payload is deliberately capped
(``MAX_ATTR_ENTITIES``) so it never re-broadcasts a large list on every state
change — the failure mode that took Core down (PRD §1, §6). The companion card
fetches the *complete* current down-set through this command instead, so the
perf-safe attribute cap and a full-list view can coexist.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DATA_MANAGER, DOMAIN

WS_TYPE_LIST = f"{DOMAIN}/list"


@callback
def _coordinator(hass: HomeAssistant):
    """Return the single instance's coordinator (or None)."""
    for data in hass.data.get(DOMAIN, {}).values():
        return data[DATA_MANAGER]
    return None


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST})
@callback
def _ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Return every currently-down entity as a list of incident dicts."""
    coordinator = _coordinator(hass)
    incidents = coordinator.all_incidents() if coordinator else []
    connection.send_result(msg["id"], {"count": len(incidents), "incidents": incidents})


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the websocket command once for the whole integration."""
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    websocket_api.async_register_command(hass, _ws_list)
    hass.data[f"{DOMAIN}_ws_registered"] = True
