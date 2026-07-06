/**
 * Sensor Sentinel — companion Lovelace card.
 *
 * Buildless vanilla custom element (no Lit/npm) so it ships inside the
 * integration and needs zero manual resource setup. Renders the live incident
 * list from sensor.sentinel_unavailable_count and drives the one-click actions
 * (snooze / exclude / why? / ping) through the sensor_sentinel.* services and
 * the sensor_sentinel/list websocket command.
 */

const DEFAULT_ENTITY = "sensor.sentinel_unavailable_count";

const CONFIG_DEFAULTS = {
  entity: DEFAULT_ENTITY,
  sort: "count",
  group_by: "integration",
  collapse_by_default: false,
  zwave_ping: true,
  show_reload: true,
  sparkline_hours: 0,
};

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor" } } },
  {
    name: "group_by",
    selector: {
      select: {
        mode: "dropdown",
        options: [
          { value: "integration", label: "Integration" },
          { value: "area", label: "Area" },
        ],
      },
    },
  },
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
  { name: "show_reload", selector: { boolean: {} } },
  {
    name: "sparkline_hours",
    selector: {
      number: { min: 0, max: 168, step: 1, mode: "box", unit_of_measurement: "h" },
    },
  },
];

const EDITOR_LABELS = {
  entity: "Count entity",
  group_by: "Group by",
  sort: "Sort groups",
  collapse_by_default: "Collapse groups by default",
  zwave_ping: "Show Z-Wave ping button",
  show_reload: "Show reload-integration button (per group)",
  sparkline_hours: "Trend sparkline window (hours, 0 = off)",
};

const SNOOZE_PRESETS = [
  ["15m", 15],
  ["1h", 60],
  ["8h", 480],
  ["1d", 1440],
];

class SensorSentinelCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("sensor-sentinel-card-editor");
  }

  static getStubConfig() {
    return { ...CONFIG_DEFAULTS };
  }

  setConfig(config) {
    this._config = { ...CONFIG_DEFAULTS, ...config };
    this._collapsed = this._loadCollapsed();
    this._filter = this._filter || "";
    this._modal = null;
    // Invalidate cached history so a changed window refetches promptly.
    this._historyFetchedAt = 0;
    this._history = null;
  }

  set hass(hass) {
    this._hass = hass;
    const st = this._stateObj();
    const stamp = st ? st.last_updated : null;
    if (stamp && stamp !== this._stamp) {
      this._stamp = stamp;
      this._fetchFull();
    }
    this._maybeFetchHistory();
    this._render();
  }

  getCardSize() {
    return 5;
  }

  _stateObj() {
    return this._hass?.states?.[this._config.entity];
  }

  // -- Data fetching -------------------------------------------------------

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
      this._full = false;
    } finally {
      this._fetching = false;
      this._render();
    }
  }

  async _maybeFetchHistory() {
    const hours = Number(this._config.sparkline_hours) || 0;
    if (!hours || !this._hass) {
      this._history = null;
      return;
    }
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

  // -- Actions -------------------------------------------------------------

  _snoozeMinutes(entityId, minutes) {
    this._modal = null;
    this._hass.callService("sensor_sentinel", "snooze", { entity_id: entityId, minutes });
    this._toast(`Snoozed ${this._nameFor(entityId)} for ${minutes} min`);
    this._render();
  }

  _doExclude(entityId) {
    this._modal = null;
    this._hass.callService("sensor_sentinel", "exclude", { entity_id: entityId });
    this._toast(`Excluded ${this._nameFor(entityId)} — undo in the integration's Configure dialog`);
    this._render();
  }

  async _disableEntity(entityId) {
    this._modal = null;
    try {
      await this._hass.callWS({
        type: "config/entity_registry/update",
        entity_id: entityId,
        disabled_by: "user",
      });
      this._toast(`Disabled ${this._nameFor(entityId)} — re-enable in Settings → Entities`);
    } catch (e) {
      this._toast(`Could not disable ${entityId} (admin required?): ${e}`);
    }
    this._render();
  }

  async _why(entityId) {
    const name = this._nameFor(entityId);
    const rows = [["Entity", entityId]];
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
      if (r.result === "down") {
        rows.push(["Status", r.stale ? "Down · stale (treated as retired)" : "Down"]);
        rows.push(["Down since", this._fmtDate(r.since)]);
        rows.push(["Current state", r.state]);
      } else if (r.result === "excluded") {
        rows.push(["Status", "Excluded from Sentinel"]);
        rows.push(["Matched rule", `${r.rule_type} = ${r.value}`]);
      } else if (r.result === "pending_grace") {
        rows.push(["Status", "Bad — waiting out the grace window"]);
        rows.push(["Current state", r.state]);
      } else if (r.result === "bad_awaiting_grace") {
        rows.push(["Status", "Bad — inside the grace window"]);
        rows.push(["Current state", r.state]);
      } else if (r.result === "ok") {
        rows.push(["Status", "OK"]);
        rows.push(["Current state", r.state]);
      } else if (r.result === "missing") {
        rows.push(["Status", "Not found in Home Assistant"]);
      } else {
        rows.push(["Detail", JSON.stringify(r)]);
      }
    } catch (e) {
      rows.push(["Error", String(e)]);
    }
    this._openModal({ kind: "why", eid: entityId, name, rows });
  }

  _nameFor(eid) {
    const inc = (this._incidents || []).find((i) => i.entity_id === eid);
    return inc?.name || this._hass?.states?.[eid]?.attributes?.friendly_name || eid;
  }

  _openModal(modal) {
    this._modal = modal;
    this._render();
  }

  _closeModal() {
    this._modal = null;
    this._render();
  }

  async _ping(entityId) {
    try {
      await this._hass.callService("zwave_js", "ping", {}, { entity_id: entityId });
      this._toast(`Pinged ${entityId}`);
    } catch (e) {
      this._toast(`Ping failed for ${entityId}: ${e}`);
    }
  }

  _idsForGroup(key) {
    const st = this._stateObj();
    const all =
      this._full && Array.isArray(this._incidents)
        ? this._incidents
        : st?.attributes?.entities || [];
    return all
      .filter((inc) => this._groupKey(inc) === key && this._matchesFilter(inc))
      .map((inc) => inc.entity_id);
  }

  _bulkSnooze(key, minutes) {
    this._modal = null;
    const ids = this._idsForGroup(key);
    for (const eid of ids) {
      this._hass.callService("sensor_sentinel", "snooze", { entity_id: eid, minutes });
    }
    this._toast(`Snoozed ${ids.length} entities for ${minutes} min`);
    this._render();
  }

  _bulkExclude(key) {
    this._modal = null;
    const ids = this._idsForGroup(key);
    for (const eid of ids) {
      this._hass.callService("sensor_sentinel", "exclude", { entity_id: eid });
    }
    this._toast(`Excluded ${ids.length} entities — undo in Configure`);
    this._render();
  }

  async _reloadIntegration(key) {
    this._modal = null;
    const ids = this._idsForGroup(key);
    try {
      // Target the group's entities; HA reloads the config entries they belong to.
      await this._hass.callService(
        "homeassistant",
        "reload_config_entry",
        {},
        { entity_id: ids }
      );
      this._toast(`Reloading ${this._groupLabel(key)}…`);
    } catch (e) {
      this._toast(`Reload failed (admin required?): ${e}`);
    }
    this._render();
  }

  _openEntity(entityId) {
    // Prefer the entity's device page; fall back to the more-info dialog for
    // entities that aren't attached to a device.
    const deviceId = this._hass?.entities?.[entityId]?.device_id;
    if (deviceId) {
      this._navigate(`/config/devices/device/${deviceId}`);
    } else {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          detail: { entityId },
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  _navigate(path) {
    history.pushState(null, "", path);
    this.dispatchEvent(
      new CustomEvent("location-changed", {
        detail: { replace: false },
        bubbles: true,
        composed: true,
      })
    );
  }

  _toast(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", {
        detail: { message },
        bubbles: true,
        composed: true,
      })
    );
  }

  // -- Formatting helpers --------------------------------------------------

  _fmtDate(iso) {
    if (!iso) return iso;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const locale = this._hass?.locale || {};
    const opts = { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
    if (locale.time_format === "12") opts.hour12 = true;
    else if (locale.time_format === "24") opts.hour12 = false;
    if (locale.time_zone === "server" && this._hass?.config?.time_zone) {
      opts.timeZone = this._hass.config.time_zone;
    }
    try {
      return new Intl.DateTimeFormat(locale.language || undefined, opts).format(d);
    } catch (e) {
      return d.toLocaleString();
    }
  }

  _integrationName(id) {
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

  // -- Collapse persistence ------------------------------------------------

  _collapseKey() {
    return `sensor-sentinel-collapsed:${this._config?.entity || DEFAULT_ENTITY}`;
  }

  _loadCollapsed() {
    try {
      return JSON.parse(window.localStorage.getItem(this._collapseKey()) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  _saveCollapsed() {
    try {
      window.localStorage.setItem(this._collapseKey(), JSON.stringify(this._collapsed));
    } catch (e) {
      /* ignore quota/private-mode errors */
    }
  }

  // -- Render --------------------------------------------------------------

  _groupKey(inc) {
    if (this._config.group_by === "area") return inc.area || "unassigned";
    return inc.integration || "unknown";
  }

  _groupLabel(key) {
    if (this._config.group_by === "area") return key;
    return this._integrationName(key);
  }

  _matchesFilter(inc) {
    const f = (this._filter || "").trim().toLowerCase();
    if (!f) return true;
    return (
      (inc.name || "").toLowerCase().includes(f) ||
      (inc.entity_id || "").toLowerCase().includes(f) ||
      (inc.area || "").toLowerCase().includes(f) ||
      (inc.integration || "").toLowerCase().includes(f)
    );
  }

  _sparklineSVG() {
    const hours = Number(this._config.sparkline_hours) || 0;
    if (!hours) return "";
    const pts = this._history;
    if (!pts || pts.length < 2) {
      return `<div class="ss-spark-wrap"><div class="ss-spark-cap">${hours}h trend</div><div class="ss-spark-empty">gathering history…</div></div>`;
    }
    const W = 400, H = 40, pad = 2;
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
    const usingFull = this._full && Array.isArray(this._incidents);
    const all = usingFull ? this._incidents : attrs.entities || [];
    const incidents = all.filter((inc) => this._matchesFilter(inc));

    // Group.
    const groups = {};
    for (const inc of incidents) {
      const key = this._groupKey(inc);
      (groups[key] = groups[key] || []).push(inc);
    }

    const sortMode = this._config.sort || "count";
    const orderedKeys = Object.keys(groups).sort((a, b) => {
      if (sortMode === "count") {
        const diff = groups[b].length - groups[a].length;
        if (diff) return diff;
      }
      return this._groupLabel(a).localeCompare(this._groupLabel(b));
    });

    let body;
    if (count === 0) {
      body = `<div class="ss-empty">✅<span class="ss-cleartext">Everything's up — nothing down right now.</span></div>`;
    } else if (incidents.length === 0) {
      body = `<div class="ss-empty">No incidents match “${this._filter}”.</div>`;
    } else {
      body = orderedKeys.map((k) => this._renderGroup(k, groups[k])).join("");
      if (!usingFull && attrs.truncated) {
        body += `<div class="ss-note">Showing a sample of ${incidents.length}; the full list couldn't be fetched. Check the integration is up to date.</div>`;
      }
    }

    const filterVal = (this._filter || "").replace(/"/g, "&quot;");
    const anyExpanded = orderedKeys.some((k) => !this._isCollapsed(k));
    const toggleAllBtn = orderedKeys.length
      ? `<button class="ss-expandall" data-toggleall>
           <ha-icon icon="${anyExpanded ? "mdi:unfold-less-horizontal" : "mdi:unfold-more-horizontal"}"></ha-icon>
           <span>${anyExpanded ? "Collapse all" : "Expand all"}</span>
         </button>`
      : "";
    this.innerHTML = this._wrap(`
      <div class="ss-head">
        <div class="ss-count ${count ? "bad" : "ok"}">${count}</div>
        <div class="ss-headmeta">
          <div class="ss-title">Sensor Sentinel</div>
          <div class="ss-sub">${count ? "entities down and/or unavailable" : "all clear"}</div>
        </div>
        ${toggleAllBtn}
      </div>
      ${this._sparklineSVG()}
      ${count ? `<input class="ss-search" type="search" placeholder="Filter by name, area, integration…" value="${filterVal}" />` : ""}
      <div class="ss-list">${body}</div>
    `) + this._renderModal();
    this._bind();
  }

  _isCollapsed(key) {
    return key in this._collapsed ? this._collapsed[key] : !!this._config.collapse_by_default;
  }

  _currentGroupKeys() {
    const st = this._stateObj();
    const all =
      this._full && Array.isArray(this._incidents)
        ? this._incidents
        : st?.attributes?.entities || [];
    const keys = new Set();
    for (const inc of all) {
      if (this._matchesFilter(inc)) keys.add(this._groupKey(inc));
    }
    return [...keys];
  }

  _toggleAll() {
    const keys = this._currentGroupKeys();
    // If anything is expanded, collapse everything; otherwise expand everything.
    const collapse = keys.some((k) => !this._isCollapsed(k));
    keys.forEach((k) => {
      this._collapsed[k] = collapse;
    });
    this._saveCollapsed();
    this._render();
  }

  _renderGroup(key, rows) {
    const collapsed = this._isCollapsed(key);
    const items = collapsed ? "" : rows.map((inc) => this._renderRow(inc)).join("");
    return `
      <div class="ss-group">
        <div class="ss-group-head">
          <span class="ss-caret" data-toggle="${encodeURIComponent(key)}">${collapsed ? "▸" : "▾"}</span>
          <span class="ss-group-name" data-toggle="${encodeURIComponent(key)}">${this._groupLabel(key)}</span>
          <span class="ss-group-count">${rows.length}</span>
          <span class="ss-group-actions">
            ${
              this._config.group_by === "integration" && this._config.show_reload
                ? `<button data-gact="reload" data-gkey="${encodeURIComponent(key)}" title="Reload this integration">🔄</button>`
                : ""
            }
            <button data-gact="snooze" data-gkey="${encodeURIComponent(key)}" title="Snooze all in group">💤</button>
            <button data-gact="exclude" data-gkey="${encodeURIComponent(key)}" title="Exclude all in group">⚠️</button>
          </span>
        </div>
        ${items}
      </div>`;
  }

  _renderRow(inc) {
    const eid = inc.entity_id;
    const canPing = inc.integration === "zwave_js" && this._config.zwave_ping;
    const badges =
      (inc.flapping ? ' <span class="ss-badge ss-flap">flapping</span>' : "") +
      (inc.stale ? ' <span class="ss-badge ss-stale">stale</span>' : "");

    const actions = `
      <button data-act="why" data-eid="${eid}" title="Why?">?</button>
      ${canPing ? `<button data-act="ping" data-eid="${eid}" title="Ping Z-Wave node">📡</button>` : ""}
      <button data-act="snooze" data-eid="${eid}" title="Snooze">💤</button>
      <button data-act="exclude" data-eid="${eid}" title="Exclude from Sentinel">⚠️</button>
      <button data-act="disable" data-eid="${eid}" title="Disable entity in Home Assistant">🚫</button>`;
    // Render the clickable area as a real <a> to the device page when the
    // entity has a device, so right-click / cmd-click / middle-click can open
    // it in a new tab. Plain left-clicks are intercepted for in-app nav.
    const deviceId = this._hass?.entities?.[eid]?.device_id;
    const href = deviceId ? `/config/devices/device/${deviceId}` : null;
    const mainOpen = href
      ? `<a class="ss-row-main" href="${href}" data-info="${eid}" title="Open device page for ${eid}">`
      : `<div class="ss-row-main" data-info="${eid}" title="Open ${eid}">`;
    const mainClose = href ? "</a>" : "</div>";
    return `
      <div class="ss-row">
        ${mainOpen}
          <div class="ss-name">${inc.name || eid}${badges}</div>
          <div class="ss-meta">${inc.area || "—"} · ${inc.state} · ${this._duration(inc.since)}</div>
        ${mainClose}
        <div class="ss-actions">${actions}</div>
      </div>`;
  }

  _bind() {
    const search = this.querySelector(".ss-search");
    if (search) {
      search.addEventListener("input", (e) => {
        this._filter = e.target.value;
        const pos = e.target.selectionStart;
        this._render();
        // Restore focus + caret after re-render.
        const again = this.querySelector(".ss-search");
        if (again) {
          again.focus();
          try {
            again.setSelectionRange(pos, pos);
          } catch (_) {}
        }
      });
    }

    this.querySelectorAll("[data-toggle]").forEach((el) =>
      el.addEventListener("click", () => {
        const k = decodeURIComponent(el.getAttribute("data-toggle"));
        this._collapsed[k] = !this._isCollapsed(k);
        this._saveCollapsed();
        this._render();
      })
    );

    const toggleAll = this.querySelector("[data-toggleall]");
    if (toggleAll) toggleAll.addEventListener("click", () => this._toggleAll());

    this.querySelectorAll(".ss-row-main[data-info]").forEach((el) =>
      el.addEventListener("click", (e) => {
        // Let modifier / non-left clicks fall through so the browser can open
        // the link in a new tab; intercept plain left-clicks for in-app nav.
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        this._openEntity(el.getAttribute("data-info"));
      })
    );

    this.querySelectorAll("button[data-act]").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const eid = btn.getAttribute("data-eid");
        const act = btn.getAttribute("data-act");
        if (act === "why") this._why(eid);
        else if (act === "snooze") this._openModal({ kind: "snooze", eid, name: this._nameFor(eid) });
        else if (act === "exclude") this._openModal({ kind: "exclude", eid, name: this._nameFor(eid) });
        else if (act === "ping") this._ping(eid);
        else if (act === "disable") this._openModal({ kind: "disable", eid, name: this._nameFor(eid) });
      })
    );

    this._bindModal();

    this.querySelectorAll("button[data-gact]").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const key = decodeURIComponent(btn.getAttribute("data-gkey"));
        const count = this._idsForGroup(key).length;
        if (!count) return;
        const act = btn.getAttribute("data-gact");
        const kind =
          act === "snooze" ? "gsnooze" : act === "exclude" ? "gexclude" : "reload";
        this._openModal({ kind, key, label: this._groupLabel(key), count });
      })
    );
  }

  _renderModal() {
    const m = this._modal;
    if (!m) return "";
    let title = "";
    let bodyHtml = "";
    let actionsHtml = `<button class="ss-mbtn" data-mact="close">Close</button>`;
    if (m.kind === "why") {
      title = m.name;
      bodyHtml = m.rows
        .map(
          ([k, v]) =>
            `<div class="ss-mrow"><span class="ss-mk">${k}</span><span class="ss-mv">${v}</span></div>`
        )
        .join("");
    } else if (m.kind === "snooze") {
      title = `Snooze ${m.name}`;
      bodyHtml =
        `<div class="ss-msub">Mute this entity so it drops off the list for a while.</div>` +
        `<div class="ss-mgrid">` +
        SNOOZE_PRESETS.map(
          ([lbl, mins]) => `<button class="ss-mbtn ss-mgridbtn" data-mact="snooze" data-min="${mins}">${lbl}</button>`
        ).join("") +
        `</div>`;
      actionsHtml = `<button class="ss-mbtn" data-mact="close">Cancel</button>`;
    } else if (m.kind === "exclude") {
      title = `Exclude ${m.name}?`;
      bodyHtml = `<div class="ss-msub">Adds a Sentinel rule so this entity is never reported as down. It stays fully active in Home Assistant — undo anytime in the integration's <b>Configure</b> dialog. (To remove it from Home Assistant entirely, use <b>Disable</b> instead.)</div>`;
      actionsHtml =
        `<button class="ss-mbtn" data-mact="close">Cancel</button>` +
        `<button class="ss-mbtn ss-mprimary" data-mact="exclude">Exclude</button>`;
    } else if (m.kind === "disable") {
      title = `Disable ${m.name}?`;
      bodyHtml = `<div class="ss-msub">This disables the entity in Home Assistant — it is removed until you re-enable it in <b>Settings → Devices &amp; Services → Entities</b> (needs a reload). This is different from <b>Exclude</b>, which only hides it from Sensor Sentinel.</div>`;
      actionsHtml =
        `<button class="ss-mbtn" data-mact="close">Cancel</button>` +
        `<button class="ss-mbtn ss-mdanger" data-mact="disable">Disable</button>`;
    } else if (m.kind === "gexclude") {
      title = `Exclude all in ${m.label}?`;
      bodyHtml = `<div class="ss-msub">Adds Sentinel exclusion rules for all <b>${m.count}</b> currently-listed ${m.count === 1 ? "entity" : "entities"} in this group. They stay fully active in Home Assistant — undo in the integration's <b>Configure</b> dialog.</div>`;
      actionsHtml =
        `<button class="ss-mbtn" data-mact="close">Cancel</button>` +
        `<button class="ss-mbtn ss-mprimary" data-mact="gexclude">Exclude ${m.count}</button>`;
    } else if (m.kind === "reload") {
      title = `Reload ${m.label}?`;
      bodyHtml = `<div class="ss-msub">Reloads this integration's config entry. Its entities briefly go unavailable and come back — often the quickest fix when a whole integration has gone dark. Needs an admin user.</div>`;
      actionsHtml =
        `<button class="ss-mbtn" data-mact="close">Cancel</button>` +
        `<button class="ss-mbtn ss-mprimary" data-mact="reload">Reload</button>`;
    } else if (m.kind === "gsnooze") {
      title = `Snooze all in ${m.label}`;
      bodyHtml =
        `<div class="ss-msub">Mute all <b>${m.count}</b> currently-listed ${m.count === 1 ? "entity" : "entities"} in this group for…</div>` +
        `<div class="ss-mgrid">` +
        SNOOZE_PRESETS.map(
          ([lbl, mins]) => `<button class="ss-mbtn ss-mgridbtn" data-mact="gsnooze" data-min="${mins}">${lbl}</button>`
        ).join("") +
        `</div>`;
      actionsHtml = `<button class="ss-mbtn" data-mact="close">Cancel</button>`;
    }
    return `
      <div class="ss-modal-overlay">
        <div class="ss-modal" role="dialog" aria-modal="true">
          <div class="ss-modal-title">${title}</div>
          <div class="ss-modal-body">${bodyHtml}</div>
          <div class="ss-modal-actions">${actionsHtml}</div>
        </div>
      </div>`;
  }

  _bindModal() {
    const overlay = this.querySelector(".ss-modal-overlay");
    if (!overlay) return;
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) this._closeModal(); // backdrop click closes
    });
    this.querySelectorAll(".ss-modal [data-mact]").forEach((el) =>
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const act = el.getAttribute("data-mact");
        if (act === "close") this._closeModal();
        else if (act === "snooze")
          this._snoozeMinutes(this._modal.eid, Number(el.getAttribute("data-min")));
        else if (act === "exclude") this._doExclude(this._modal.eid);
        else if (act === "disable") this._disableEntity(this._modal.eid);
        else if (act === "gexclude") this._bulkExclude(this._modal.key);
        else if (act === "reload") this._reloadIntegration(this._modal.key);
        else if (act === "gsnooze")
          this._bulkSnooze(this._modal.key, Number(el.getAttribute("data-min")));
      })
    );
  }

  _wrap(inner) {
    return `
      <ha-card>
        <style>
          ha-card { padding: 12px 16px 16px; }
          .ss-head { display:flex; align-items:center; gap:6px; }
          .ss-count { font-size:2.2rem; font-weight:700; min-width:2ch; text-align:center;
            border-radius:12px; padding:2px 10px; }
          .ss-count.bad { color:var(--error-color,#db4437); }
          .ss-count.ok { color:var(--success-color,#43a047); }
          .ss-title { font-weight:600; }
          .ss-sub { color:var(--secondary-text-color); font-size:.85rem; }
          .ss-expandall { margin-left:auto; align-self:center; display:inline-flex;
            align-items:center; gap:4px; background:none; border:1px solid var(--divider-color);
            cursor:pointer; padding:5px 10px; border-radius:8px; font-size:.85rem;
            color:var(--secondary-text-color); white-space:nowrap; }
          .ss-expandall:hover { background:var(--secondary-background-color); }
          .ss-expandall ha-icon { --mdc-icon-size:18px; }
          .ss-spark-wrap { margin:10px 0 2px; }
          .ss-spark-cap { color:var(--secondary-text-color); font-size:.72rem; margin-bottom:2px; }
          .ss-spark { width:100%; height:40px; display:block; }
          .ss-spark-line { fill:none; stroke:var(--primary-color,#03a9f4); stroke-width:1.5;
            vector-effect:non-scaling-stroke; }
          .ss-spark-area { fill:var(--primary-color,#03a9f4); opacity:.12; stroke:none; }
          .ss-spark-dot { fill:var(--primary-color,#03a9f4); }
          .ss-spark-empty { color:var(--secondary-text-color); font-size:.75rem; font-style:italic; }
          .ss-search { width:100%; box-sizing:border-box; margin:10px 0 4px; padding:6px 10px;
            border:1px solid var(--divider-color); border-radius:8px;
            background:var(--card-background-color); color:var(--primary-text-color); font-size:.9rem; }
          .ss-group { margin-top:8px; }
          .ss-group-head { display:flex; align-items:center; gap:8px;
            font-weight:600; padding:4px 0; border-bottom:1px solid var(--divider-color); }
          .ss-caret, .ss-group-name { cursor:pointer; }
          .ss-caret { width:1ch; color:var(--secondary-text-color); }
          .ss-group-count { margin-left:auto; color:var(--secondary-text-color);
            font-weight:400; font-size:.8rem; }
          .ss-group-actions { display:flex; gap:2px; }
          .ss-group-actions button, .ss-actions button { background:none; border:none;
            cursor:pointer; font-size:1rem; padding:3px 5px; border-radius:6px; }
          .ss-group-actions button:hover, .ss-actions button:hover { background:var(--secondary-background-color); }
          .ss-row { display:flex; align-items:center; gap:8px; padding:6px 0 6px 18px; }
          .ss-row-main { flex:1; min-width:0; cursor:pointer; text-decoration:none; color:inherit; }
          .ss-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
          .ss-meta { color:var(--secondary-text-color); font-size:.78rem; }
          .ss-badge { font-size:.7rem; border:1px solid; border-radius:6px; padding:0 4px; }
          .ss-flap { color:var(--warning-color,#ffa600); }
          .ss-stale { color:var(--secondary-text-color); }
          .ss-actions { display:flex; align-items:center; gap:8px; flex-shrink:0; }
          .ss-mdi { --mdc-icon-size:18px; width:18px; height:18px; vertical-align:middle;
            color:var(--secondary-text-color); }
          .ss-empty { padding:14px 2px 14px 20px; color:var(--secondary-text-color); }
          .ss-cleartext { margin-left:24px; }
          .ss-note { color:var(--secondary-text-color); font-size:.75rem; margin-top:8px; font-style:italic; }
          .ss-modal-overlay { position:fixed; inset:0; z-index:10; background:rgba(0,0,0,.55);
            display:flex; align-items:center; justify-content:center; padding:16px; }
          .ss-modal { background:var(--ha-card-background,var(--card-background-color,#fff));
            color:var(--primary-text-color); border-radius:16px; padding:20px 22px; width:100%;
            max-width:440px; box-sizing:border-box; box-shadow:0 10px 48px rgba(0,0,0,.45); }
          .ss-modal-title { font-size:1.25rem; font-weight:700; margin-bottom:12px; word-break:break-word; }
          .ss-modal-body { font-size:1rem; line-height:1.55; }
          .ss-mrow { display:flex; gap:12px; padding:7px 0; border-bottom:1px solid var(--divider-color); }
          .ss-mrow:last-child { border-bottom:none; }
          .ss-mk { color:var(--secondary-text-color); min-width:96px; flex-shrink:0; }
          .ss-mv { font-weight:500; word-break:break-word; }
          .ss-msub { color:var(--secondary-text-color); margin-bottom:14px; }
          .ss-mgrid { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }
          .ss-modal-actions { display:flex; justify-content:flex-end; gap:10px; margin-top:20px; }
          .ss-mbtn { font-size:1rem; padding:10px 18px; border-radius:10px; cursor:pointer;
            border:1px solid var(--divider-color); background:var(--secondary-background-color);
            color:var(--primary-text-color); }
          .ss-mbtn:hover { background:var(--divider-color); }
          .ss-mgridbtn { padding:14px; font-size:1.05rem; font-weight:600; }
          .ss-mdanger { border-color:var(--error-color,#db4437); color:var(--error-color,#db4437);
            background:transparent; }
          .ss-mprimary { border-color:var(--primary-color,#03a9f4); color:var(--primary-color,#03a9f4);
            background:transparent; }
        </style>
        ${inner}
      </ha-card>`;
  }
}

/**
 * Visual editor for the card — an ha-form driven by EDITOR_SCHEMA.
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
        const config = { ...this._config, ...ev.detail.value };
        this.dispatchEvent(
          new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true })
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
  description: "Live unavailable-entity incidents with search, grouping, and one-click actions.",
  preview: true,
  documentationURL: "https://github.com/petergCA/sensor-sentinel",
});
console.info("%c SENSOR-SENTINEL-CARD %c v0.6.15 ", "background:#0288d1;color:#fff", "");
