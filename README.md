# Sensor Sentinel

A **performance-safe, event-driven watchdog** for Home Assistant that answers
"what's unavailable right now, and why?" across the whole fleet — without ever
scanning every entity on every state change.

> Built to replace a `sensor.unavailable_entities` template that took Core down
> (2.3 GB RSS, 44% CPU) by rescanning ~6,800 entities on every event. Sensor
> Sentinel maintains an **incremental in-memory set** instead: one scan at
> startup, then O(1) work per `state_changed` event.

## Why an integration (not a template or add-on)

A template sensor can only poll or scan-per-event — neither is acceptable at
several thousand entities. An add-on can't cleanly reach the entity/device
registry. A custom integration listens on the event bus, keeps an incremental
set, enriches from cached registry maps, and exposes clean entities, events and
services.

## Features

- **Incremental detection** — a single bus listener maintains the down-set. No
  fleet-wide scan on any event; attribute payloads are hard-capped.
- **Grace window** — a per-entity debounce (default 60s) suppresses flaps: an
  entity must stay bad past the window before it becomes an incident.
- **Rules over lists** — exclude by **domain**, **integration**, **entity_id
  glob**, or **explicit entity**, all from the UI. No hand-edited YAML.
- **Dry-run preview** — before saving a rule change, see exactly which
  currently-down entities it would silence (no blind over-exclusion).
- **Grouped, debounced notifications** — a burst of drops becomes one message
  rolled up by integration, plus recovery notices.
- **Companion Lovelace card** — bundled and auto-registered, with a **visual
  editor** (no YAML needed). Shows the **full** live incident list grouped by
  integration — using each integration's **display name** (e.g. "Z-Wave JS", not
  `zwave_js`) — with one-click **snooze / exclude / why?** actions, plus a
  **ping** button on Z-Wave rows (`zwave_js.ping`) to wake a dead node. Options:
  sort integrations by down-count or alphabetically, collapse groups by default
  (off by default), toggle the Z-Wave ping button, and an optional **trend
  sparkline** of the down-count over a configurable window (in hours, from
  recorder history). The card pulls the
  complete list on demand via a `sensor_sentinel/list` websocket command, so the
  count sensor's attribute payload stays capped no matter how many entities are
  down.
- **Automation surface** — `sensor_sentinel.entity_down` /
  `entity_recovered` bus events carry full context.

## Entities

| Entity | Purpose |
| --- | --- |
| `sensor.sentinel_unavailable_count` | Live down-count; capped rollup attributes (`by_integration`, `by_area`, 25-row sample). |
| `binary_sensor.sentinel_problem` | `device_class: problem`; on when anything is down. |

## Events

| Event | Data |
| --- | --- |
| `sensor_sentinel.entity_down` | `entity_id, state, name, integration, device, area, since, flapping` |
| `sensor_sentinel.entity_recovered` | `entity_id, name` |

## Services

| Service | Description |
| --- | --- |
| `sensor_sentinel.snooze` | Mute an entity for N minutes. |
| `sensor_sentinel.unsnooze` | Clear a snooze. |
| `sensor_sentinel.exclude` | Add a permanent explicit-entity exclusion. |
| `sensor_sentinel.explain` | Return why an entity is flagged/excluded (response). |

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add this repo, category **Integration**.
2. Install **Sensor Sentinel**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Sensor Sentinel**.
4. Add the card to a dashboard: **Add Card → Custom: Sensor Sentinel Card**
   (the card is bundled with the integration — no separate install or resource
   setup needed).

## Configuration

Everything is in the integration's **Configure** dialog:

- **Bad states** — which states count as down (default `unavailable`, `unknown`).
- **Excluded domains** — defaults to the domains where `unknown` is normal
  (`button`, `event`, `image`, `input_button`, `input_text`, `remote`, `scene`,
  `stt`, `tts`, `group`).
- **Excluded integrations / globs / entities** — the rule types above.
- **Grace window** and **integration rollup threshold**.
- **Notification** targets and persistent-notification toggle.

## Performance notes

- One `hass.states` scan at startup to seed the set; never again.
- Per event: a state-set membership test and, at most, an O(rules) exclusion
  check — only for entities that are bad or already tracked.
- Registry enrichment happens per *incident* (post-grace), not per event, off
  cached registry maps refreshed only on registry-update events.
- Sensor writes are coalesced (≤1/sec) so a flapping storm can't thrash.

## Status

v0.5 — MVP plus the full-list card with a visual editor, integration display names, count/name sorting, collapse-by-default, a Z-Wave ping action, and an optional trend sparkline (detection, exclusions UI, notifications, entities/events). Auto-recovery is a planned later phase and is **not**
names, count/name sorting, collapse-by-default, and a Z-Wave ping action
(detection, exclusions UI, notifications, entities/events). Auto-recovery is a
planned later phase and is **not** included.

## License

MIT
