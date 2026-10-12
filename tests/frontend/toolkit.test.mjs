/** Behavioral tests for the production web component, using a small native DOM double. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

class Element {
  constructor(tag = "element") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.type = "text";
    this.checked = false;
  }
  attachShadow() { this.shadowRoot = new Element("shadow"); return this.shadowRoot; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return String(this._text || "") + this.children.map(child => child.textContent).join(""); }
  set value(value) { this._value = String(value); }
  get value() {
    if (this.tagName === "select") return this.children.some(child => child.value === this._value) ? this._value : this.children[0]?.value || "";
    return this._value ?? this.attributes.value ?? "";
  }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === "id") this.id = value; }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  async click() { for (const listener of this.listeners.click || []) await listener({ target: this }); }
  async dispatch(name, event = {}) { for (const listener of this.listeners[name] || []) await listener(event); }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  querySelectorAll(selector) { return this.all().filter(element => selector === "[data-draft]" ? element.dataset.draft !== undefined : element.tagName === selector); }
  getElementById(id) { return this.all().find(element => element.id === id); }
  focus() { this.focused = true; }
}

globalThis.HTMLElement = Element;
globalThis.document = { createElement: tag => new Element(tag) };
globalThis.customElements = { definitions: new Map(), get(name) { return this.definitions.get(name); }, define(name, value) { this.definitions.set(name, value); } };
const source = await readFile(new URL("../../custom_components/bermuda/frontend/toolkit.js", import.meta.url), "utf8");
const { BermudaToolkitPanel, finiteInput, readJSONFile } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const state = () => ({
  options: { auto_calibration: false, receiver_limits: { receiver1: 0 }, filters: {} },
  snapshot: {
    devices: { device1: { device_id: "device1", name: "<script>unsafe name</script>" } },
    scanners: { receiver1: { scanner_id: "receiver1", name: "Receiver one", scanner_last_seen_age: 12 } },
  },
  areas: [{ area_id: "room1", name: "Known room" }],
});
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
function panel(callApi = async () => state()) {
  const result = new BermudaToolkitPanel();
  result._connected = true;
  result._hass = { callApi };
  result._state = state();
  return result;
}
const button = (component, label) => component.shadowRoot.all().find(element => element.tagName === "button" && element.textContent === label);
const field = (component, label) => component.shadowRoot.all().find(element => element.tagName === "label" && element.children[0]?.textContent === label)?.children[1];

test("numbers preserve blank optional ranges and reject nonfinite/out-of-bound input", () => {
  assert.equal(finiteInput({ value: "" }, { optional: true }), undefined);
  assert.equal(finiteInput({ value: ".05" }, { min: .05, max: 1000 }), .05);
  for (const value of ["", "Infinity", "NaN", "-1", "0"]) assert.throws(() => finiteInput({ value }, { min: .05, max: 1000 }));
});

test("JSON import rejects size before reading, invalid envelopes, and actual oversized text", async () => {
  let reads = 0;
  await assert.rejects(readJSONFile({ size: 2 * 1024 * 1024 + 1, text() { reads += 1; } }), /2 MiB/);
  assert.equal(reads, 0);
  await assert.rejects(readJSONFile({ size: 2, text: async () => "[]" }), /object/);
  await assert.rejects(readJSONFile({ size: 2, text: async () => "{" }), SyntaxError);
  await assert.rejects(readJSONFile({ size: 2, text: async () => "x".repeat(2 * 1024 * 1024 + 1) }), /2 MiB/);
  assert.deepEqual(await readJSONFile({ size: 2, text: async () => '{"version":1}' }), { version: 1 });
});

test("refresh calls coalesce and callers await the bounded trailing refresh", async () => {
  const calls = [];
  const requests = [deferred(), deferred()];
  const component = panel(async (method, path) => { calls.push([method, path]); return requests[calls.length - 1].promise; });
  component._render = () => {};
  const first = component.refresh();
  const followers = Array.from({ length: 20 }, () => component.refresh());
  assert.equal(calls.length, 1);
  requests[0].resolve({ sequence: 1 });
  await tick();
  assert.equal(calls.length, 2);
  let completed = false;
  void followers[0].then(() => { completed = true; });
  await tick();
  assert.equal(completed, false);
  requests[1].resolve({ sequence: 2 });
  await Promise.all([first, ...followers]);
  assert.deepEqual(component._state, { sequence: 2 });
  assert.equal(calls.length, 2);
});

test("a prior entry response cannot replace the new target and its drafts", async () => {
  const first = deferred();
  const calls = [];
  const component = panel(async (method, path) => { calls.push(path); return calls.length === 1 ? first.promise : { entry_id: "entry-b" }; });
  component._entryId = "entry-a";
  component._captureNeedsPreview = true;
  component._render();
  field(component, "History window (seconds)").value = "777";
  const loading = component.refresh();
  component.panel = { config: { entry_id: "entry-b" } };
  first.resolve({ entry_id: "entry-a" });
  await loading;
  assert.equal(component._state.entry_id, "entry-b");
  assert.equal(component._drafts.has("window"), false);
  assert.equal(component._captureNeedsPreview, false);
  assert.deepEqual(calls, ["bermuda/toolkit?entry_id=entry-a", "bermuda/toolkit?entry_id=entry-b"]);
});

test("action admission is reserved before awaiting so repeated clicks send once", async () => {
  const pending = deferred();
  const calls = [];
  const component = panel(async (method, path, payload) => { calls.push([method, path, payload]); return method === "POST" ? pending.promise : state(); });
  component._entryId = "synthetic-entry";
  component._render = () => {};
  const first = component._perform("history_start");
  await component._perform("history_start");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], ["POST", "bermuda/toolkit", { action: "history_start", entry_id: "synthetic-entry" }]);
  pending.resolve({ running: true });
  await first;
  assert.equal(component._busy, false);
  assert.equal(calls.filter(([method]) => method === "POST").length, 1);
});

test("settings render native text, labels, controls and keyboard-accessible tabs", async () => {
  const component = panel();
  component._render();
  assert.equal(component.shadowRoot.querySelectorAll("script").length, 0);
  assert.match(component.shadowRoot.textContent, /<script>unsafe name<\/script>/);
  assert.equal(field(component, "Enable automatic calibration").checked, false);
  assert.equal(field(component, "Enable reliability weighting").checked, false);
  const tabs = component.shadowRoot.all().filter(element => element.getAttribute("role") === "tab");
  assert.equal(tabs.length, 5);
  assert.equal(tabs[0].getAttribute("aria-selected"), "true");
  await tabs[0].dispatch("keydown", { key: "End", preventDefault() {} });
  assert.equal(component._activeTab, 4);
  assert.equal(component.shadowRoot.getElementById("tab-4").focused, true);
  assert.match(component.shadowRoot.textContent, /Last packet age/);
});

test("capture validates independent truth and sends measured ranges through the real button", async () => {
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return state(); });
  component._activeTab = 1;
  component._entryId = "entry1";
  component._render();
  await button(component, "Capture labelled readings").click();
  assert.equal(calls.length, 0);
  assert.match(component._message, /location label/);
  field(component, "Independent location label").value = "Measured spot A";
  field(component, "Measured slant range to Receiver one (receiver1) (m)").value = "-2";
  await button(component, "Capture labelled readings").click();
  assert.equal(calls.length, 0);
  field(component, "Measured slant range to Receiver one (receiver1) (m)").value = "2.25";
  await button(component, "Capture labelled readings").click();
  assert.deepEqual(calls[0], {
    action: "capture", entry_id: "entry1", device_id: "device1", location_id: "Measured spot A", ranges: { receiver1: 2.25 }, accuracy_m: .1,
  });
});

test("fingerprint training uses an existing area selector and disables unavailable truth", async () => {
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return state(); });
  component._activeTab = 3;
  component._state.areas = [];
  component._render();
  assert.equal(button(component, "Train labelled sample").disabled, true);
  component._state.areas = [{ area_id: "room1", name: "Known room" }];
  component._render();
  await button(component, "Train labelled sample").click();
  assert.equal(calls[0].location_id, "room1");
  assert.equal(calls[0].device_id, "device1");
});

test("failed preview preserves previous result and unsaved measured range draft", async () => {
  const component = panel(async () => { throw { body: { message: "Insufficient independent coverage." } }; });
  component._activeTab = 1;
  component._results.calibration_preview = { accepted: false, reason: "Prior result" };
  component._render();
  field(component, "Measured slant range to Receiver one (receiver1) (m)").value = "4.2";
  await button(component, "Preview calibration").click();
  assert.equal(component._results.calibration_preview.reason, "Prior result");
  assert.equal(field(component, "Measured slant range to Receiver one (receiver1) (m)").value, "4.2");
  assert.equal(component._message, "Insufficient independent coverage.");
});

test("import reads reserve admission and a target change prevents sending to a new entry", async () => {
  const reading = deferred();
  const calls = [];
  const component = panel(async (method, path, payload) => { calls.push([method, payload]); return state(); });
  component._render = () => {};
  const file = { size: 2, text: () => reading.promise };
  const importing = component._importHistory(file);
  await component._importHistory(file);
  assert.equal(component._fileBusy, true);
  component.panel = { config: { entry_id: "different-entry" } };
  reading.resolve("{}");
  await importing;
  assert.equal(calls.filter(([method]) => method === "POST").length, 0);
  assert.equal(component._fileBusy, false);
});

test("runtime settings and receiver limits send their values through native controls", async () => {
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return state(); });
  component._entryId = "entry1";
  component._render();
  field(component, "Enable automatic calibration").checked = true;
  field(component, "Enable reliability weighting").checked = true;
  field(component, "History window (seconds)").value = "1800";
  await button(component, "Save runtime settings").click();
  assert.deepEqual(calls[0].settings, { auto_calibration: true, fingerprint_area_selection: false, reliability_weighting: true, history_window_s: 1800 });
  field(component, "Maximum distance (m)").value = "0";
  await button(component, "Save receiver limit").click();
  assert.deepEqual(calls[1].settings, { receiver_limits: { receiver1: 0 } });
  field(component, "Filter").value = "pet";
  await button(component, "Save filter mode").click();
  assert.deepEqual(calls[2].settings, { filters: { device1: "pet" } });
  field(component, "Fixed reference map (JSON)").value = '{"device1":{"location_id":"reference","ranges":{"receiver1":3},"accuracy_m":0.1}}';
  await button(component, "Save fixed references").click();
  assert.equal(calls[3].settings.fixed_references.device1.ranges.receiver1, 3);
  field(component, "Source → binding object map (JSON)").value = '{"receiver1":{"device_registry_id":"registry1"}}';
  await button(component, "Save scanner bindings").click();
  assert.deepEqual(calls[4].settings.scanner_bindings, { receiver1: { device_registry_id: "registry1" } });
});

test("history, replay and shadow buttons dispatch complete actions with safe exports", async () => {
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return method === "POST" ? { version: 1 } : state(); });
  component._activeTab = 2;
  component._entryId = "entry1";
  component._download = () => {};
  component._copy = async () => {};
  component._render();
  for (const label of ["Start recording", "Stop recording", "Clear history", "Export JSON", "Copy recording", "Run replay", "Start shadow", "Stop shadow", "Promote shadow candidate"]) await button(component, label).click();
  assert.deepEqual(calls.map(payload => payload.action), ["history_start", "history_stop", "history_clear", "history_export", "history_export", "replay", "shadow_start", "shadow_stop", "shadow_promote"]);
  assert.deepEqual(calls[0].devices, []);
  assert.equal(calls[3].redact, true);
  assert.equal(calls[4].redact, true);
  assert.equal("candidate" in calls[5], false);
  field(component, "Record device (or all tracked devices)").value = "device1";
  await button(component, "Start recording").click();
  assert.deepEqual(calls.at(-1).devices, ["device1"]);
  field(component, "Candidate receiver offsets (JSON; empty object uses the current proposal)").value = '{"receiver1":1.5}';
  await button(component, "Run replay").click();
  assert.deepEqual(calls.at(-1).candidate, { receiver1: 1.5 });
  const file = field(component, "Import recording JSON (maximum 2 MiB)");
  file.files = [{ size: 2, text: async () => '{"version":1}' }];
  await button(component, "Import recording").click();
  assert.equal(calls.at(-1).action, "history_import");
  assert.deepEqual(calls.at(-1).data, { version: 1 });
});

test("recording one device then restarting for all explicitly clears the retained selection", async () => {
  const calls = [];
  let selectedDevices = new Set();
  let running = false;
  const component = panel(async (method, path, payload) => {
    if (method === "POST") {
      calls.push(payload);
      if (payload.action === "history_start") {
        // The history API retains its prior selection when devices is omitted.
        if (payload.devices !== undefined) selectedDevices = new Set(payload.devices);
        running = true;
      } else if (payload.action === "history_stop") running = false;
    }
    return { ...state(), history: { running, selected_devices: [...selectedDevices] } };
  });
  component._activeTab = 2;
  component._render();
  field(component, "Record device (or all tracked devices)").value = "device1";
  await button(component, "Start recording").click();
  assert.deepEqual([...selectedDevices], ["device1"]);
  assert.equal(running, true);
  await button(component, "Stop recording").click();
  assert.equal(running, false);
  assert.deepEqual([...selectedDevices], ["device1"]);
  field(component, "Record device (or all tracked devices)").value = "";
  await button(component, "Start recording").click();
  assert.deepEqual(calls.at(-1).devices, []);
  assert.deepEqual([...selectedDevices], []);
  assert.equal(running, true);
  assert.deepEqual(component._state.history.selected_devices, []);
});

test("large recording downloads and clipboard JSON retain the import byte budget", async t => {
  const frames = Array.from({ length: 1000 }, (_, index) => ({
    sequence: index + 1, captured_at: 1750000000 + index, session_epoch: 1, model_version: "synthetic-model",
    observations: Array.from({ length: 4 }, (_, scanner) => ({
      device_id: "synthetic-device", scanner_id: `receiver-${scanner}`, observed: 1750000000 + index,
      stamp: 1000 + index, rssi: -70, rssi_offset: 0, reference_power: -59, attenuation: 2,
      raw_distance: 3.55, filtered_distance: 3.5, age: 0, status: "current",
      quality: { fresh: true, reliability: .75, reasons: [] }, model_version: "synthetic-model",
    })), truth: [],
  }));
  const recording = {
    schema_version: 1, session_epoch: 1, selected_devices: [], redacted: true,
    frames,
  };
  const maximumBytes = 2 * 1024 * 1024;
  const compactBytes = new Blob([JSON.stringify(recording)]).size;
  assert.ok(compactBytes < maximumBytes);
  assert.ok(new Blob([JSON.stringify(recording, null, 2)]).size > maximumBytes, "fixture reproduces pretty JSON exceeding the import cap");
  let downloaded;
  t.mock.method(URL, "createObjectURL", blob => { downloaded = blob; return "blob:synthetic-recording"; });
  t.mock.method(URL, "revokeObjectURL", () => {});
  let copied;
  const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { copied = text; } } });
  t.after(() => { if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor); else delete navigator.clipboard; });
  const diagnostics = { status: "synthetic", quality: { fresh: false, reasons: ["coverage"] } };
  const component = panel(async (method, path, payload) => method === "POST" ? payload.action === "diagnostics" ? diagnostics : recording : state());
  component._activeTab = 2;
  component._render();
  await button(component, "Export JSON").click();
  assert.ok(downloaded instanceof Blob);
  assert.equal(downloaded.size, compactBytes);
  assert.ok(downloaded.size < maximumBytes);
  assert.deepEqual(await readJSONFile(downloaded), recording);
  await button(component, "Copy recording").click();
  assert.equal(new Blob([copied]).size, compactBytes);
  assert.deepEqual(JSON.parse(copied), recording);
  assert.deepEqual(await readJSONFile(new Blob([copied])), recording);
  component._activeTab = 4;
  component._render();
  await button(component, "Copy full diagnostics").click();
  assert.equal(copied, JSON.stringify(diagnostics, null, 2), "diagnostics copies remain readable");
  await new Promise(resolve => setTimeout(resolve, 0));
});

test("pending proposals disable apply; accepted previews, rollback and diagnostics dispatch", async () => {
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") {
      calls.push(payload);
      return payload.action === "calibration_preview" ? { eligible: true, proposal_id: "synthetic-proposal" } : { result: "done" };
    }
    return state();
  });
  component._activeTab = 1;
  component._render();
  assert.equal(button(component, "Apply accepted proposal").disabled, true);
  component._results.calibration_preview = { eligible: false, status: "pending", reasons: ["insufficient_truth"] };
  component._render();
  assert.equal(button(component, "Apply accepted proposal").disabled, true);
  await button(component, "Preview calibration").click();
  assert.equal(button(component, "Apply accepted proposal").disabled, false);
  await button(component, "Apply accepted proposal").click();
  assert.equal(calls.at(-1).proposal_id, "synthetic-proposal");
  await button(component, "Rollback calibration").click();
  assert.equal(calls.at(-1).action, "calibration_rollback");
  component._activeTab = 4;
  component._render();
  await button(component, "Load full diagnostics").click();
  assert.equal(calls.at(-1).action, "diagnostics");
  component._copy = async () => {};
  await button(component, "Copy full diagnostics").click();
  assert.equal(calls.at(-1).action, "diagnostics");
});

test("profile targets load their own drafts and beacon verification sends correct model fields", async () => {
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return state(); });
  component._render();
  assert.equal(field(component, "Receiver RSSI offset (dB)").disabled, false);
  assert.equal(field(component, "Reference power independently verified").disabled, true);
  const kind = field(component, "Profile type");
  kind.value = "beacon";
  await kind.dispatch("change");
  assert.equal(field(component, "Receiver RSSI offset (dB)").disabled, true);
  field(component, "model").value = "Synthetic measured beacon";
  field(component, "Reference power at 1 m (dBm)").value = "-59";
  field(component, "Reference power independently verified").checked = true;
  await button(component, "Save profile").click();
  assert.equal(calls[0].kind, "beacon");
  assert.equal(calls[0].id, "device1");
  assert.equal(calls[0].profile.model, "Synthetic measured beacon");
  assert.equal(calls[0].profile.reference_power, -59);
  assert.equal(calls[0].profile.reference_power_verified, true);
  assert.equal("rssi_offset" in calls[0].profile, false);
  assert.equal(calls[0].remove.includes("attenuation"), true);
  assert.equal(calls[0].remove.includes("reference_power"), false);
  assert.equal(calls[0].remove.includes("rssi_offset"), false);
});

test("role changes clear prior admin state and ignore an in-flight response", async () => {
  const loading = deferred();
  let posts = 0;
  const component = panel(async method => { if (method === "POST") posts += 1; return loading.promise; });
  component._hass.user = { id: "admin", is_admin: true };
  component._render();
  const refresh = component.refresh();
  component.hass = { user: { id: "guest", is_admin: false }, callApi: component._hass.callApi };
  assert.equal(component._state, null);
  assert.equal(component._message, "Administrator access is required.");
  loading.resolve(state());
  await refresh;
  assert.equal(component._state, null);
  await component._perform("history_clear");
  assert.equal(posts, 0);
});

test("model changes invalidate prior previews while promotion rejection stays visible", async () => {
  const changed = state();
  changed.diagnostics = { model_version: "new-model" };
  const component = panel(async method => method === "GET" ? changed : { eligible: false, reasons: ["insufficient_independent_truth"] });
  component._state.diagnostics = { model_version: "old-model" };
  component._results.calibration_preview = { eligible: true, proposal_id: "old-model-proposal" };
  component._results.replay = { result: "prior recording" };
  await component.refresh();
  assert.equal(component._results.calibration_preview, undefined);
  assert.equal(component._results.replay, undefined);
  component._activeTab = 2;
  component._render();
  await button(component, "Promote shadow candidate").click();
  assert.match(component._message, /needs more evidence/);
  assert.match(component.shadowRoot.textContent, /insufficient_independent_truth/);
});

test("fingerprint matches render confidence and rejection reasons separately from training coverage", () => {
  const component = panel();
  component._activeTab = 3;
  component._state.fingerprint_results = { device1: { accepted: false, confidence: .4, reasons: [{ code: "ambiguous" }] } };
  component._render();
  assert.match(component.shadowRoot.textContent, /Current fingerprint matches/);
  assert.match(component.shadowRoot.textContent, /ambiguous/);
  assert.match(component.shadowRoot.textContent, /0.4/);
});

test("serialized request envelope also respects the 2 MiB client bound", async () => {
  let requests = 0;
  const component = panel(async () => { requests += 1; return state(); });
  component._render = () => {};
  await component._perform("history_import", { data: { padding: "x".repeat(2 * 1024 * 1024) } });
  assert.equal(requests, 0);
  assert.match(component._message, /including metadata exceeds 2 MiB/);
  assert.equal(component._busy, false);
});

test("new measured captures gate a previous proposal until another preview completes", async () => {
  const component = panel(async method => method === "POST" ? { eligible: true } : { ...state(), calibration: { proposal: { eligible: true } } });
  component._activeTab = 1;
  component._results.calibration_preview = { eligible: true };
  component._render();
  assert.equal(button(component, "Apply accepted proposal").disabled, false);
  await component._perform("capture", { device_id: "device1" });
  assert.equal(button(component, "Apply accepted proposal").disabled, true);
  await button(component, "Preview calibration").click();
  assert.equal(button(component, "Apply accepted proposal").disabled, false);
});
