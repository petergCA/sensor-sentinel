"""Constants for the Sensor Sentinel integration."""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

DOMAIN = "sensor_sentinel"
NAME = "Sensor Sentinel"

# ---------------------------------------------------------------------------
# Events fired on the HA event bus for user automations.
# ---------------------------------------------------------------------------
EVENT_ENTITY_DOWN = f"{DOMAIN}.entity_down"
EVENT_ENTITY_RECOVERED = f"{DOMAIN}.entity_recovered"

# ---------------------------------------------------------------------------
# Config / options keys.
# ---------------------------------------------------------------------------
CONF_BAD_STATES = "bad_states"
CONF_EXCLUDED_DOMAINS = "excluded_domains"
CONF_EXCLUDED_INTEGRATIONS = "excluded_integrations"
CONF_EXCLUDED_PATTERNS = "excluded_patterns"
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_GRACE_PERIOD = "grace_period"
CONF_INTEGRATION_THRESHOLD = "integration_threshold"
CONF_NOTIFY_TARGETS = "notify_targets"
CONF_PERSISTENT_NOTIFICATION = "persistent_notification"

# ---------------------------------------------------------------------------
# Defaults — ported from the jazzyisj template semantics (see PRD §3, Q1).
# ---------------------------------------------------------------------------

# A "bad" state is any state that fails Home Assistant's `has_value` test.
# We treat unavailable + unknown as bad; the benign-by-design noise is removed
# by the default excluded domains below, not by narrowing the state set.
DEFAULT_BAD_STATES: list[str] = [STATE_UNAVAILABLE, STATE_UNKNOWN]

# Domains where `unknown` is normal and should not be flagged. Drop the domain,
# not the state (PRD §3 / Q1).
DEFAULT_EXCLUDED_DOMAINS: list[str] = [
    "button",
    "event",
    "group",
    "image",
    "input_button",
    "input_text",
    "remote",
    "scene",
    "stt",
    "tts",
]

# Flapping guard: ignore anything that has been bad for less than this many
# seconds. Ported from the template's 60s `last_changed` grace window, but
# implemented as a per-entity debounce timer instead of a fleet-wide scan.
DEFAULT_GRACE_PERIOD = 60

# One incident is raised for an integration once this many of its entities are
# down at once (flapping-storm rollup). 0 disables the rollup threshold.
DEFAULT_INTEGRATION_THRESHOLD = 5

# ---------------------------------------------------------------------------
# Performance guardrails (PRD §6 — "performance is the product").
# ---------------------------------------------------------------------------

# Hard cap on how many entities we ever serialise into a sensor attribute.
# Detail lives in events / the Store, never in a giant attribute payload.
MAX_ATTR_ENTITIES = 25

# Coalesce sensor state writes during flapping storms: at most one push per
# this many seconds.
STATE_WRITE_DEBOUNCE = 1.0

# hass.data storage keys.
DATA_MANAGER = "manager"

# Store (`.storage`) version for live incident/audit state.
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.incidents"
