/**
 * Sensor Sentinel — companion Lovelace card.
 *
 * Buildless vanilla custom element (no Lit/npm) so it ships inside the
 * integration and needs zero manual resource setup. Renders the live incident
 * list from sensor.sentinel_unavailable_count and drives the one-click actions
 * (snooze / exclude / why?) through the sensor_sentinel.* services.
 */

const DEFAULT_ENTITY = "sensor.sentinel_unavailable_count";

const CONFIG_DEFAULTS = {
  entity: DEFAULT_ENTITY,
  sort: "count",
  collapse_by_default: false,
  zwave_ping: true,
  sparkline_hours: 0,
};

// Schema for the visual (ha-form) editor.
const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor" } } },
  {
    name: "sort",
    selector: {
      select: {
        mode: "dropdown",
        options: [
          { value: "count", label: "By count (most down first)" },
          { value: "name", label: "Alphabetical" },
        ],
      },
    },
  },
  { name: "collapse_by_default", selector: { boolean: {} } },
  { name: "zwave_ping", selector: { boolean: {} } },
  {
    name: "sparkline_hours",
    selector: {
      number: { min: 0, max: 168, step: 1, mode: "box", unit_of_measurement: "h" },
    },
  },
];

const EDITOR_LABELS = {
  entity: "Count entity",
  sort: "Sort integrations",
  collapse_by_default: "Collapse integrations by default",
  zwave_ping: "Show Z-Wave ping button",
  sparkline_hours: "Trend sparkline window (hours, 0 = off)",
};

class SensorSentinelCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("sensor-sentinel-card-editor");
  }

  static getStubConfig() {
    return { ...CONFIG_DEFAULTS };
  }

  setConfig(config) {
    this._config = { ...CONFIG_DEFAULTS, ...config };
    this._collapsed = this._collapsed || {};
    // Invalidate any cached history so a changed window refetches promptly.
    this._historyFetchedAt = 0;
    this._history = null;
  }

  set hass(hass) {
    this._hass = hass;
    // Re-pull the full incident list whenever the count sensor updates. The
    // sensor attribute only carries a capped sample; the complete list comes
    // from the sensor_sentinel/list websocket command.
    const st = this._stateObj();
    const stamp = st ? st.last_updated : null;
    if (stamp && stamp !== this._stamp) {
      this._stamp = stamp;
      this._fetchFull();
    }
    this._maybeFetchHistory();
    this._render();
  }

  async _maybeFetchHistory() {
    const hours = Number(this._config.sparkline_hours) || 0;
    if (!hours || !this._hass) {
      this._history = null;
      return;
    }
    // Self-throttle: at most one recorder query per 30s (set hass fires often).
    const now = Date.now();
    if (this._historyFetching || now - (this._historyFetchedAt || 0) < 30000) return;
    this._historyFetching = true;
    this._historyFetchedAt = now;
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(now - hours * 3600 * 1000).toISOString(),
        end_time: new Date(now).toISOString(),
        entity_ids: [this._config.entity],
        minimal_response: true,
        no_attributes: true,
      });
      const series = (res && res[this._config.entity]) || [];
      this._history = series
        .map((s) => ({
          t: (s.lu ?? s.lc) ? (s.lu ?? s.lc) * 1000 : new Date(s.last_updated || s.last_changed).getTime(),
          v: Number(s.s ?? s.state),
        }))
        .filter((p) => Number.isFinite(p.v) && Number.isFinite(p.t));
    } catch (e) {
      this._history = null;
    } finally {
      this._historyFetching = false;
      this._render();
    }
  }

  _sparklineSVG() {
    const hours = Number(this._config.sparkline_hours) || 0;
    if (!hours) return "";
    const pts = this._history;
    if (!pts || pts.length < 2) {
      return `<div class="ss-spark-wrap"><div class="ss-spark-cap">${hours}h trend</div><div class="ss-spark-empty">gathering history…</div></div>`;
    }
    const W = 400;
    const H = 40;
    const pad = 2;
    const now = Date.now();
    const t0 = now - hours * 3600 * 1000;
    const span = now - t0 || 1;
    const vals = pts.map((p) => p.v);
    const vmax = Math.max(...vals);
    const vmin = Math.min(...vals);
    const range = vmax - vmin || 1;
    const xy = (p) => {
      const fx = Math.min(1, Math.max(0, (p.t - t0) / span));
      const x = pad + fx * (W - 2 * pad);
      const y = pad + (1 - (p.v - vmin) / range) * (H - 2 * pad);
      return [x, y];
    };
    const line = pts.map((p) => xy(p).map((n) => n.toFixed(1)).join(",")).join(" ");
    const [lx, ly] = xy(pts[pts.length - 1]);
    const area = `${pad},${H - pad} ${line} ${lx.toFixed(1)},${H - pad}`;
    return `
      <div class="ss-spark-wrap">
        <div class="ss-spark-cap">${hours}h trend · min ${vmin} / max ${vmax}</div>
        <svg class="ss-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
          <polygon class="ss-spark-area" points="${area}" />
          <polyline class="ss-spark-line" points="${line}" />
          <circle class="ss-spark-dot" cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2" />
        </svg>
      </div>`;
  }

  async _fetchFull() {
    if (this._fetching || !this._hass) return;
    this._fetching = true;
    try {
      const res = await this._hass.connection.sendMessagePromise({
        type: "sensor_sentinel/list",
      });
      this._incidents = res.incidents || [];
      this._full = true;
    } catch (e) {
      // Fall back to the capped attribute sample if the command is unavailable
      // (e.g. an older integration version behind this card).
      this._full = false;
    } finally {
      this._fetching = false;
      this._render();
    }
  }

  getCardSize() {
    return 4;
  }

  _stateObj() {
    return this._hass?.states?.[this._config.entity];
  }

  async _snooze(entityId) {
    const raw = window.prompt(`Snooze ${entityId} for how many minutes?`, "60");
    if (raw === null) return;
    const minutes = parseInt(raw, 10);
    if (!Number.isFinite(minutes) || minutes < 1) return;
    await this._hass.callService("sensor_sentinel", "snooze", {
      entity_id: entityId,
      minutes,
    });
  }

  async _exclude(entityId) {
    if (!window.confirm(`Add a permanent exclusion rule for ${entityId}?`)) return;
    await this._hass.callService("sensor_sentinel", "exclude", {
      entity_id: entityId,
    });
  }

  async _why(entityId) {
    try {
      const res = await this._hass.callService(
        "sensor_sentinel",
        "explain",
        { entity_id: entityId },
        undefined,
        false,
        true
      );
      const r = res?.response || {};
      let msg;
      if (r.result === "down") msg = `Down since ${r.since} (state: ${r.state}).`;
      else if (r.result === "excluded")
        msg = `Excluded by ${r.rule_type} rule: ${r.value}`;
      else if (r.result === "pending_grace")
        msg = `Bad, waiting out the grace window (state: ${r.state}).`;
      else msg = JSON.stringify(r);
      window.alert(`${entityId}\n\n${msg}`);
    } catch (e) {
      window.alert(`Could not explain ${entityId}: ${e}`);
    }
  }

  _integrationName(id) {
    // Resolve a platform/domain id (e.g. "zwave_js") to its display name
    // (e.g. "Z-Wave JS") via the frontend's component translations, falling
    // back to a prettified id when no translation is loaded.
    if (!id || id === "unknown") return "Unknown";
    const loc = this._hass?.localize?.(`component.${id}.title`);
    if (loc) return loc;
    return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  _duration(sinceIso) {
    const secs = Math.max(0, (Date.now() - new Date(sinceIso).getTime()) / 1000);
    if (secs < 90) return `${Math.round(secs)}s`;
    if (secs < 5400) return `${Math.round(secs / 60)}m`;
    if (secs < 172800) return `${Math.round(secs / 3600)}h`;
    return `${Math.round(secs / 86400)}d`;
  }

  _render() {
    if (!this._hass) return;
    const st = this._stateObj();
    if (!st) {
      this.innerHTML = this._wrap(
        `<div class="ss-empty">Entity <code>${this._config.entity}</code> not found.</div>`
      );
      return;
    }

    const count = Number(st.state) || 0;
    const attrs = st.attributes || {};
    // Prefer the full websocket-fetched list; fall back to the capped sample.
    const usingFull = this._full && Array.isArray(this._incidents);
    const incidents = usingFull ? this._incidents : attrs.entities || [];

    // Group the sampled incidents by integration for a rolled-up view.
    const groups = {};
    for (const inc of incidents) {
      const key = inc.integration || "unknown";
      (groups[key] = groups[key] || []).push(inc);
    }

    // Order the groups: by number of down entities (most first), or by name.
    const sortMode = this._config.sort || "count";
    const orderedKeys = Object.keys(groups).sort((a, b) => {
      if (sortMode === "count") {
        const diff = groups[b].length - groups[a].length;
        if (diff) return diff;
      }
      return this._integrationName(a).localeCompare(this._integrationName(b));
    });

    let body;
    if (count === 0) {
      body = `<div class="ss-empty">✅ Everything's up — nothing down right now.</div>`;
    } else {
      body = orderedKeys
        .map((integration) => this._renderGroup(integration, groups[integration]))
        .join("");
      if (!usingFull && attrs.truncated) {
        body += `<div class="ss-note">Showing a sample of ${incidents.length}; the full list couldn't be fetched. Check the integration is up to date.</div>`;
      }
    }

    this.innerHTML = this._wrap(`
      <div class="ss-head">
        <div class="ss-count ${count ? "bad" : "ok"}">${count}</div>
        <div class="ss-headmeta">
          <div class="ss-title">Sensor Sentinel</div>
          <div class="ss-sub">${count ? "entities down and/or unavailable" : "all clear"}</div>
        </div>
      </div>
      ${this._sparklineSVG()}
      <div class="ss-list">${body}</div>
    `);
    this._bind();
  }

  _renderGroup(integration, rows) {
    // A group's collapsed state: an explicit user toggle wins; otherwise fall
    // back to the card's collapse_by_default option.
    const collapsed =
      integration in this._collapsed
        ? this._collapsed[integration]
        : !!this._config.collapse_by_default;
    const canPing = integration === "zwave_js" && this._config.zwave_ping;
    const items = collapsed
      ? ""
      : rows
          .map(
            (inc) => `
      <div class="ss-row">
        <div class="ss-row-main">
          <div class="ss-name">${inc.name || inc.entity_id}${
              inc.flapping ? ' <span class="ss-flap">flapping</span>' : ""
            }</div>
          <div class="ss-meta">${inc.area || "—"} · ${inc.state} · ${this._duration(
              inc.since
            )}</div>
        </div>
        <div class="ss-actions">
          ${
            canPing
              ? `<button data-act="ping" data-eid="${inc.entity_id}" title="Ping Z-Wave node">📡</button>`
              : ""
          }
          <button data-act="why" data-eid="${inc.entity_id}" title="Why?">?</button>
          <button data-act="snooze" data-eid="${inc.entity_id}" title="Snooze">💤</button>
          <button data-act="exclude" data-eid="${inc.entity_id}" title="Exclude">🚫</button>
        </div>
      </div>`
          )
          .join("");
    return `
      <div class="ss-group">
        <div class="ss-group-head" data-toggle="${integration}">
          <span class="ss-caret">${collapsed ? "▸" : "▾"}</span>
          <span>${this._integrationName(integration)}</span>
          <span class="ss-group-count">${rows.length}</span>
        </div>
        ${items}
      </div>`;
  }

  _bind() {
    this.querySelectorAll("[data-toggle]").forEach((el) =>
      el.addEventListener("click", () => {
        const k = el.getAttribute("data-toggle");
        // Toggle from the *effective* state so the first click behaves
        // correctly even when collapse_by_default has set the initial state.
        const current =
          k in this._collapsed
            ? this._collapsed[k]
            : !!this._config.collapse_by_default;
        this._collapsed[k] = !current;
        this._render();
      })
    );
    this.querySelectorAll("button[data-act]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const eid = btn.getAttribute("data-eid");
        const act = btn.getAttribute("data-act");
        if (act === "why") this._why(eid);
        else if (act === "snooze") this._snooze(eid);
        else if (act === "exclude") this._exclude(eid);
        else if (act === "ping") this._ping(eid);
      })
    );
  }

  async _ping(entityId) {
    try {
      // zwave_js.ping wakes/round-trips the node; if it recovers it leaves the
      // bad state and drops off the list on the next update.
      await this._hass.callService("zwave_js", "ping", {}, { entity_id: entityId });
      this._toast(`Pinged ${entityId}`);
    } catch (e) {
      this._toast(`Ping failed for ${entityId}: ${e}`);
    }
  }

  _toast(message) {
    // Fire HA's global toast rather than a blocking alert().
    this.dispatchEvent(
      new CustomEvent("hass-notification", {
        detail: { message },
        bubbles: true,
        composed: true,
      })
    );
  }

  _wrap(inner) {
    return `
      <ha-card>
        <style>
          ha-card { padding: 12px 16px 16px; }
          .ss-head { display:flex; align-items:center; gap:12px; }
          .ss-count { font-size:2.2rem; font-weight:700; min-width:2ch; text-align:center;
            border-radius:12px; padding:2px 10px; }
          .ss-count.bad { color:var(--error-color,#db4437); }
          .ss-count.ok { color:var(--success-color,#43a047); }
          .ss-title { font-weight:600; }
          .ss-sub { color:var(--secondary-text-color); font-size:.85rem; }
          .ss-spark-wrap { margin:10px 0 2px; }
          .ss-spark-cap { color:var(--secondary-text-color); font-size:.72rem; margin-bottom:2px; }
          .ss-spark { width:100%; height:40px; display:block; }
          .ss-spark-line { fill:none; stroke:var(--primary-color,#03a9f4); stroke-width:1.5;
            vector-effect:non-scaling-stroke; }
          .ss-spark-area { fill:var(--primary-color,#03a9f4); opacity:.12; stroke:none; }
          .ss-spark-dot { fill:var(--primary-color,#03a9f4); }
          .ss-spark-empty { color:var(--secondary-text-color); font-size:.75rem; font-style:italic; }
          .ss-group { margin-top:8px; }
          .ss-group-head { display:flex; align-items:center; gap:8px; cursor:pointer;
            font-weight:600; padding:4px 0; border-bottom:1px solid var(--divider-color); }
          .ss-caret { width:1ch; color:var(--secondary-text-color); }
          .ss-group-count { margin-left:auto; color:var(--secondary-text-color);
            font-weight:400; font-size:.8rem; }
          .ss-row { display:flex; align-items:center; gap:8px; padding:6px 0 6px 18px; }
          .ss-row-main { flex:1; min-width:0; }
          .ss-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
          .ss-meta { color:var(--secondary-text-color); font-size:.78rem; }
          .ss-flap { color:var(--warning-color,#ffa600); font-size:.7rem; border:1px solid;
            border-radius:6px; padding:0 4px; }
          .ss-actions button { background:none; border:none; cursor:pointer; font-size:1rem;
            padding:2px 4px; border-radius:6px; }
          .ss-actions button:hover { background:var(--secondary-background-color); }
          .ss-empty { padding:14px 2px; color:var(--secondary-text-color); }
          .ss-note, .ss-group + .ss-note { color:var(--secondary-text-color);
            font-size:.75rem; margin-top:8px; font-style:italic; }
        </style>
        ${inner}
      </ha-card>`;
  }
}

/**
 * Visual editor for the card — an ha-form driven by EDITOR_SCHEMA. Presence of
 * SensorSentinelCard.getConfigElement() makes HA offer the "Visual editor".
 */
class SensorSentinelCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...CONFIG_DEFAULTS, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => EDITOR_LABELS[schema.name] || schema.name;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        // Preserve type and any keys ha-form doesn't manage.
        const config = { ...this._config, ...ev.detail.value };
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config },
            bubbles: true,
            composed: true,
          })
        );
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = this._config;
  }
}

customElements.define("sensor-sentinel-card-editor", SensorSentinelCardEditor);
customElements.define("sensor-sentinel-card", SensorSentinelCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "sensor-sentinel-card",
  name: "Sensor Sentinel Card",
  description: "Live unavailable-entity incidents with one-click snooze/exclude/ping.",
  preview: true,
  documentationURL: "https://github.com/petergCA/sensor-sentinel",
});
console.info("%c SENSOR-SENTINEL-CARD %c v0.5.0 ", "background:#0288d1;color:#fff", "");
