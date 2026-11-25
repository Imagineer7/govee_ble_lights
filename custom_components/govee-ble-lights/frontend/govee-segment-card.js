/*
 * Govee Segment Card: paint individual segments of a Govee BLE RGBIC strip
 * and manage presets. Served and auto-loaded by the govee-ble-lights integration.
 *
 *   type: custom:govee-segment-card
 *   entity: light.govee_light_2   # the main strip light
 *   name: Bedroom strip           # optional
 */
const CARD_VERSION = "1.0.0";
const DOMAIN = "govee_ble_lights";

const SWATCHES = [
  "#ff0000", "#ff6a00", "#ffd000", "#00ff00", "#00ffb4", "#00c8ff",
  "#0040ff", "#8000ff", "#ff00c8", "#ffa046", "#ffffff", "#000000",
];

const hexToRgb = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const rgbToHex = (rgb) => "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join("");
const css = (rgb) => (rgb ? `rgb(${rgb[0]},${rgb[1]},${rgb[2]})` : "transparent");

class GoveeSegmentCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._selected = new Set();
    this._color = "#ff0000";
    this._local = null; // optimistic colors until HA catches up
    this._localUntil = 0;
    this._dragging = false;
    this._dragAdd = true;
    this._confirmDelete = null;
    this._presetsKey = "";
    this._built = false;
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass.states).find(
      (e) => e.startsWith("light.") && Array.isArray(hass.states[e].attributes.segment_colors)
    );
    return { entity: id || "light.govee_light" };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("Set 'entity' to the main Govee BLE strip light (e.g. light.govee_light_2)");
    }
    this._config = config;
    if (this._built) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    this._render();
  }

  getCardSize() {
    return 6;
  }

  // ---- state helpers ---------------------------------------------------

  get _light() {
    return this._hass && this._hass.states[this._config.entity];
  }

  get _presetEntity() {
    const id = this._light && this._light.attributes.preset_entity;
    return id ? this._hass.states[id] : undefined;
  }

  _count() {
    return (this._light && this._light.attributes.segment_count) || 15;
  }

  _colors() {
    const n = this._count();
    const real = (this._light && this._light.attributes.segment_colors) || Array(n).fill(null);
    if (this._local && Date.now() < this._localUntil) {
      if (JSON.stringify(real) === JSON.stringify(this._local)) this._local = null;
      else return this._local;
    }
    this._local = null;
    return real;
  }

  _optimistic(updates) {
    const next = this._colors().slice();
    for (const [i, rgb] of Object.entries(updates)) next[i] = rgb;
    this._local = next;
    this._localUntil = Date.now() + 5000;
    this._renderStrip();
  }

  _call(service, data, domain = DOMAIN) {
    this._status("");
    return this._hass
      .callService(domain, service, data, { entity_id: this._config.entity })
      .catch((err) => {
        this._local = null;
        this._status(err.message || String(err), true);
        this._renderStrip();
      });
  }

  _callNoTarget(service, data) {
    this._status("");
    return this._hass.callService(DOMAIN, service, data).catch((err) => {
      this._status(err.message || String(err), true);
      throw err;
    });
  }

  _status(text, error = false) {
    const el = this._$(".status");
    el.textContent = text;
    el.classList.toggle("error", error);
    clearTimeout(this._statusTimer);
    if (text && !error) this._statusTimer = setTimeout(() => (el.textContent = ""), 3000);
  }

  _$(sel) {
    return this.shadowRoot.querySelector(sel);
  }

  // ---- actions ---------------------------------------------------------

  _paintSelected() {
    const segs = [...this._selected].sort((a, b) => a - b);
    if (!segs.length) {
      this._status("Tap or drag across segments to select them first.", true);
      return;
    }
    const rgb = hexToRgb(this._color);
    const updates = {};
    segs.forEach((i) => (updates[i] = rgb));
    this._optimistic(updates);
    this._call("set_segment_color", { segments: segs.map((i) => i + 1), rgb_color: rgb });
  }

  _fill() {
    const rgb = hexToRgb(this._color);
    const n = this._count();
    const updates = {};
    for (let i = 0; i < n; i++) updates[i] = rgb;
    this._optimistic(updates);
    this._call("set_segments", { colors: Array(n).fill(rgb) });
  }

  _applyPreset(name) {
    const presets = (this._presetEntity && this._presetEntity.attributes.presets) || {};
    if (presets[name]) {
      const n = this._count();
      const src = presets[name];
      const updates = {};
      for (let i = 0; i < n; i++) updates[i] = src[Math.min(Math.floor((i * src.length) / n), src.length - 1)];
      this._optimistic(updates);
    }
    this._$(".pname").value = name;
    this._call("apply_preset", { preset: name });
  }

  _savePreset() {
    const name = this._$(".pname").value.trim();
    if (!name) {
      this._status("Type a name for the preset first.", true);
      return;
    }
    const colors = this._colors();
    const unknown = colors.some((c) => !c);
    const existed = !!((this._presetEntity && this._presetEntity.attributes.presets) || {})[name];
    this._callNoTarget("save_preset", { name, colors: colors.map((c) => c || [0, 0, 0]) })
      .then(() =>
        this._status(
          `${existed ? "Updated" : "Saved"} "${name}"` + (unknown ? " (unknown segments saved as off)" : "")
        )
      )
      .catch(() => {});
  }

  _deletePreset(name) {
    if (this._confirmDelete !== name) {
      this._confirmDelete = name;
      this._presetsKey = "";
      this._renderPresets();
      clearTimeout(this._confirmTimer);
      this._confirmTimer = setTimeout(() => {
        this._confirmDelete = null;
        this._presetsKey = "";
        this._renderPresets();
      }, 3000);
      return;
    }
    this._confirmDelete = null;
    this._callNoTarget("delete_preset", { name })
      .then(() => this._status(`Deleted "${name}"`))
      .catch(() => {});
  }

  // ---- selection -------------------------------------------------------

  _segIndexAt(x, y) {
    const el = this.shadowRoot.elementFromPoint(x, y);
    if (!el || el.dataset.idx === undefined) return null;
    return Number(el.dataset.idx);
  }

  _touchSeg(i) {
    if (i === null) return;
    if (this._dragAdd) this._selected.add(i);
    else this._selected.delete(i);
    this._renderStrip();
  }

  // ---- DOM -------------------------------------------------------------

  _build() {
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; }
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .title { font-size: 1.15em; font-weight: 500; }
        .sub { font-size: .85em; color: var(--secondary-text-color); }
        button {
          font: inherit; cursor: pointer; border-radius: 8px; padding: 6px 12px;
          border: 1px solid var(--divider-color); background: var(--secondary-background-color);
          color: var(--primary-text-color);
        }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
        .power { padding: 6px 10px; line-height: 0; --mdc-icon-size: 22px; }
        .power.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
        .strip { display: flex; gap: 4px; touch-action: none; user-select: none; margin-bottom: 6px; }
        .seg {
          flex: 1; height: 44px; border-radius: 6px; border: 2px solid var(--divider-color);
          box-sizing: border-box; position: relative;
        }
        .seg.unknown {
          background: repeating-linear-gradient(45deg, var(--divider-color) 0 4px, transparent 4px 8px) !important;
        }
        .seg.sel { border-color: var(--primary-text-color); box-shadow: 0 0 0 2px var(--primary-color); }
        .strip.off .seg { opacity: .35; }
        .nums { display: flex; gap: 4px; font-size: .7em; color: var(--secondary-text-color); margin-bottom: 12px; }
        .nums span { flex: 1; text-align: center; }
        .row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
        .grow { flex: 1; }
        input[type=color] { width: 44px; height: 36px; border: none; background: none; padding: 0; cursor: pointer; }
        input[type=range] { flex: 1; }
        input[type=text] {
          flex: 1; min-width: 120px; font: inherit; padding: 6px 10px; border-radius: 8px;
          border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color);
        }
        .swatches { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
        .sw { width: 28px; height: 28px; border-radius: 50%; border: 2px solid var(--divider-color); cursor: pointer; padding: 0; }
        .sw.cur { box-shadow: 0 0 0 2px var(--primary-color); }
        .section { font-weight: 500; margin: 6px 0 8px; }
        .presets { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
        .chip {
          display: flex; align-items: center; gap: 6px; padding: 4px 6px 4px 4px; border-radius: 18px;
          border: 1px solid var(--divider-color); background: var(--secondary-background-color); cursor: pointer;
        }
        .chip.cur { border-color: var(--primary-color); box-shadow: 0 0 0 1px var(--primary-color); }
        .chip .prev { width: 46px; height: 22px; border-radius: 12px; }
        .chip .del { border: none; background: none; padding: 0 4px; color: var(--secondary-text-color); }
        .chip .del.confirm { color: var(--error-color, #db4437); font-weight: 500; }
        .status { min-height: 1.2em; font-size: .85em; color: var(--secondary-text-color); }
        .status.error { color: var(--error-color, #db4437); }
        .empty { color: var(--secondary-text-color); font-size: .9em; }
      </style>
      <ha-card>
        <div class="header">
          <div><div class="title"></div><div class="sub selinfo"></div></div>
          <button class="power" title="Power"><ha-icon icon="mdi:power"></ha-icon></button>
        </div>
        <div class="strip"></div>
        <div class="nums"></div>
        <div class="row">
          <input type="color" class="picker" title="Pick a color">
          <button class="primary paint">Paint selected</button>
          <button class="fill">Fill strip</button>
          <span class="grow"></span>
          <button class="all">All</button>
          <button class="none">None</button>
        </div>
        <div class="swatches"></div>
        <div class="row"><span>Brightness</span><input type="range" class="bri" min="1" max="100"></div>
        <div class="section">Presets</div>
        <div class="presets"></div>
        <div class="row">
          <input type="text" class="pname" placeholder="Preset name" maxlength="40">
          <button class="save">Save current</button>
        </div>
        <div class="status"></div>
      </ha-card>`;

    const strip = this._$(".strip");
    strip.addEventListener("pointerdown", (e) => {
      const i = this._segIndexAt(e.clientX, e.clientY);
      if (i === null) return;
      e.preventDefault();
      this._dragging = true;
      this._dragAdd = !this._selected.has(i);
      strip.setPointerCapture(e.pointerId);
      this._touchSeg(i);
    });
    strip.addEventListener("pointermove", (e) => {
      if (this._dragging) this._touchSeg(this._segIndexAt(e.clientX, e.clientY));
    });
    const stop = () => (this._dragging = false);
    strip.addEventListener("pointerup", stop);
    strip.addEventListener("pointercancel", stop);

    const picker = this._$(".picker");
    picker.value = this._color;
    picker.addEventListener("input", () => {
      this._color = picker.value;
      this._renderSwatches();
    });
    picker.addEventListener("change", () => {
      this._color = picker.value;
      this._renderSwatches();
      if (this._selected.size) this._paintSelected();
    });

    this._$(".paint").addEventListener("click", () => this._paintSelected());
    this._$(".fill").addEventListener("click", () => this._fill());
    this._$(".all").addEventListener("click", () => {
      for (let i = 0; i < this._count(); i++) this._selected.add(i);
      this._renderStrip();
    });
    this._$(".none").addEventListener("click", () => {
      this._selected.clear();
      this._renderStrip();
    });
    this._$(".power").addEventListener("click", () => this._call("toggle", {}, "light"));
    this._$(".bri").addEventListener("change", (e) =>
      this._call("turn_on", { brightness_pct: Number(e.target.value) }, "light")
    );
    this._$(".save").addEventListener("click", () => this._savePreset());
    this._$(".pname").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this._savePreset();
    });

    const sw = this._$(".swatches");
    SWATCHES.forEach((hex) => {
      const b = document.createElement("button");
      b.className = "sw";
      b.style.background = hex;
      b.title = hex === "#000000" ? "Off (black)" : hex;
      b.dataset.hex = hex;
      b.addEventListener("click", () => {
        this._color = hex;
        picker.value = hex;
        this._renderSwatches();
        if (this._selected.size) this._paintSelected();
      });
      sw.appendChild(b);
    });
  }

  _render() {
    if (!this._hass || !this._config) return;
    const st = this._light;
    this._$(".title").textContent =
      this._config.name || (st && st.attributes.friendly_name) || this._config.entity;
    if (!st) {
      this._status(`Entity ${this._config.entity} not found`, true);
      return;
    }
    if (!Array.isArray(st.attributes.segment_colors) && st.state !== "unavailable") {
      this._status("This light has no segment data. Use the main strip light of a segmented model.", true);
    }
    const on = st.state === "on";
    this._$(".power").classList.toggle("on", on);
    const bri = this._$(".bri");
    if (this.shadowRoot.activeElement !== bri && st.attributes.brightness) {
      bri.value = Math.max(1, Math.round((st.attributes.brightness / 255) * 100));
    }
    this._renderStrip();
    this._renderSwatches();
    this._renderPresets();
  }

  _renderStrip() {
    const strip = this._$(".strip");
    const nums = this._$(".nums");
    const n = this._count();
    if (strip.children.length !== n) {
      strip.innerHTML = "";
      nums.innerHTML = "";
      for (let i = 0; i < n; i++) {
        const d = document.createElement("div");
        d.className = "seg";
        d.dataset.idx = i;
        strip.appendChild(d);
        const s = document.createElement("span");
        s.textContent = i + 1;
        nums.appendChild(s);
      }
    }
    const colors = this._colors();
    [...strip.children].forEach((d, i) => {
      const c = colors[i];
      d.style.background = css(c);
      d.classList.toggle("unknown", !c);
      d.classList.toggle("sel", this._selected.has(i));
      d.title = `Segment ${i + 1}` + (c ? ` ${rgbToHex(c)}` : " (unknown)");
    });
    strip.classList.toggle("off", !!this._light && this._light.state === "off");
    const k = this._selected.size;
    this._$(".selinfo").textContent = k ? `${k} segment${k > 1 ? "s" : ""} selected` : "Tap or drag to select segments";
  }

  _renderSwatches() {
    this.shadowRoot.querySelectorAll(".sw").forEach((b) => b.classList.toggle("cur", b.dataset.hex === this._color));
  }

  _renderPresets() {
    const pe = this._presetEntity;
    const presets = (pe && pe.attributes.presets) || {};
    const current = pe ? pe.state : null;
    const key = JSON.stringify([presets, current, this._confirmDelete]);
    if (key === this._presetsKey) return;
    this._presetsKey = key;

    const box = this._$(".presets");
    box.innerHTML = "";
    const names = Object.keys(presets);
    if (!names.length) {
      const e = document.createElement("div");
      e.className = "empty";
      e.textContent = pe ? "No presets yet. Paint the strip, name it below, and save." : "Preset entity not found.";
      box.appendChild(e);
      return;
    }
    names.forEach((name) => {
      const colors = presets[name];
      const chip = document.createElement("div");
      chip.className = "chip" + (name === current ? " cur" : "");
      chip.title = `Apply "${name}"`;
      const prev = document.createElement("span");
      prev.className = "prev";
      prev.style.background =
        colors.length > 1 ? `linear-gradient(90deg, ${colors.map(css).join(", ")})` : css(colors[0]);
      const label = document.createElement("span");
      label.textContent = name;
      const del = document.createElement("button");
      const confirming = this._confirmDelete === name;
      del.className = "del" + (confirming ? " confirm" : "");
      del.textContent = confirming ? "Delete?" : "\u2715";
      del.title = "Delete preset";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        this._deletePreset(name);
      });
      chip.addEventListener("click", () => this._applyPreset(name));
      chip.append(prev, label, del);
      box.appendChild(chip);
    });
  }
}

if (!customElements.get("govee-segment-card")) {
  customElements.define("govee-segment-card", GoveeSegmentCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "govee-segment-card",
    name: "Govee Segment Card",
    description: "Paint individual segments and manage presets for Govee BLE strips",
  });
  console.info(`%c GOVEE-SEGMENT-CARD %c ${CARD_VERSION} `, "background:#1e88e5;color:#fff", "");
}
