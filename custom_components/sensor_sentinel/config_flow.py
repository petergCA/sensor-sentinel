"""Config and options flow for Sensor Sentinel.

The options flow *is* the exclusions UI (PRD §4.5, §4a): rules over hand-edited
YAML, plus a dry-run preview that shows exactly which currently-down entities a
proposed rule set would silence before you save — no blind over-exclusion.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_BAD_STATES,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXCLUDED_PATTERNS,
    CONF_GRACE_PERIOD,
    CONF_INTEGRATION_THRESHOLD,
    CONF_NOTIFY_TARGETS,
    CONF_PERSISTENT_NOTIFICATION,
    DATA_MANAGER,
    DEFAULT_BAD_STATES,
    DEFAULT_EXCLUDED_DOMAINS,
    DEFAULT_GRACE_PERIOD,
    DEFAULT_INTEGRATION_THRESHOLD,
    DOMAIN,
    NAME,
)
from .exclusions import ExclusionEngine


class SentinelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance setup — there is one watchdog per HA instance."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={}, options=_DEFAULTS)
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SentinelOptionsFlow(config_entry)


_DEFAULTS: dict[str, Any] = {
    CONF_BAD_STATES: DEFAULT_BAD_STATES,
    CONF_EXCLUDED_DOMAINS: DEFAULT_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_INTEGRATIONS: [],
    CONF_EXCLUDED_PATTERNS: [],
    CONF_EXCLUDED_ENTITIES: [],
    CONF_GRACE_PERIOD: DEFAULT_GRACE_PERIOD,
    CONF_INTEGRATION_THRESHOLD: DEFAULT_INTEGRATION_THRESHOLD,
    CONF_NOTIFY_TARGETS: [],
    CONF_PERSISTENT_NOTIFICATION: True,
}


def _tag_selector(current: list[str]) -> selector.SelectSelector:
    """A free-text, multi-value tag input pre-seeded with the current values."""
    return _pick_selector(current, current)


def _pick_selector(options: list[str], current: list[str]) -> selector.SelectSelector:
    """Multi-select dropdown offering ``options`` (plus any current values), while
    still allowing a typed custom entry for values not present right now."""
    merged = sorted(set(options) | set(current))
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=merged,
            multiple=True,
            custom_value=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _present_integrations(hass) -> list[str]:
    """The platform (integration) names that currently own at least one entity.

    This is exactly what an integration-exclusion rule matches on, so offering
    these as options guarantees a picked value actually excludes something.
    """
    registry = er.async_get(hass)
    return sorted({e.platform for e in registry.entities.values() if e.platform})


def _present_domains(hass) -> list[str]:
    """The entity domains currently present in the state machine."""
    return sorted({state.entity_id.partition(".")[0] for state in hass.states.async_all()})


class SentinelOptionsFlow(OptionsFlow):
    """Edit exclusion rules, then preview before saving."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._proposed: dict[str, Any] = {}

    def _current(self, key: str) -> Any:
        return self._entry.options.get(key, _DEFAULTS[key])

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._proposed = {**self._entry.options, **user_input}
            return await self.async_step_preview()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BAD_STATES, default=self._current(CONF_BAD_STATES)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["unavailable", "unknown"], multiple=True
                    )
                ),
                vol.Optional(
                    CONF_EXCLUDED_DOMAINS, default=self._current(CONF_EXCLUDED_DOMAINS)
                ): _pick_selector(
                    _present_domains(self.hass),
                    self._current(CONF_EXCLUDED_DOMAINS),
                ),
                vol.Optional(
                    CONF_EXCLUDED_INTEGRATIONS,
                    default=self._current(CONF_EXCLUDED_INTEGRATIONS),
                ): _pick_selector(
                    _present_integrations(self.hass),
                    self._current(CONF_EXCLUDED_INTEGRATIONS),
                ),
                vol.Optional(
                    CONF_EXCLUDED_PATTERNS,
                    default=self._current(CONF_EXCLUDED_PATTERNS),
                ): _tag_selector(self._current(CONF_EXCLUDED_PATTERNS)),
                vol.Optional(
                    CONF_EXCLUDED_ENTITIES,
                    default=self._current(CONF_EXCLUDED_ENTITIES),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Required(
                    CONF_GRACE_PERIOD, default=self._current(CONF_GRACE_PERIOD)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=3600, step=5, unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_INTEGRATION_THRESHOLD,
                    default=self._current(CONF_INTEGRATION_THRESHOLD),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=100, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_NOTIFY_TARGETS, default=self._current(CONF_NOTIFY_TARGETS)
                ): _tag_selector(self._current(CONF_NOTIFY_TARGETS)),
                vol.Required(
                    CONF_PERSISTENT_NOTIFICATION,
                    default=self._current(CONF_PERSISTENT_NOTIFICATION),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what the proposed rules would newly silence, then save."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._proposed)

        newly_silenced = self._newly_silenced()
        if newly_silenced:
            preview = "\n".join(f"• {eid}" for eid in newly_silenced[:30])
            if len(newly_silenced) > 30:
                preview += f"\n… and {len(newly_silenced) - 30} more"
        else:
            preview = "No currently-down entities would be silenced by these rules."

        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(newly_silenced)),
                "preview": preview,
            },
        )

    def _newly_silenced(self) -> list[str]:
        """Currently-down entities that the *proposed* rules would exclude.

        Bounded: iterates only the current incident set, never the full fleet.
        """
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if not data:
            return []
        coordinator = data[DATA_MANAGER]
        engine = ExclusionEngine(self._proposed, coordinator._platform_of)
        return sorted(
            eid
            for eid in coordinator.data.incidents
            if engine.is_excluded(eid)
        )
