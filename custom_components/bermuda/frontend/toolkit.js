/** Administrator controls for Bermuda's opt-in measurement toolkit. */
const MAX_IMPORT_BYTES = 2 * 1024 * 1024;
const TABS = ["Settings", "Calibration", "History & replay", "Fingerprints", "Health"];

function node(tag, text, attributes = {}) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  return element;
}

function rows(value) {
  if (Array.isArray(value)) return value;
  return Object.entries(value || {}).map(([id, item]) => ({ id, ...(item || {}) }));
}

function pretty(value) {
  return JSON.stringify(value === undefined ? null : value, null, 2);
}

export function finiteInput(input, { min = -Infinity, max = Infinity, optional = false } = {}) {
  if (input.value.trim() === "") {
    if (optional) return undefined;
    throw new Error("Enter a measured number.");
  }
  const number = Number(input.value);
  if (!Number.isFinite(number) || number < min || number > max) throw new Error(`Enter a number between ${min} and ${max}.`);
  return number;
}

export async function readJSONFile(file) {
  if (!file || file.size > MAX_IMPORT_BYTES) throw new Error("Choose a JSON file no larger than 2 MiB.");
  const text = await file.text();
  if (new TextEncoder().encode(text).length > MAX_IMPORT_BYTES) throw new Error("JSON file exceeds 2 MiB.");
  const data = JSON.parse(text);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("The JSON file must contain an object.");
  return data;
}

export class BermudaToolkitPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._activeTab = 0;
    this._state = null;
    this._busy = false;
    this._connected = false;
    this._generation = 0;
    this._drafts = new Map();
    this._results = {};
    this._captureNeedsPreview = false;
    this._message = "";
  }

  set hass(value) {
    const first = !this._hass && value;
    const previousUser = this._hass?.user;
    const userChanged = previousUser?.id && previousUser.id !== value?.user?.id;
    const denied = value?.user?.is_admin === false;
    const restored = previousUser?.is_admin === false && value?.user?.is_admin === true;
    this._hass = value;
    if (userChanged || (denied && previousUser?.is_admin !== false)) {
      this._generation += 1;
      this._state = null;
      this._content = null;
      this._drafts.clear();
      this._results = {};
      this._captureNeedsPreview = false;
      this._message = denied ? "Administrator access is required." : "";
      if (this._connected) this._render();
    }
    if ((first || userChanged || restored) && !denied && this._connected) void this.refresh();
  }

  get hass() { return this._hass; }

  set panel(value) {
    const previous = this._entryId;
    this._panel = value;
    this._entryId = value?.config?.entry_id;
    if (previous !== this._entryId) {
      this._generation += 1;
      this._state = null;
      this._content = null;
      this._drafts.clear();
      this._results = {};
      this._message = "";
      this._captureNeedsPreview = false;
      if (this._connected && this._hass) void this.refresh();
    }
  }

  get panel() { return this._panel; }

  connectedCallback() {
    this._connected = true;
    this._render();
    if (this._hass) void this.refresh();
  }

  disconnectedCallback() {
    this._connected = false;
    this._generation += 1;
  }

  async refresh() {
    if (!this._hass || !this._connected || this._hass.user?.is_admin === false) return;
    if (this._refreshPromise) {
      this._refreshAgain = true;
      return this._refreshPromise;
    }
    this._refreshPromise = (async () => {
      do {
        this._refreshAgain = false;
        const generation = this._generation;
        const path = this._entryId ? `bermuda/toolkit?entry_id=${encodeURIComponent(this._entryId)}` : "bermuda/toolkit";
        try {
          const state = await this._hass.callApi("GET", path);
          if (generation === this._generation && this._connected) {
            const previousModel = this._state?.diagnostics?.model_version;
            if (previousModel && previousModel !== state?.diagnostics?.model_version) this._invalidateResults("settings");
            this._state = state;
            this._render();
          }
        } catch (error) {
          if (generation === this._generation && this._connected) {
            this._message = this._errorText(error);
            this._render();
          }
        }
      } while (this._refreshAgain && this._connected);
    })();
    try { await this._refreshPromise; } finally { this._refreshPromise = null; }
  }

  _errorText(error) {
    return error?.body?.message || error?.message || "The toolkit request failed. Refresh and try again.";
  }

  async _perform(action, payload = {}, { download = false } = {}) {
    if (this._busy || !this._hass || this._hass.user?.is_admin === false) return;
    this._busy = true;
    const generation = this._generation;
    const entryId = this._entryId;
    this._message = "Working…";
    this._render();
    try {
      const request = { ...payload, action, entry_id: entryId };
      if (new TextEncoder().encode(JSON.stringify(request)).length > MAX_IMPORT_BYTES) throw new Error("The JSON request including metadata exceeds 2 MiB. Use a smaller recording or settings document.");
      const result = await this._hass.callApi("POST", "bermuda/toolkit", request);
      if (generation !== this._generation || !this._connected) return;
      this._invalidateResults(action);
      this._results[action] = result;
      if (action === "calibration_preview") this._captureNeedsPreview = false;
      const pending = result?.eligible === false || result?.accepted === false || result?.status === "pending" || result?.state === "absent";
      this._message = pending ? `${action.replaceAll("_", " ")} needs more evidence; see results and rejection reasons.` : `${action.replaceAll("_", " ")} completed.`;
      if (download) this._download(result, "bermuda-history.json");
      await this.refresh();
      return result;
    } catch (error) {
      if (generation === this._generation && this._connected) this._message = this._errorText(error);
    } finally {
      this._busy = false;
      if (this._connected) this._render();
    }
  }

  _invalidateResults(action) {
    if (action === "capture") this._captureNeedsPreview = true;
    if (action === "calibration_apply") delete this._results.calibration_rollback;
    if (action === "calibration_rollback") delete this._results.calibration_apply;
    if (["settings", "profile", "calibration_apply", "calibration_rollback", "capture"].includes(action)) delete this._results.calibration_preview;
    if (["settings", "profile", "calibration_apply", "calibration_rollback", "capture", "history_start", "history_stop", "history_clear", "history_import"].includes(action)) {
      delete this._results.replay;
      delete this._results.shadow_stop;
      delete this._results.shadow_promote;
    }
    if (action === "shadow_start") { delete this._results.shadow_stop; delete this._results.shadow_promote; }
    if (action === "fingerprint_clear") delete this._results.fingerprint_train;
  }

  async _importHistory(file) {
    if (this._busy || this._fileBusy) return;
    this._fileBusy = true;
    const generation = this._generation;
    this._render();
    try {
      const data = await readJSONFile(file);
      if (generation === this._generation && this._connected) await this._perform("history_import", { data });
    } catch (error) {
      if (generation === this._generation && this._connected) this._message = this._errorText(error);
    } finally {
      this._fileBusy = false;
      if (this._connected) this._render();
    }
  }

  _download(data, filename) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data)], { type: "application/json" }));
    const link = node("a", "", { href: url, download: filename });
    this.shadowRoot.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async _copy(data, { compact = false } = {}) {
    try {
      await navigator.clipboard.writeText(compact ? JSON.stringify(data) : pretty(data));
      this._message = "Copied JSON to clipboard.";
    } catch {
      this._message = "Clipboard is unavailable. Select and copy the displayed JSON.";
    }
    this._render();
  }

  _rememberDrafts() {
    if (!this._content) return;
    for (const input of this._content.querySelectorAll("[data-draft]")) {
      this._drafts.set(input.dataset.draft, input.type === "checkbox" ? input.checked : input.value);
    }
  }

  _field(parent, label, name, value = "", options = {}) {
    const wrapper = node("label", undefined, { class: "field" });
    wrapper.append(node("span", label));
    const input = node(options.multiline ? "textarea" : "input");
    input.type = options.type || "text";
    input.dataset.draft = name;
    if (input.type === "checkbox") input.checked = this._drafts.has(name) ? this._drafts.get(name) : Boolean(value);
    else input.value = this._drafts.has(name) ? this._drafts.get(name) : String(value ?? "");
    if (options.min !== undefined) input.min = String(options.min);
    if (options.max !== undefined) input.max = String(options.max);
    if (options.step !== undefined) input.step = String(options.step);
    if (options.required) input.required = true;
    if (options.placeholder) input.placeholder = options.placeholder;
    wrapper.append(input);
    parent.append(wrapper);
    return input;
  }

  _select(parent, label, name, choices, value) {
    const wrapper = node("label", undefined, { class: "field" });
    wrapper.append(node("span", label));
    const select = node("select");
    select.dataset.draft = name;
    for (const [id, text] of choices) select.append(node("option", text, { value: id }));
    const saved = this._drafts.has(name) ? this._drafts.get(name) : value;
    if (choices.some(([id]) => id === saved)) select.value = saved;
    wrapper.append(select);
    parent.append(wrapper);
    return select;
  }

  _button(parent, label, callback, { disabled = false, allowBusy = false } = {}) {
    const button = node("button", label, { type: "button" });
    button.disabled = (!allowBusy && (this._busy || this._fileBusy)) || disabled;
    button.addEventListener("click", async () => {
      if ((!allowBusy && (this._busy || this._fileBusy)) || button.disabled) return;
      try { await callback(); } catch (error) { this._message = this._errorText(error); this._render(); }
    });
    parent.append(button);
    return button;
  }

  _section(parent, title, explanation) {
    const section = node("section");
    section.append(node("h2", title));
    if (explanation) section.append(node("p", explanation));
    parent.append(section);
    return section;
  }

  _json(parent, value) {
    if (value === undefined || value === null) {
      parent.append(node("p", "Unavailable. Refresh state or collect independent labelled samples to evaluate this feature."));
      return;
    }
    parent.append(node("pre", pretty(value)));
  }

  _options() { return this._state?.options || this._state?.settings || this._state?.measurement_toolkit || {}; }
  _snapshot() { return this._state?.snapshot || this._state?.advert_snapshot || this._state || {}; }
  _devices() { return rows(this._snapshot().devices).map(d => [d.device_id || d.id || d.address, `${d.name || d.id || d.address} (${d.device_id || d.id || d.address})`]); }
  _scanners() { return rows(this._snapshot().scanners).map(s => [s.scanner_id || s.id || s.address, `${s.name || s.id || s.address} (${s.scanner_id || s.id || s.address})`]); }

  _render() {
    this._rememberDrafts();
    this.shadowRoot.replaceChildren();
    const style = node("style", `
      :host{display:block;padding:20px;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family, sans-serif)}
      main{max-width:1100px;margin:auto}header,.buttons,nav{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
      header{justify-content:space-between}h1{font-size:1.6rem}h2{font-size:1.15rem}section{padding:16px;margin:16px 0;border:1px solid var(--divider-color,#aaa);border-radius:8px}
      button,input,select,textarea{font:inherit;color:inherit;background:var(--card-background-color,white);border:1px solid var(--divider-color,#888);border-radius:4px;padding:9px;box-sizing:border-box}
      button{cursor:pointer}button:disabled{opacity:.55;cursor:default}button[aria-selected=true]{border:2px solid var(--primary-color,#03a9f4)}
      .field{display:flex;flex-direction:column;gap:5px;margin:12px 0;max-width:650px}.field:has(input[type=checkbox]){flex-direction:row;align-items:center}
      textarea{min-height:110px;width:100%}pre{overflow:auto;max-height:450px;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--secondary-background-color,#eee);padding:12px}
      table{border-collapse:collapse;width:100%}th,td{padding:8px;text-align:left;border-bottom:1px solid var(--divider-color,#aaa);overflow-wrap:anywhere}.table-wrap{overflow:auto}
      [role=status]{min-height:1.5em;margin:12px 0}p{line-height:1.5}input[type=checkbox]{width:20px;height:20px}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid var(--primary-color,#03a9f4)}
      @media(max-width:600px){:host{padding:10px}section{padding:12px}nav button{flex:1 1 40%}}
    `);
    const main = node("main");
    const header = node("header");
    header.append(node("h1", "Bermuda measurement toolkit"));
    this._button(header, "Refresh state", () => this.refresh());
    main.append(header);
    main.append(node("p", "Measured ranges are slant distances in metres between the beacon and each receiver. Label truth independently of Bermuda’s predicted area. Settings are opt-in."));
    const navigation = node("nav", undefined, { role: "tablist", "aria-label": "Toolkit controls" });
    TABS.forEach((title, index) => {
      const tab = this._button(navigation, title, () => { this._activeTab = index; this._render(); }, { allowBusy: true });
      tab.id = `tab-${index}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(this._activeTab === index));
      tab.setAttribute("aria-controls", "toolkit-content");
      tab.tabIndex = this._activeTab === index ? 0 : -1;
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        this._activeTab = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length;
        this._render();
        this.shadowRoot.getElementById(`tab-${this._activeTab}`).focus();
      });
    });
    main.append(navigation, node("div", this._message, { role: "status", "aria-live": "polite" }));
    this._content = node("div", undefined, { id: "toolkit-content", role: "tabpanel", "aria-labelledby": `tab-${this._activeTab}`, "aria-busy": String(this._busy) });
    main.append(this._content);
    this.shadowRoot.append(style, main);
    if (!this._state) { this._content.append(node("p", "State unavailable. Refresh after Bermuda is loaded; administrator access is required.")); return; }
    [this._settings, this._calibration, this._history, this._fingerprints, this._health][this._activeTab].call(this, this._content);
  }

  _settings(parent) {
    const options = this._options();
    const general = this._section(parent, "Optional runtime settings", "Automatic calibration uses independent reference labels and safety checks. Fingerprint area selection requires a confident, supervised result in a valid Home Assistant area.");
    const auto = this._field(general, "Enable automatic calibration", "auto", options.auto_calibration, { type: "checkbox" });
    const area = this._field(general, "Enable fingerprint area selection", "fingerprint-area", options.fingerprint_area_selection, { type: "checkbox" });
    const reliability = this._field(general, "Enable reliability weighting", "reliability", options.reliability_weighting, { type: "checkbox" });
    const window = this._field(general, "History window (seconds)", "window", options.history_window_s ?? 3600, { type: "number", min: 60, max: 86400 });
    this._button(general, "Save runtime settings", () => this._perform("settings", { settings: { auto_calibration: auto.checked, fingerprint_area_selection: area.checked, reliability_weighting: reliability.checked, history_window_s: finiteInput(window, { min: 60, max: 86400 }) } }));
    const receiver = this._section(parent, "Receiver maximum distance", "A value of 0 follows Bermuda’s existing global maximum distance.");
    const scanner = this._select(receiver, "Receiver", "limit-scanner", this._scanners());
    const limit = this._field(receiver, "Maximum distance (m)", "limit", options.receiver_limits?.[scanner.value] ?? 0, { type: "number", min: 0, max: 1000, step: .1 });
    scanner.addEventListener("change", () => { limit.value = options.receiver_limits?.[scanner.value] ?? 0; });
    this._button(receiver, "Save receiver limit", () => this._perform("settings", { settings: { receiver_limits: { ...options.receiver_limits, [scanner.value]: finiteInput(limit, { min: 0, max: 1000 }) } } }), { disabled: !this._scanners().length });
    const filters = this._section(parent, "Device filter mode", "Legacy preserves normal Bermuda behavior. Pet adapts to movement; stationary smooths stable references. Raw RSSI stays available in diagnostics.");
    const device = this._select(filters, "Device", "filter-device", this._devices());
    const mode = this._select(filters, "Filter", "filter-mode", ["legacy", "pet", "stationary"].map(x => [x, x]), options.filters?.[device.value] || "legacy");
    device.addEventListener("change", () => { mode.value = options.filters?.[device.value] || "legacy"; });
    this._button(filters, "Save filter mode", () => this._perform("settings", { settings: { filters: { ...options.filters, [device.value]: mode.value } } }), { disabled: !this._devices().length });
    const profiles = this._section(parent, "Hardware and radio profile", "Hardware and antenna descriptions are metadata. Only explicit numeric model values change distance. Leave a numeric field empty to inherit the existing model.");
    const kind = this._select(profiles, "Profile type", "profile-kind", [["receiver", "Receiver"], ["beacon", "Beacon"]], "receiver");
    const target = this._select(profiles, "Profile target", "profile-id", kind.value === "beacon" ? this._devices() : this._scanners());
    const profileInputs = {};
    const current = options[`${kind.value}_profiles`]?.[target.value] || {};
    const numericLabels = { height_m: "Receiver / beacon mounting height (m)", antenna_gain_dbi: "Antenna gain (dBi; metadata)", distance_accuracy_m: "Distance accuracy (m; metadata)", rssi_offset: "Receiver RSSI offset (dB)", reference_power: "Reference power at 1 m (dBm)", attenuation: "Path-loss exponent" };
    for (const key of ["hardware_type", "manufacturer", "model", "antenna", "environment", "notes", "height_m", "antenna_gain_dbi", "distance_accuracy_m", "rssi_offset", "reference_power", "attenuation"]) {
      const numeric = ["height_m", "antenna_gain_dbi", "distance_accuracy_m", "rssi_offset", "reference_power", "attenuation"].includes(key);
      profileInputs[key] = this._field(profiles, numericLabels[key] || key.replaceAll("_", " "), `profile-${key}`, current[key] ?? "", { type: numeric ? "number" : "text", step: numeric ? .1 : undefined });
    }
    const verified = this._field(profiles, "Reference power independently verified", "profile-verified", current.reference_power_verified, { type: "checkbox" });
    const configureKind = () => { profileInputs.rssi_offset.disabled = kind.value !== "receiver"; verified.disabled = kind.value !== "beacon"; };
    configureKind();
    const loadProfile = () => {
      const existing = options[`${kind.value}_profiles`]?.[target.value] || {};
      for (const [key, input] of Object.entries(profileInputs)) input.value = existing[key] ?? "";
      verified.checked = Boolean(existing.reference_power_verified);
      configureKind();
    };
    target.addEventListener("change", loadProfile);
    kind.addEventListener("change", () => {
      target.replaceChildren();
      for (const [id, name] of kind.value === "beacon" ? this._devices() : this._scanners()) target.append(node("option", name, { value: id }));
      loadProfile();
    });
    this._button(profiles, "Save profile", () => {
      if (!target.value) throw new Error("Choose a profile target.");
      const profile = {};
      const remove = [];
      for (const [key, input] of Object.entries(profileInputs)) {
        if (!input.disabled && input.value.trim()) profile[key] = input.type === "number" ? finiteInput(input) : input.value;
        else if (!input.disabled) remove.push(key);
      }
      if (!verified.disabled) profile.reference_power_verified = verified.checked;
      return this._perform("profile", { kind: kind.value, id: target.value, profile, remove });
    });
    const fixed = this._section(parent, "Fixed reference labels", "Only permanently fixed beacons belong here. Saving explicitly labels future samples. Use measured slant ranges and an independent location label. Use {} to remove fixed references.");
    const referenceJSON = this._field(fixed, "Fixed reference map (JSON)", "fixed-references", pretty(options.fixed_references || {}), { multiline: true });
    this._button(fixed, "Save fixed references", () => this._perform("settings", { settings: { fixed_references: JSON.parse(referenceJSON.value) } }));
    const binding = this._section(parent, "Explicit scanner bindings", 'Bind an opaque Bluetooth source to its Home Assistant device registry ID using {"source-id":{"device_registry_id":"registry-id"}}. Friendly names are not identity.');
    const bindingJSON = this._field(binding, "Source → binding object map (JSON)", "scanner-bindings", pretty(options.scanner_bindings || {}), { multiline: true });
    this._button(binding, "Save scanner bindings", () => this._perform("settings", { settings: { scanner_bindings: JSON.parse(bindingJSON.value) } }));
  }

  _calibration(parent) {
    const capture = this._section(parent, "1. Capture independent measured truth", "Place the same device at several separately labelled locations. Measure receiver-to-beacon slant distance, including height. Leave receivers without a measured range blank. Repeated clicks at one position do not create independent coverage.");
    const device = this._select(capture, "Device", "capture-device", this._devices());
    const location = this._field(capture, "Independent location label", "capture-location", "", { required: true, placeholder: "Reference spot A" });
    const accuracy = this._field(capture, "Measurement tolerance (m)", "capture-accuracy", .1, { type: "number", min: 0, max: 100, step: .01 });
    const ranges = this._scanners().map(([id, name]) => [id, this._field(capture, `Measured slant range to ${name} (m)`, `range-${id}`, "", { type: "number", min: .05, max: 1000, step: .01 })]);
    this._button(capture, "Capture labelled readings", () => {
      if (!location.value.trim()) throw new Error("Enter an independent location label.");
      const measured = {};
      for (const [id, input] of ranges) {
        const value = finiteInput(input, { min: .05, max: 1000, optional: true });
        if (value !== undefined) measured[id] = value;
      }
      if (!Object.keys(measured).length) throw new Error("Enter at least one measured slant range.");
      return this._perform("capture", { device_id: device.value, location_id: location.value.trim(), ranges: measured, accuracy_m: finiteInput(accuracy, { min: 0, max: 100 }) });
    }, { disabled: !this._devices().length || !ranges.length });
    if (this._results.capture) this._json(capture, this._results.capture);
    const calibration = this._section(parent, "2. Preview, apply, or rollback", "Preview is a trial. Inspect coverage, holdout errors, confidence and rejection reasons before applying. Range error does not prove position accuracy.");
    this._button(calibration, "Preview calibration", () => this._perform("calibration_preview"));
    const preview = this._results.calibration_preview;
    const proposal = preview?.proposal || preview || this._state.calibration?.proposal;
    if (proposal) this._json(calibration, proposal);
    const buttons = node("div", undefined, { class: "buttons" });
    this._button(buttons, "Apply accepted proposal", () => this._perform("calibration_apply", proposal?.proposal_id ? { proposal_id: proposal.proposal_id } : {}), { disabled: this._captureNeedsPreview || (proposal?.eligible !== true && proposal?.accepted !== true) });
    this._button(buttons, "Rollback calibration", () => this._perform("calibration_rollback"));
    calibration.append(buttons);
    if (this._results.calibration_apply || this._results.calibration_rollback) this._json(calibration, this._results.calibration_rollback || this._results.calibration_apply);
  }

  _history(parent) {
    const history = this._section(parent, "Bounded diagnostic recording", "Recording is off by default. Exports contain frozen observations, model versions and explicit label provenance. Recording downloads and copies use compact JSON to preserve the 2 MiB import budget. Imported recordings are validated on the server.");
    this._json(history, this._state.history);
    const devices = this._select(history, "Record device (or all tracked devices)", "history-device", [["", "All tracked devices"], ...this._devices()], "");
    const buttons = node("div", undefined, { class: "buttons" });
    this._button(buttons, "Start recording", () => this._perform("history_start", { devices: devices.value ? [devices.value] : [] }));
    this._button(buttons, "Stop recording", () => this._perform("history_stop"));
    this._button(buttons, "Clear history", () => this._perform("history_clear"));
    const redact = this._field(history, "Redact recording identifiers for sharing", "history-redact", true, { type: "checkbox" });
    this._button(buttons, "Export JSON", () => this._perform("history_export", { redact: redact.checked }, { download: true }));
    this._button(buttons, "Copy recording", async () => { const data = await this._perform("history_export", { redact: redact.checked }); if (data) await this._copy(data, { compact: true }); });
    history.append(buttons);
    const file = this._field(history, "Import recording JSON (maximum 2 MiB)", "history-file", "", { type: "file" });
    file.accept = ".json,application/json";
    delete file.dataset.draft;
    this._button(history, "Import recording", () => this._importHistory(file.files?.[0]));
    const replay = this._section(parent, "Replay and shadow comparison", "Compare frozen raw/current/candidate ranges and held-out labelled errors. Metrics without independent truth are unavailable. Shadow observes a candidate without applying it; promotion requires a passing safety check.");
    const candidate = this._field(replay, "Candidate receiver offsets (JSON; empty object uses the current proposal)", "replay-candidate", "{}", { multiline: true });
    const payload = () => { const parsed = JSON.parse(candidate.value); return Object.keys(parsed).length ? { candidate: parsed } : {}; };
    const controls = node("div", undefined, { class: "buttons" });
    this._button(controls, "Run replay", () => this._perform("replay", payload()));
    this._button(controls, "Start shadow", () => this._perform("shadow_start", payload()));
    this._button(controls, "Stop shadow", () => this._perform("shadow_stop"));
    this._button(controls, "Promote shadow candidate", () => this._perform("shadow_promote"));
    replay.append(controls);
    this._json(replay, this._results.replay || this._state.replay || { message: "Run replay to evaluate a recording." });
    this._json(replay, this._results.shadow_stop || this._state.shadow || { message: "No shadow comparison." });
    if (this._results.shadow_promote) this._json(replay, this._results.shadow_promote);
  }

  _fingerprints(parent) {
    const section = this._section(parent, "Supervised RSSI fingerprints", "Train the selected device at an independently known Home Assistant area. Capture multiple positions and times. Suggestions report unknown, ambiguous and missing coverage; they do not automatically label later samples.");
    const device = this._select(section, "Device", "fingerprint-device", this._devices());
    const areas = rows(this._state.areas).map(area => [area.area_id || area.id, area.name || area.area_id || area.id]);
    const location = this._select(section, "Independently known Home Assistant area", "fingerprint-location", areas);
    const controls = node("div", undefined, { class: "buttons" });
    this._button(controls, "Train labelled sample", () => {
      if (!location.value) throw new Error("Choose an independently known Home Assistant area.");
      return this._perform("fingerprint_train", { device_id: device.value, location_id: location.value.trim() });
    }, { disabled: !this._devices().length || !areas.length });
    this._button(controls, "Clear device fingerprints", () => this._perform("fingerprint_clear", { device_id: device.value }), { disabled: !this._devices().length });
    section.append(controls);
    this._json(section, this._results.fingerprint_train || this._state.fingerprints || this._state.fingerprint);
    const results = this._section(parent, "Current fingerprint matches", "Confidence, coverage, ambiguity, suggested area and rejection reasons for supervised matches. A suggestion requires fresh observations; unknown results are not assigned to an area.");
    this._json(results, this._state.fingerprint_results);
  }

  _health(parent) {
    const health = this._section(parent, "Scanner health and freshness", "Observation age belongs to the source packet. Refreshing this panel does not make stale data fresh. Raw and filtered distance are separate measurements.");
    const table = node("table");
    const head = node("tr");
    for (const title of ["Receiver", "Last packet age (s)", "Area", "Binding"]) head.append(node("th", title, { scope: "col" }));
    const thead = node("thead"); thead.append(head); table.append(thead);
    const body = node("tbody");
    for (const scanner of rows(this._snapshot().scanners)) {
      const tr = node("tr");
      for (const value of [scanner.name || scanner.scanner_id || scanner.id, scanner.scanner_last_seen_age ?? scanner.age ?? "unavailable", scanner.area_name || scanner.area_id || "unassigned", scanner.binding ? pretty(scanner.binding) : "unavailable"]) tr.append(node("td", value));
      body.append(tr);
    }
    table.append(body);
    const wrapper = node("div", undefined, { class: "table-wrap" }); wrapper.append(table); health.append(wrapper);
    if (!body.children.length) health.append(node("p", "Scanner health is unavailable."));
    const diagnostic = this._section(parent, "Diagnostics and rejection reasons", "Use full diagnostics when reporting coverage, freshness, model applicability, calibration or fingerprint rejection. Exports omit private keys.");
    this._button(diagnostic, "Load full diagnostics", () => this._perform("diagnostics"));
    const data = this._results.diagnostics || this._state.diagnostics || this._state;
    this._button(diagnostic, "Copy full diagnostics", async () => { const full = await this._perform("diagnostics"); if (full) await this._copy(full); });
    this._json(diagnostic, data);
    const observations = this._section(parent, "Current device observations", "RSSI, source stamps, age, status, quality, raw range and filtered range for the current snapshot.");
    this._json(observations, this._snapshot().devices);
  }
}

if (!customElements.get("bermuda-toolkit-panel")) customElements.define("bermuda-toolkit-panel", BermudaToolkitPanel);
