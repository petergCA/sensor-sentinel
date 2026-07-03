/**
 * Sensor Sentinel — companion Lovelace card.
 *
 * Buildless vanilla custom element (no Lit/npm) so it ships inside the
 * integration and needs zero manual resource setup. Renders the live incident
 * list from sensor.sentinel_unavailable_count and drives the one-click actions
 * (snooze / exclude / why?) through the sensor_sentinel.* services.
 */

const DEFAULT_ENTITY = "sensor.sentinel_unavailable_count";

class SensorSentinelCard extends HTMLElement {
  static getStubConfig() {
    return { entity: DEFAULT_ENTITY };
  }

  setConfig(config) {
    this._config = { entity: DEFAULT_ENTITY, ...config };
    this._collapsed = this._collapsed || {};
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
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
    const incidents = attrs.entities || [];
    const byIntegration = attrs.by_integration || {};

    // Group the sampled incidents by integration for a rolled-up view.
    const groups = {};
    for (const inc of incidents) {
      const key = inc.integration || "unknown";
      (groups[key] = groups[key] || []).push(inc);
    }

    const chips = Object.entries(byIntegration)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([k, v]) => `<span class="ss-chip">${k}: ${v}</span>`)
      .join("");

    let body;
    if (count === 0) {
      body = `<div class="ss-empty">✅ Everything's up — nothing down right now.</div>`;
    } else {
      body = Object.keys(groups)
        .sort()
        .map((integration) => this._renderGroup(integration, groups[integration]))
        .join("");
      if (attrs.truncated) {
        body += `<div class="ss-note">Showing a sample; more incidents not listed (attribute payload is capped for performance).</div>`;
      }
    }

    this.innerHTML = this._wrap(`
      <div class="ss-head">
        <div class="ss-count ${count ? "bad" : "ok"}">${count}</div>
        <div class="ss-headmeta">
          <div class="ss-title">Sensor Sentinel</div>
          <div class="ss-sub">${count ? "entities down" : "all clear"}</div>
        </div>
      </div>
      <div class="ss-chips">${chips}</div>
      <div class="ss-list">${body}</div>
    `);
    this._bind();
  }

  _renderGroup(integration, rows) {
    const collapsed = this._collapsed[integration];
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
          <span>${integration}</span>
          <span class="ss-group-count">${rows.length}</span>
        </div>
        ${items}
      </div>`;
  }

  _bind() {
    this.querySelectorAll("[data-toggle]").forEach((el) =>
      el.addEventListener("click", () => {
        const k = el.getAttribute("data-toggle");
        this._collapsed[k] = !this._collapsed[k];
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
          .ss-chips { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 4px; }
          .ss-chip { background:var(--secondary-background-color); border-radius:12px;
            padding:2px 8px; font-size:.75rem; }
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

customElements.define("sensor-sentinel-card", SensorSentinelCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "sensor-sentinel-card",
  name: "Sensor Sentinel Card",
  description: "Live unavailable-entity incidents with one-click snooze/exclude.",
});
console.info("%c SENSOR-SENTINEL-CARD %c v0.1.0 ", "background:#0288d1;color:#fff", "");
