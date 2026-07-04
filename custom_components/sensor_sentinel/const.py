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
# Fired when an entity has stayed down past the re-alert window (opt-in).
EVENT_ENTITY_STILL_DOWN = f"{DOMAIN}.entity_still_down"

# ---------------------------------------------------------------------------
# Config / options keys.
# ---------------------------------------------------------------------------
CONF_BAD_STATES = "bad_states"
CONF_EXCLUDED_DOMAINS = "excluded_domains"
CONF_EXCLUDED_INTEGRATIONS = "excluded_integrations"
CONF_EXCLUDED_PATTERNS = "excluded_patterns"
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_GRACE_PERIOD = "grace_period"
CONF_STARTUP_GRACE = "startup_grace"
CONF_INTEGRATION_THRESHOLD = "integration_threshold"
CONF_NOTIFY_TARGETS = "notify_targets"
CONF_PERSISTENT_NOTIFICATION = "persistent_notification"
CONF_REALERT_HOURS = "realert_hours"
CONF_STALE_DAYS = "stale_days"
CONF_AUTO_RECOVERY = "auto_recovery"
CONF_RECOVERY_DELAY = "recovery_delay"

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

# At startup, entities already bad are held for this many seconds before being
# counted, so transient boot-time unknowns (MQTT retained, Z-Wave interview,
# device_trackers) don't spike the count. Anything that recovers inside this
# window is never counted. Promotion after warmup is silent (no notifications).
DEFAULT_STARTUP_GRACE = 120

# One incident is raised for an integration once this many of its entities are
# down at once (flapping-storm rollup). 0 disables the rollup threshold.
DEFAULT_INTEGRATION_THRESHOLD = 5

# Re-alert on entities still down after this many hours. 0 disables re-alerts.
DEFAULT_REALERT_HOURS = 0

# Auto-drop incidents that have been down longer than this many days (they are
# almost certainly removed/retired devices). 0 disables. Stale incidents stay
# visible (flagged) in the full list but leave the count and notifications.
DEFAULT_STALE_DAYS = 0

# Opt-in self-healing. Off by default so the integration never acts on its own.
DEFAULT_AUTO_RECOVERY = False
# Wait this long after an entity goes down before attempting recovery.
DEFAULT_RECOVERY_DELAY = 300
# Guardrails: at most this many attempts per entity, spaced by the cooldown.
RECOVERY_MAX_ATTEMPTS = 3
RECOVERY_COOLDOWN = 900
# How often the periodic housekeeping tick runs (re-alert / stale / recovery).
HOUSEKEEPING_INTERVAL = 300

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
