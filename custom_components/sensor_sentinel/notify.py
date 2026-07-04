"""Debounced, deduplicated notifications for Sensor Sentinel.

Subscribes to the same ``entity_down`` / ``entity_recovered`` bus events that
user automations get, and turns a burst of them into a *single* grouped,
human-readable notification (PRD §4.6). A mesh brown-out that drops 200 entities
produces one message, not 200.
"""

from __future__ import annotations

import logging

from homeassistant.components.persistent_notification import (
    async_create as pn_create,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_INTEGRATION_THRESHOLD,
    CONF_NOTIFY_TARGETS,
    CONF_PERSISTENT_NOTIFICATION,
    DEFAULT_INTEGRATION_THRESHOLD,
    DOMAIN,
    EVENT_ENTITY_DOWN,
    EVENT_ENTITY_RECOVERED,
    EVENT_ENTITY_STILL_DOWN,
    NAME,
)

_LOGGER = logging.getLogger(__name__)

# Collect events for this long before sending one grouped notification.
_BATCH_WINDOW = 5.0


class SentinelNotifier:
    """Batches down/recovery events into grouped notifications."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub: list[callable] = []
        self._down_batch: list[dict] = []
        self._recovered_batch: list[dict] = []
        self._still_down_batch: list[dict] = []
        self._flush_scheduled = False
        self._load_options()

    def _load_options(self) -> None:
        opts = self.entry.options
        self._targets: list[str] = list(opts.get(CONF_NOTIFY_TARGETS, []))
        self._persistent: bool = opts.get(CONF_PERSISTENT_NOTIFICATION, True)
        self._integration_threshold: int = opts.get(
            CONF_INTEGRATION_THRESHOLD, DEFAULT_INTEGRATION_THRESHOLD
        )

    def async_reload_options(self) -> None:
        self._load_options()

    def async_start(self) -> None:
        self._unsub.append(
            self.hass.bus.async_listen(EVENT_ENTITY_DOWN, self._on_down)
        )
        self._unsub.append(
            self.hass.bus.async_listen(EVENT_ENTITY_RECOVERED, self._on_recovered)
        )
        self._unsub.append(
            self.hass.bus.async_listen(EVENT_ENTITY_STILL_DOWN, self._on_still_down)
        )

    def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _on_down(self, event: Event) -> None:
        self._down_batch.append(dict(event.data))
        self._schedule_flush()

    @callback
    def _on_recovered(self, event: Event) -> None:
        self._recovered_batch.append(dict(event.data))
        self._schedule_flush()

    @callback
    def _on_still_down(self, event: Event) -> None:
        self._still_down_batch.append(dict(event.data))
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        async_call_later(self.hass, _BATCH_WINDOW, self._flush)

    @callback
    def _flush(self, _now) -> None:
        self._flush_scheduled = False
        down, self._down_batch = self._down_batch, []
        recovered, self._recovered_batch = self._recovered_batch, []
        still_down, self._still_down_batch = self._still_down_batch, []
        if not down and not recovered and not still_down:
            return

        message = self._compose(down, recovered, still_down)
        if down:
            title = f"{NAME}: {len(down)} down"
        elif recovered:
            title = f"{NAME}: {len(recovered)} recovered"
        else:
            title = f"{NAME}: {len(still_down)} still down"

        if self._persistent:
            pn_create(
                self.hass, message, title=title, notification_id=f"{DOMAIN}_incident"
            )
        for target in self._targets:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "notify",
                    target,
                    {"title": title, "message": message},
                    blocking=False,
                )
            )

    def _compose(
        self, down: list[dict], recovered: list[dict], still_down: list[dict]
    ) -> str:
        lines: list[str] = []
        if down:
            # Roll up by integration; large groups collapse to one line.
            by_integration: dict[str, list[dict]] = {}
            for inc in down:
                by_integration.setdefault(inc.get("integration") or "unknown", []).append(inc)
            lines.append("**Down**")
            for integration, incidents in sorted(by_integration.items()):
                if (
                    self._integration_threshold
                    and len(incidents) >= self._integration_threshold
                ):
                    lines.append(f"- {integration}: {len(incidents)} entities down")
                else:
                    for inc in incidents:
                        flap = " (flapping)" if inc.get("flapping") else ""
                        lines.append(f"- {inc.get('name', inc['entity_id'])}{flap}")
        if still_down:
            lines.append("")
            lines.append("**Still down**")
            for inc in still_down:
                hrs = int(inc.get("down_seconds", 0) // 3600)
                suffix = f" ({hrs}h)" if hrs else ""
                lines.append(f"- {inc.get('name', inc['entity_id'])}{suffix}")
        if recovered:
            lines.append("")
            lines.append("**Recovered**")
            for inc in recovered:
                lines.append(f"- {inc.get('name', inc['entity_id'])}")
        return "\n".join(lines)
