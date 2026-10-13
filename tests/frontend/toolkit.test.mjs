/** Behavioral tests for the production web component, using a small native DOM double. */
import assert from "node:assert/strict";
import test from "node:test";

class Element {
  constructor(tag = "element") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this._type = "text";
    this.checked = false;
  }
  attachShadow() { this.shadowRoot = new Element("shadow"); return this.shadowRoot; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; if (this.tagName === "select") delete this._value; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return String(this._text || "") + this.children.map(child => child.textContent).join(""); }
  set type(value) {
    if (this.tagName === "textarea" || this.tagName === "select") throw new TypeError("Native control type is read-only");
    this._type = String(value);
  }
  get type() { return this.tagName === "textarea" ? "textarea" : this.tagName === "select" ? "select-one" : this._type; }
  set value(value) {
    if (this.type === "file" && String(value) !== "") throw new DOMException("A file input may only be reset to an empty string", "InvalidStateError");
    this._value = String(value);
    if (this.type === "file") this.files = [];
  }
  get value() {
    if (this.type === "file" && this.files?.length) return `C:\\fakepath\\${this.files[0].name || "synthetic-recording.json"}`;
    if (this.tagName === "select") return this._value === undefined ? this.children[0]?.value || "" : this.children.some(child => child.value === this._value) ? this._value : "";
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
const { BermudaToolkitPanel, finiteInput, readJSONFile, receiverValue } = await import(new URL("../../custom_components/bermuda/frontend/toolkit.js", import.meta.url));

const state = () => ({
  options: { auto_calibration: false, receiver_limits: { receiver1: 0 }, filters: {} },
  snapshot: {
    devices: { device1: { device_id: "device1", name: "<script>unsafe name</script>" } },
    scanners: { receiver1: { scanner_id: "receiver1", name: "Receiver one", scanner_last_seen_age: 12 } },
  },
  areas: [{ area_id: "room1", name: "Known room" }],
});
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((done, failed) => { resolve = done; reject = failed; }); return { promise, resolve, reject }; }
function panel(callApi = async () => state()) {
  const result = new BermudaToolkitPanel();
  result._connected = true;
  result._hass = { callApi };
  result._state = state();
  return result;
}
const button = (component, label) => component.shadowRoot.all().find(element => element.tagName === "button" && element.textContent === label);
const field = (component, label) => component.shadowRoot.all().find(element => element.tagName === "label" && element.children[0]?.textContent === label)?.children[1];
async function edit(component, label, value, event = "input") {
  const input = field(component, label);
  assert.ok(input, `field ${label} exists`);
  if (input.type === "checkbox") input.checked = Boolean(value);
  else input.value = String(value);
  await input.dispatch(event);
  return input;
}

function mockClipboard(t, writeText) {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  t.after(() => { if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor); else delete navigator.clipboard; });
}

function switchContext(component, change) {
  if (change === "entry") component.panel = { config: { entry_id: "entry-b" } };
  else if (change === "disconnect") component.disconnectedCallback();
  else component.hass = { user: { id: change === "user" ? "admin-b" : "admin-a", is_admin: change !== "admin" }, callApi: component._hass.callApi };
}

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
  await edit(component, "Independent location label", "Measured spot A");
  await edit(component, "Measured slant range to Receiver one (receiver1) (m)", "-2");
  await button(component, "Capture labelled readings").click();
  assert.equal(calls.length, 0);
  await edit(component, "Measured slant range to Receiver one (receiver1) (m)", "2.25");
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
  await edit(component, "Measured slant range to Receiver one (receiver1) (m)", "4.2");
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
  await edit(component, "Enable automatic calibration", true);
  await edit(component, "Enable reliability weighting", true);
  await edit(component, "History window (seconds)", "1800");
  await button(component, "Save runtime settings").click();
  assert.deepEqual(calls[0].settings, { auto_calibration: true, reliability_weighting: true, history_window_s: 1800 });
  await edit(component, "Maximum distance (m)", "5");
  await button(component, "Save receiver limit").click();
  assert.deepEqual(calls[1].settings, { receiver_limits: { receiver1: 5 } });
  assert.equal(calls[1].patch_maps, true);
  await edit(component, "Filter", "pet", "change");
  await button(component, "Save filter mode").click();
  assert.deepEqual(calls[2].settings, { filters: { device1: "pet" } });
  assert.equal(calls[2].patch_maps, true);
  await edit(component, "Fixed reference map (JSON)", '{"device1":{"location_id":"reference","ranges":{"receiver1":3},"accuracy_m":0.1}}');
  await button(component, "Save fixed references").click();
  assert.equal(calls[3].settings.fixed_references.device1.ranges.receiver1, 3);
  await edit(component, "Source → binding object map (JSON)", '{"receiver1":{"device_registry_id":"registry1"}}');
  await button(component, "Save scanner bindings").click();
  assert.deepEqual(calls[4].settings.scanner_bindings, { receiver1: { device_registry_id: "registry1" } });
  assert.equal("patch_maps" in calls[0], false);
  assert.equal("patch_maps" in calls[3], false);
  assert.equal("patch_maps" in calls[4], false);
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
  await edit(component, "Record device (or all tracked devices)", "device1", "change");
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
  await edit(component, "Record device (or all tracked devices)", "device1", "change");
  await button(component, "Start recording").click();
  assert.deepEqual([...selectedDevices], ["device1"]);
  assert.equal(running, true);
  await button(component, "Stop recording").click();
  assert.equal(running, false);
  assert.deepEqual([...selectedDevices], ["device1"]);
  await edit(component, "Record device (or all tracked devices)", "", "change");
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
  await edit(component, "model", "Synthetic measured beacon");
  await edit(component, "Reference power at 1 m (dBm)", "-59");
  await edit(component, "Reference power independently verified", true);
  await button(component, "Save profile").click();
  assert.equal(calls[0].kind, "beacon");
  assert.equal(calls[0].id, "device1");
  assert.equal(calls[0].profile.model, "Synthetic measured beacon");
  assert.equal(calls[0].profile.reference_power, -59);
  assert.equal(calls[0].profile.reference_power_verified, true);
  assert.equal("rssi_offset" in calls[0].profile, false);
  assert.equal(calls[0].remove.includes("attenuation"), false);
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

const proposalInvalidatingControls = [
  ["history_start", "Start recording", "History & replay"],
  ["history_stop", "Stop recording", "History & replay"],
  ["history_clear", "Clear history", "History & replay"],
  ["history_import", "Import recording", "History & replay"],
  ["settings", "Save runtime settings", "Settings"],
  ["profile", "Save profile", "Settings"],
  ["calibration_apply", "Apply accepted proposal", "Calibration"],
  ["calibration_rollback", "Rollback calibration", "Calibration"],
  ["capture", "Capture labelled readings", "Calibration"],
];

for (const [action, label, tab] of proposalInvalidatingControls) {
  for (const source of ["cached", "state-only"]) {
    for (const outcome of ["refreshed", "refresh-failed", "post-failed"]) {
      test(`${action} ${outcome} keeps the ${source} proposal consistent with the acknowledged action`, async () => {
        const oldProposal = { proposal_id: "prior-verified-proposal", eligible: true };
        const freshProposal = { proposal_id: "fresh-verified-proposal", eligible: true };
        const server = state();
        server.diagnostics = { model_version: "unchanged-model" };
        server.calibration = { proposal: oldProposal, unrelated_metadata: "preserved" };
        server.options.receiver_profiles = { receiver1: { model: "old metadata", rssi_offset: 5 } };
        const requests = [];
        let gets = 0;
        const component = panel(async (method, path, payload) => {
          if (method === "GET") {
            gets += 1;
            if (outcome === "refresh-failed") throw new Error("Synthetic trailing refresh failure");
            return structuredClone(server);
          }
          requests.push(payload);
          if (payload.action === action && outcome === "post-failed") throw new Error("Synthetic POST failure");
          if (payload.action === "calibration_preview") {
            server.calibration.proposal = freshProposal;
            return { proposal: structuredClone(freshProposal) };
          }
          if (payload.action === "calibration_apply") {
            assert.equal(payload.proposal_id, server.calibration.proposal?.proposal_id, "only the currently verified server proposal can be applied");
          }
          server.calibration.proposal = null;
          if (payload.action === "settings") {
            server.options = { ...server.options, ...payload.settings };
            return { options: structuredClone(server.options) };
          }
          if (payload.action === "profile") {
            const profile = { ...server.options.receiver_profiles.receiver1, ...payload.profile };
            server.options.receiver_profiles.receiver1 = profile;
            return { profile: structuredClone(profile) };
          }
          return { completed: true };
        });
        component._state = structuredClone(server);
        const stateProposal = component._state.calibration.proposal;
        const cachedPreview = { proposal: structuredClone(oldProposal) };
        if (source === "cached") component._results.calibration_preview = cachedPreview;
        component._activeTab = 1; component._render();
        assert.equal(button(component, "Apply accepted proposal").disabled, false);
        await edit(component, "Independent location label", "Unsaved measured truth");
        const truthRevision = component._draftVersions.get("capture-location");
        await button(component, tab).click();
        if (action === "history_import") {
          const text = '{"schema_version":1,"frames":[]}';
          field(component, "Import recording JSON (maximum 2 MiB)").files = [{ name: "recording.json", size: text.length, text: async () => text }];
        } else if (action === "settings") await edit(component, "Enable automatic calibration", true);
        else if (action === "profile") await edit(component, "model", "saved metadata");
        else if (action === "capture") await edit(component, "Measured slant range to Receiver one (receiver1) (m)", "2.5");
        await button(component, label).click();
        assert.equal(requests[0].action, action);
        assert.equal(component._state.diagnostics.model_version, "unchanged-model");
        assert.equal(component._state.calibration.unrelated_metadata, "preserved");
        assert.equal(component._drafts.get("capture-location"), "Unsaved measured truth");
        assert.equal(component._draftVersions.get("capture-location"), truthRevision);
        await button(component, "Calibration").click();
        if (outcome === "post-failed") {
          assert.equal(gets, 0);
          assert.equal(component._message, "Synthetic POST failure");
          assert.equal(component._state.calibration.proposal, stateProposal);
          assert.equal(component._results.calibration_preview, source === "cached" ? cachedPreview : undefined);
          assert.equal(button(component, "Apply accepted proposal").disabled, false, "failed mutation leaves the verified proposal available");
          return;
        }
        assert.equal(gets, 1);
        assert.equal(component._results.calibration_preview, undefined);
        assert.equal(component._state.calibration.proposal, null, "the old state fallback is retired even when the refresh fails");
        assert.equal(button(component, "Apply accepted proposal").disabled, true);
        const requestCount = requests.length;
        await button(component, "Apply accepted proposal").click();
        assert.equal(requests.length, requestCount, "a disabled Apply control cannot submit the stale proposal");
        if (action === "settings") assert.equal(component._options().auto_calibration, true, "options acknowledgement survives invalidation");
        if (action === "profile") assert.equal(component._profile("receiver", "receiver1").model, "saved metadata", "profile acknowledgement survives invalidation");
        if (outcome === "refresh-failed") assert.equal(component._message, "Synthetic trailing refresh failure");
        await button(component, "Preview calibration").click();
        assert.equal(button(component, "Apply accepted proposal").disabled, false, "a fresh accepted preview restores Apply");
        await button(component, "Apply accepted proposal").click();
        assert.equal(requests.at(-1).action, "calibration_apply");
        assert.equal(requests.at(-1).proposal_id, "fresh-verified-proposal");
      });
    }
  }
}

test("native textarea and selected-file restrictions hold across all tabs and import renders", async () => {
  assert.throws(() => { new Element("textarea").type = "text"; }, TypeError);
  const nativeFile = new Element("input"); nativeFile.type = "file";
  assert.throws(() => { nativeFile.value = "C:\\fakepath\\recording.json"; }, { name: "InvalidStateError" });
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return state(); });
  for (let tab = 0; tab < 5; tab += 1) { component._activeTab = tab; assert.doesNotThrow(() => component._render()); }
  component._activeTab = 2; component._render();
  let file = field(component, "Import recording JSON (maximum 2 MiB)");
  file.files = [{ name: "recording.json", size: 2, text: async () => "{}" }];
  assert.match(file.value, /fakepath/);
  await component.refresh();
  assert.equal(component._drafts.has("history-file"), false);
  file = field(component, "Import recording JSON (maximum 2 MiB)");
  file.files = [{ name: "recording.json", size: 2, text: async () => "{}" }];
  await button(component, "Import recording").click();
  assert.equal(calls.at(-1).action, "history_import");
  assert.equal(component._fileBusy, false);
  file = field(component, "Import recording JSON (maximum 2 MiB)");
  file.files = [{ name: "broken.json", size: 1, text: async () => "{" }];
  await button(component, "Import recording").click();
  assert.equal(component._fileBusy, false);
  assert.equal(calls.length, 1);
});

test("untouched controls reflect new server radio, automatic setting and history window", async () => {
  const server = state(); server.options.receiver_profiles = { receiver1: { rssi_offset: 1 } };
  const component = panel(async () => structuredClone(server));
  component._state = structuredClone(server); component._render();
  assert.equal(component._drafts.size, 0);
  server.options.receiver_profiles.receiver1.rssi_offset = 5;
  server.options.auto_calibration = true;
  server.options.history_window_s = 7200;
  await component.refresh();
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "5");
  assert.equal(field(component, "Enable automatic calibration").checked, true);
  assert.equal(field(component, "History window (seconds)").value, "7200");
  assert.equal(component._drafts.size, 0);
});

test("metadata and runtime saves send only edited fields and preserve concurrent radio and auto changes", async () => {
  const server = state(); server.options.receiver_profiles = { receiver1: { rssi_offset: 1, model: "original" } };
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "GET") return structuredClone(server);
    calls.push(payload);
    if (payload.action === "profile") {
      const profile = { ...server.options.receiver_profiles.receiver1, ...payload.profile };
      for (const key of payload.remove) delete profile[key];
      server.options.receiver_profiles.receiver1 = profile;
      return { profile: structuredClone(profile) };
    }
    server.options = { ...server.options, ...payload.settings };
    return { options: structuredClone(server.options) };
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "model", "submitted metadata");
  server.options.receiver_profiles.receiver1.rssi_offset = 5;
  server.options.auto_calibration = true;
  await button(component, "Save profile").click();
  assert.deepEqual(calls[0].profile, { model: "submitted metadata" });
  assert.deepEqual(calls[0].remove, []);
  assert.equal(server.options.receiver_profiles.receiver1.rssi_offset, 5);
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "5");
  assert.equal(field(component, "Enable automatic calibration").checked, true);
  assert.equal(component._drafts.has("profile-model"), false);
  server.options.receiver_profiles.receiver1.model = "external metadata";
  await component.refresh();
  assert.equal(field(component, "model").value, "external metadata");
  await edit(component, "History window (seconds)", "1800");
  server.options.auto_calibration = false;
  await button(component, "Save runtime settings").click();
  assert.deepEqual(calls[1].settings, { history_window_s: 1800 });
  assert.equal(server.options.auto_calibration, false);
});

test("failed saves preserve deliberately edited fields through refresh and tab changes", async () => {
  const server = state(); server.options.receiver_profiles = { receiver1: { rssi_offset: 1, model: "original" } };
  const component = panel(async method => { if (method === "POST") throw { body: { message: "Synthetic save failed" } }; return structuredClone(server); });
  component._state = structuredClone(server); component._render();
  await edit(component, "model", "unsaved metadata");
  await button(component, "Save profile").click();
  assert.equal(field(component, "model").value, "unsaved metadata");
  component._activeTab = 4; component._render();
  server.options.receiver_profiles.receiver1 = { model: "external metadata", rssi_offset: 8 };
  await component.refresh();
  component._activeTab = 0; component._render();
  assert.equal(field(component, "model").value, "unsaved metadata");
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "8");
  await edit(component, "Enable automatic calibration", true);
  await button(component, "Save runtime settings").click();
  await component.refresh();
  assert.equal(field(component, "Enable automatic calibration").checked, true);
  assert.equal(component._message, "Synthetic save failed");
});

test("newer edits during a save survive its acknowledgement and old target replies cannot clear new drafts", async () => {
  const pending = deferred(); const server = state(); server.options.receiver_profiles = { receiver1: { model: "original", rssi_offset: 1 } };
  const component = panel(async (method, path, payload) => {
    if (method === "GET") return structuredClone(server);
    await pending.promise;
    const profile = { ...server.options.receiver_profiles.receiver1, ...payload.profile };
    server.options.receiver_profiles.receiver1 = profile;
    return { profile };
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "model", "first submission");
  const saving = button(component, "Save profile").click();
  await edit(component, "model", "newer unsaved metadata");
  server.options.receiver_profiles.receiver1.rssi_offset = 9;
  pending.resolve(); await saving;
  assert.equal(field(component, "model").value, "newer unsaved metadata");
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "9");
  assert.equal(component._drafts.has("profile-model"), true);
  const oldReply = deferred();
  component._hass.callApi = async method => method === "GET" ? state() : oldReply.promise;
  const oldSaving = button(component, "Save profile").click();
  component.panel = { config: { entry_id: "new-entry" } };
  await component.refresh();
  await edit(component, "model", "new entry draft");
  oldReply.resolve({ profile: { model: "old entry acknowledgement" } }); await oldSaving;
  assert.equal(field(component, "model").value, "new entry draft");
  assert.equal(component._drafts.get("profile-model"), "new entry draft");
});

test("explicitly clearing an edited override removes only that field", async () => {
  const server = state(); server.options.receiver_profiles = { receiver1: { attenuation: 2.5, rssi_offset: 7 } };
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return structuredClone(server); });
  component._state = structuredClone(server); component._render();
  await edit(component, "Path-loss exponent", "");
  await button(component, "Save profile").click();
  assert.deepEqual(calls[0].profile, {});
  assert.deepEqual(calls[0].remove, ["attenuation"]);
  assert.equal("rssi_offset" in calls[0].profile, false);
  assert.equal(component._drafts.has("profile-attenuation"), false);
});

test("successful acknowledgement survives a failed refresh and a failed POST preserves newer edits", async () => {
  const component = panel(async method => {
    if (method === "GET") throw { body: { message: "Synthetic refresh failure" } };
    return { profile: { model: "saved metadata", rssi_offset: 5 } };
  });
  component._render();
  await edit(component, "model", "saved metadata");
  await button(component, "Save profile").click();
  assert.equal(field(component, "model").value, "saved metadata");
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "5");
  assert.equal(component._drafts.has("profile-model"), false);
  const pending = deferred();
  component._hass.callApi = async method => method === "POST" ? pending.promise : state();
  await edit(component, "model", "submitted edit");
  const saving = button(component, "Save profile").click();
  await edit(component, "model", "newer edit during failed save");
  pending.reject({ body: { message: "Synthetic POST failure" } }); await saving;
  assert.equal(field(component, "model").value, "newer edit during failed save");
  assert.equal(component._drafts.get("profile-model"), "newer edit during failed save");
  assert.equal(component._message, "Synthetic POST failure");
});

test("stale control events cannot create drafts after an entry or role change", async () => {
  const component = panel(); component._render();
  const priorInput = field(component, "model");
  component.panel = { config: { entry_id: "new-entry" } }; await component.refresh();
  priorInput.value = "old entry edit"; await priorInput.dispatch("input");
  assert.equal(component._drafts.has("profile-model"), false);
  const newInput = field(component, "model");
  component._hass.user = { id: "admin", is_admin: true };
  component.hass = { user: { id: "guest", is_admin: false }, callApi: component._hass.callApi };
  newInput.value = "old role edit"; await newInput.dispatch("change");
  assert.equal(component._drafts.size, 0);
});

test("clipboard denial directs recording users to export and diagnostics users to visible Health JSON", async t => {
  const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async () => { throw new DOMException("Denied", "NotAllowedError"); } } });
  t.after(() => { if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor); else delete navigator.clipboard; });
  const component = panel(); component._activeTab = 2; component._render();
  await component._copy({ frames: [] }, { compact: true });
  assert.match(component._message, /Use Export JSON/);
  assert.ok(button(component, "Export JSON"));
  component._activeTab = 4; component._results.diagnostics = { quality: "synthetic" }; component._render();
  await component._copy(component._results.diagnostics);
  assert.match(component._message, /diagnostics JSON in Health/);
  assert.match(component.shadowRoot.textContent, /synthetic/);
});

for (const [label, tab] of [["Copy full diagnostics", 4], ["Copy recording", 2]]) {
  for (const change of ["entry", "user", "admin", "disconnect"]) {
    test(`${label} discards a result when ${change} changes during its refresh`, async t => {
      const copied = [];
      mockClipboard(t, async text => { copied.push(text); });
      const loading = deferred();
      let gets = 0;
      const replacement = { ...state(), entry_id: "entry-b", diagnostics: { owner: "admin-b" } };
      const component = panel(async method => method === "POST" ? { prior_context: "private synthetic result" } : ++gets === 1 ? loading.promise : replacement);
      component._entryId = "entry-a";
      component._hass.user = { id: "admin-a", is_admin: true };
      component._activeTab = tab; component._render();
      const copying = button(component, label).click();
      await tick();
      assert.equal(gets, 1);
      assert.equal(component._busy, true);
      switchContext(component, change);
      component._message = "Current context status";
      loading.resolve(state());
      await copying;
      assert.deepEqual(copied, []);
      assert.equal(component._message, "Current context status");
      assert.equal(component._busy, false);
      if (change === "entry" || change === "user") {
        assert.equal(gets, 2, "the replacement context gets the bounded trailing read");
        assert.equal(component._state, replacement);
      } else assert.equal(gets, 1, "denied or disconnected contexts do not start another read");
    });
  }
}

for (const [change, label, tab, failure] of [
  ["entry", "Copy full diagnostics", 4, false],
  ["user", "Copy recording", 2, true],
  ["admin", "Copy full diagnostics", 4, true],
  ["disconnect", "Copy recording", 2, false],
]) {
  test(`${label} ${failure ? "failure" : "completion"} cannot update status after a ${change} change`, async t => {
    const copyingText = deferred();
    const copied = [];
    mockClipboard(t, text => { copied.push(text); return copyingText.promise; });
    const component = panel(async method => method === "POST" ? { synthetic: "current-context result" } : state());
    component._hass.user = { id: "admin-a", is_admin: true };
    component._activeTab = tab; component._render();
    const copying = button(component, label).click();
    await tick();
    assert.equal(copied.length, 1, "the current context initiated the clipboard write");
    switchContext(component, change);
    await tick();
    component._message = "Replacement context status";
    let renders = 0;
    t.mock.method(component, "_render", () => { renders += 1; });
    if (failure) copyingText.reject(new DOMException("Denied", "NotAllowedError"));
    else copyingText.resolve();
    await copying;
    assert.equal(component._message, "Replacement context status");
    assert.equal(renders, 0);
  });
}

for (const action of ["profile", "settings"]) {
  for (const olderFails of [false, true]) {
    for (const trailingFails of [false, true]) {
      for (const newerEdit of [false, true]) {
        test(`${action} acknowledgement survives a pre-save GET ${olderFails ? "error" : "response"}, trailing GET ${trailingFails ? "failure" : "success"}${newerEdit ? " and newer edits" : ""}`, async () => {
          const older = deferred(), trailing = deferred();
          const server = state();
          server.options.receiver_profiles = { receiver1: { model: "old metadata", rssi_offset: 1 } };
          server.options.history_window_s = 900;
          const beforeSave = structuredClone(server);
          let gets = 0;
          const component = panel(async (method, path, payload) => {
            if (method === "GET") return ++gets === 1 ? older.promise : trailing.promise;
            if (action === "profile") {
              server.options.receiver_profiles.receiver1 = { model: payload.profile.model, rssi_offset: 5 };
              return { profile: structuredClone(server.options.receiver_profiles.receiver1) };
            }
            server.options = { ...server.options, ...payload.settings, auto_calibration: true };
            return { options: structuredClone(server.options) };
          });
          component._state = structuredClone(server); component._render();
          const label = action === "profile" ? "model" : "History window (seconds)";
          const draftName = action === "profile" ? "profile-model" : "window";
          const saved = action === "profile" ? "saved metadata" : "1800";
          const newer = action === "profile" ? "newer unsaved metadata" : "2400";
          const reading = component.refresh();
          await edit(component, label, saved);
          const saving = button(component, action === "profile" ? "Save profile" : "Save runtime settings").click();
          await tick();
          assert.equal(component._drafts.has(draftName), false);
          if (newerEdit) await edit(component, label, newer);
          const draftVersion = component._draftVersions.get(draftName);
          if (olderFails) older.reject(new Error("Synthetic pre-save read failure"));
          else older.resolve(beforeSave);
          await tick();
          assert.equal(gets, 2, "the save shares one bounded trailing refresh");
          assert.match(component._message, /completed\./, "a superseded read cannot replace the save status");
          assert.equal(action === "profile" ? component._profile("receiver", "receiver1").model : String(component._options().history_window_s), saved);
          assert.equal(field(component, label).value, newerEdit ? newer : saved);
          if (trailingFails) trailing.reject(new Error("Synthetic trailing refresh failure"));
          else {
            server.options.receiver_profiles.receiver1.rssi_offset = 7;
            server.options.reliability_weighting = true;
            trailing.resolve(structuredClone(server));
          }
          await Promise.all([reading, saving]);
          assert.equal(gets, 2);
          assert.equal(field(component, label).value, newerEdit ? newer : saved);
          assert.equal(component._drafts.has(draftName), newerEdit);
          assert.equal(component._draftVersions.get(draftName), draftVersion);
          if (newerEdit) assert.equal(component._drafts.get(draftName), newer);
          if (trailingFails) assert.equal(component._message, "Synthetic trailing refresh failure");
          else {
            assert.equal(field(component, "Receiver RSSI offset (dB)").value, "7");
            assert.equal(field(component, "Enable reliability weighting").checked, true);
          }
        });
      }
    }
  }
}

for (const action of ["profile", "settings"]) {
  test(`a failed ${action} POST leaves the pending GET usable and keeps newer drafts`, async () => {
    const loading = deferred(), posting = deferred();
    let gets = 0;
    const component = panel(async method => { if (method === "GET") { gets += 1; return loading.promise; } return posting.promise; });
    component._render();
    const label = action === "profile" ? "model" : "History window (seconds)";
    const draftName = action === "profile" ? "profile-model" : "window";
    const reading = component.refresh();
    await edit(component, label, action === "profile" ? "submitted metadata" : "1800");
    const saving = button(component, action === "profile" ? "Save profile" : "Save runtime settings").click();
    const newer = action === "profile" ? "newer unsaved metadata" : "2400";
    await edit(component, label, newer);
    const draftVersion = component._draftVersions.get(draftName);
    posting.reject(new Error("Synthetic POST failure"));
    await saving;
    const fresh = state();
    fresh.options.receiver_profiles = { receiver1: { model: "external metadata", rssi_offset: 9 } };
    fresh.options.history_window_s = 3600;
    loading.resolve(fresh);
    await reading;
    assert.equal(component._state, fresh);
    assert.equal(gets, 1);
    assert.equal(field(component, label).value, newer);
    assert.equal(component._drafts.get(draftName), newer);
    assert.equal(component._draftVersions.get(draftName), draftVersion);
    assert.equal(field(component, "Receiver RSSI offset (dB)").value, "9");
    assert.equal(component._message, "Synthetic POST failure");
  });
}

function twoTargetState() {
  const receiverA = "aa:bb:cc:dd:ee:11", receiverB = "aa:bb:cc:dd:ee:22";
  const deviceA = "private_" + "a".repeat(32), deviceB = "private_" + "b".repeat(32);
  const server = state();
  server.options.receiver_limits = { [receiverA]: 0, [receiverB]: 7 };
  server.options.filters = { [deviceA]: "legacy", [deviceB]: "pet" };
  server.snapshot.scanners = Object.fromEntries([receiverA, receiverB].map(id => [id, { scanner_id: id, name: "Synthetic receiver" }]));
  server.snapshot.devices = Object.fromEntries([deviceA, deviceB].map(id => [id, { device_id: id, name: "Synthetic beacon" }]));
  return { server, receiverA, receiverB, deviceA, deviceB };
}

function applySettingRequest(server, payload) {
  for (const [name, value] of Object.entries(payload.settings)) {
    server.options[name] = payload.patch_maps && ["receiver_limits", "filters"].includes(name) ? { ...server.options[name], ...value } : value;
  }
  return { options: structuredClone(server.options) };
}

test("removed receivers cannot redirect limit drafts and reappearance requires a fresh edit", async () => {
  const { server, receiverA, receiverB } = twoTargetState();
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "GET") return structuredClone(server);
    calls.push(payload); return applySettingRequest(server, payload);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Maximum distance (m)", "30");
  await edit(component, "History window (seconds)", "1800");
  const removed = server.snapshot.scanners[receiverA];
  delete server.snapshot.scanners[receiverA];
  await component.refresh();
  assert.equal(field(component, "Receiver").value, receiverB);
  assert.equal(field(component, "Maximum distance (m)").value, "7");
  await button(component, "Save receiver limit").click();
  assert.equal(calls.length, 0);
  server.snapshot.scanners = { [receiverA]: removed, ...server.snapshot.scanners };
  await component.refresh();
  assert.equal(field(component, "Receiver").value, receiverB);
  await edit(component, "Receiver", receiverA, "change");
  assert.equal(field(component, "Maximum distance (m)").value, "0");
  await button(component, "Save receiver limit").click();
  assert.equal(calls.length, 0);
  await edit(component, "Maximum distance (m)", "12");
  await component.refresh();
  await button(component, "Save receiver limit").click();
  assert.deepEqual(calls[0].settings, { receiver_limits: { [receiverA]: 12 } });
  assert.equal(field(component, "History window (seconds)").value, "1800");
});

test("removed devices cannot redirect filter drafts or restore them on reappearance", async () => {
  const { server, deviceA, deviceB } = twoTargetState();
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "GET") return structuredClone(server);
    calls.push(payload); return applySettingRequest(server, payload);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Device", deviceA, "change");
  await edit(component, "Filter", "stationary", "change");
  const removed = server.snapshot.devices[deviceA];
  delete server.snapshot.devices[deviceA];
  await component.refresh();
  assert.equal(field(component, "Device").value, deviceB);
  assert.equal(field(component, "Filter").value, "pet");
  await button(component, "Save filter mode").click();
  assert.equal(calls.length, 0);
  server.snapshot.devices = { [deviceA]: removed, ...server.snapshot.devices };
  await component.refresh();
  assert.equal(field(component, "Device").value, deviceB);
  await edit(component, "Device", deviceA, "change");
  assert.equal(field(component, "Filter").value, "legacy");
  await button(component, "Save filter mode").click();
  assert.equal(calls.length, 0);
  await edit(component, "Filter", "pet", "change");
  await component.refresh();
  await button(component, "Save filter mode").click();
  assert.deepEqual(calls[0].settings, { filters: { [deviceA]: "pet" } });
});

for (const kind of ["receiver", "beacon"]) {
  test(`removed ${kind} profile targets cannot redirect radio, metadata or verification drafts`, async () => {
    const { server, receiverA, receiverB, deviceA, deviceB } = twoTargetState();
    const [idA, idB] = kind === "receiver" ? [receiverA, receiverB] : [deviceA, deviceB];
    const roster = kind === "receiver" ? "scanners" : "devices";
    const key = kind === "receiver" ? "rssi_offset" : "reference_power";
    const label = kind === "receiver" ? "Receiver RSSI offset (dB)" : "Reference power at 1 m (dBm)";
    server.options[`${kind}_profiles`] = {
      [idA]: { model: "saved A", [key]: 1, reference_power_verified: false },
      [idB]: { model: "saved B", [key]: 2, reference_power_verified: false },
    };
    const calls = [];
    const component = panel(async (method, path, payload) => {
      if (method === "POST") calls.push(payload);
      return structuredClone(server);
    });
    component._state = structuredClone(server); component._render();
    await edit(component, "Profile type", kind, "change");
    await edit(component, "Profile target", idA, "change");
    await edit(component, label, "10");
    await edit(component, "model", "unsaved A");
    if (kind === "beacon") await edit(component, "Reference power independently verified", true);
    const removed = server.snapshot[roster][idA];
    delete server.snapshot[roster][idA];
    await component.refresh();
    assert.equal(field(component, "Profile target").value, idB);
    assert.equal(field(component, label).value, "2");
    assert.equal(field(component, "model").value, "saved B");
    assert.equal(field(component, "Reference power independently verified").checked, false);
    await button(component, "Save profile").click();
    assert.equal(calls.length, 0);
    server.snapshot[roster] = { [idA]: removed, ...server.snapshot[roster] };
    await component.refresh();
    assert.equal(field(component, "Profile target").value, idB);
    await edit(component, "Profile target", idA, "change");
    assert.equal(field(component, label).value, "1");
    assert.equal(field(component, "model").value, "saved A");
    await button(component, "Save profile").click();
    assert.equal(calls.length, 0);
    await edit(component, label, "20");
    await component.refresh();
    await button(component, "Save profile").click();
    assert.equal(calls[0].kind, kind);
    assert.equal(calls[0].id, idA);
    assert.deepEqual(calls[0].profile, { [key]: 20 });
  });
}

test("roster changes preserve a present implicit target and its drafts despite reordered choices", async () => {
  const { server, receiverA, receiverB, deviceA, deviceB } = twoTargetState();
  const component = panel(async () => structuredClone(server));
  component._state = structuredClone(server); component._render();
  await edit(component, "Maximum distance (m)", "30");
  await edit(component, "Filter", "stationary", "change");
  await edit(component, "model", "unsaved A");
  server.snapshot.scanners = { [receiverB]: server.snapshot.scanners[receiverB], [receiverA]: server.snapshot.scanners[receiverA] };
  server.snapshot.devices = { [deviceB]: server.snapshot.devices[deviceB], [deviceA]: server.snapshot.devices[deviceA] };
  await component.refresh();
  assert.equal(field(component, "Receiver").value, receiverA);
  assert.equal(field(component, "Maximum distance (m)").value, "30");
  assert.equal(field(component, "Device").value, deviceA);
  assert.equal(field(component, "Filter").value, "stationary");
  assert.equal(field(component, "Profile target").value, receiverA);
  assert.equal(field(component, "model").value, "unsaved A");
});

test("target removal while settings are hidden clears drafts before the target reappears", async () => {
  const { server, receiverA, deviceA } = twoTargetState();
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") calls.push(payload);
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Maximum distance (m)", "30");
  await edit(component, "Filter", "stationary", "change");
  await edit(component, "model", "unsaved A");
  component._activeTab = 4; component._render();
  const scanners = server.snapshot.scanners, devices = server.snapshot.devices;
  server.snapshot.scanners = {}; server.snapshot.devices = {};
  await component.refresh();
  server.snapshot.scanners = scanners; server.snapshot.devices = devices;
  await component.refresh();
  component._activeTab = 0; component._render();
  assert.equal(field(component, "Receiver").value, receiverA);
  assert.equal(field(component, "Device").value, deviceA);
  assert.equal(field(component, "Maximum distance (m)").value, "0");
  assert.equal(field(component, "Filter").value, "legacy");
  assert.equal(field(component, "model").value, "");
  for (const label of ["Save receiver limit", "Save filter mode", "Save profile"]) await button(component, label).click();
  assert.equal(calls.length, 0);
});

test("profile kind changes replace a remembered target and scope drafts even when IDs overlap", async () => {
  const { server, receiverA, receiverB } = twoTargetState();
  server.snapshot.devices = Object.fromEntries([receiverA, receiverB].map(id => [id, { device_id: id, name: "Synthetic beacon" }]));
  server.options.receiver_profiles = { [receiverA]: { model: "receiver A" }, [receiverB]: { model: "receiver B" } };
  server.options.beacon_profiles = { [receiverA]: { model: "beacon A" }, [receiverB]: { model: "beacon B" } };
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") calls.push(payload);
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Profile target", receiverB, "change");
  await edit(component, "model", "unsaved receiver B");
  await edit(component, "Profile type", "beacon", "change");
  assert.equal(field(component, "Profile target").value, receiverA);
  assert.equal(field(component, "model").value, "beacon A");
  await edit(component, "model", "edited beacon A");
  await component.refresh();
  assert.equal(field(component, "Profile target").value, receiverA);
  assert.equal(field(component, "model").value, "edited beacon A");
  await edit(component, "Profile type", "receiver", "change");
  assert.equal(field(component, "Profile target").value, receiverA);
  assert.equal(field(component, "model").value, "receiver A");
  await button(component, "Save profile").click();
  assert.equal(calls.length, 0);
  await edit(component, "model", "edited receiver A");
  await component.refresh();
  await button(component, "Save profile").click();
  assert.equal(calls[0].kind, "receiver");
  assert.equal(calls[0].id, receiverA);
  assert.deepEqual(calls[0].profile, { model: "edited receiver A" });
});

for (const availableKind of ["receiver", "beacon", "none"]) test(`profile type changes update save availability with ${availableKind} targets`, async () => {
  const server = state();
  if (availableKind !== "receiver") server.snapshot.scanners = {};
  if (availableKind !== "beacon") server.snapshot.devices = {};
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") calls.push(payload);
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._render();
  for (const kind of ["beacon", "receiver", "beacon", "receiver"]) {
    await edit(component, "Profile type", kind, "change");
    assert.equal(button(component, "Save profile").disabled, kind !== availableKind);
  }
  if (availableKind === "none") {
    await button(component, "Save profile").click();
    assert.equal(calls.length, 0);
    return;
  }
  await edit(component, "Profile type", availableKind, "change");
  await edit(component, "model", "Synthetic profile edit");
  await button(component, "Save profile").click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].kind, availableKind);
  assert.equal(calls[0].id, availableKind === "receiver" ? "receiver1" : "device1");
  assert.deepEqual(calls[0].profile, { model: "Synthetic profile edit" });
});

for (const busyFlag of ["_busy", "_fileBusy"]) test(`profile type change retains ${busyFlag} action blocking`, async () => {
  const component = panel(); component._render();
  component[busyFlag] = true;
  await edit(component, "Profile type", "beacon", "change");
  assert.equal(button(component, "Save profile").disabled, true);
  component[busyFlag] = false;
  await edit(component, "Profile type", "receiver", "change");
  assert.equal(button(component, "Save profile").disabled, false);
});

test("clearing verified beacon reference power also clears its verification and restores inheritance", async () => {
  const server = state();
  server.options.beacon_profiles = { device1: { reference_power: -58, reference_power_verified: true, model: "Known hardware" } };
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") {
      calls.push(payload);
      const merged = { ...server.options.beacon_profiles[payload.id], ...payload.profile };
      for (const key of payload.remove) delete merged[key];
      assert.ok(!merged.reference_power_verified || Object.hasOwn(merged, "reference_power"));
      server.options.beacon_profiles[payload.id] = merged;
      return { profile: merged };
    }
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Profile type", "beacon", "change");
  assert.equal(field(component, "Reference power independently verified").checked, true);
  await edit(component, "Reference power at 1 m (dBm)", "");
  await button(component, "Save profile").click();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].remove, ["reference_power"]);
  assert.deepEqual(calls[0].profile, { reference_power_verified: false });
  assert.equal(field(component, "Reference power at 1 m (dBm)").value, "");
  assert.equal(field(component, "Reference power independently verified").checked, false);
  assert.equal(field(component, "model").value, "Known hardware");
});

test("an inheritance acknowledgement preserves newer reference power and verification edits", async () => {
  const server = state();
  server.options.beacon_profiles = { device1: { reference_power: -58, reference_power_verified: true } };
  const pending = deferred(); const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") { calls.push(payload); return pending.promise; }
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Profile type", "beacon", "change");
  await edit(component, "Reference power at 1 m (dBm)", "");
  const saving = button(component, "Save profile").click();
  assert.equal(calls[0].profile.reference_power_verified, false);
  await edit(component, "Reference power at 1 m (dBm)", "-55");
  await edit(component, "Reference power independently verified", false, "change");
  await edit(component, "Reference power independently verified", true, "change");
  server.options.beacon_profiles.device1 = { reference_power_verified: false };
  pending.resolve({ profile: server.options.beacon_profiles.device1 });
  await saving;
  assert.equal(field(component, "Reference power at 1 m (dBm)").value, "-55");
  assert.equal(field(component, "Reference power independently verified").checked, true);
  assert.equal(component._drafts.get("profile-reference_power"), "-55");
  assert.equal(component._drafts.get("profile-verified"), true);
});

test("a profile acknowledgement cannot clear newer edits after explicit target and kind selection", async () => {
  const { server, receiverA, deviceA } = twoTargetState();
  const pending = deferred(); const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "GET") return structuredClone(server);
    calls.push(payload); return pending.promise;
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "model", "submitted receiver A");
  const saving = button(component, "Save profile").click();
  await edit(component, "Profile type", "beacon", "change");
  await edit(component, "model", "newer beacon A");
  pending.resolve({ profile: { model: "submitted receiver A" } });
  await saving;
  assert.equal(calls[0].id, receiverA);
  assert.equal(calls[0].kind, "receiver");
  assert.equal(field(component, "Profile target").value, deviceA);
  assert.equal(field(component, "model").value, "newer beacon A");
  assert.equal(component._drafts.get("profile-model"), "newer beacon A");
});

test("detached target controls cannot overwrite or discard a replacement target's newer drafts", async () => {
  const { server, receiverA, receiverB, deviceA } = twoTargetState();
  const component = panel(async () => structuredClone(server));
  component._state = structuredClone(server); component._render();
  const prior = Object.fromEntries(["Receiver", "Maximum distance (m)", "Device", "Filter", "Profile type", "Profile target", "model"].map(label => [label, field(component, label)]));
  delete server.snapshot.scanners[receiverA]; delete server.snapshot.devices[deviceA];
  await component.refresh();
  await edit(component, "Maximum distance (m)", "18");
  await edit(component, "Filter", "stationary", "change");
  await edit(component, "model", "newer receiver B");
  prior.Receiver.value = receiverA; await prior.Receiver.dispatch("change");
  prior["Maximum distance (m)"].value = "30"; await prior["Maximum distance (m)"].dispatch("input");
  prior.Device.value = deviceA; await prior.Device.dispatch("change");
  prior.Filter.value = "legacy"; await prior.Filter.dispatch("change");
  prior["Profile target"].value = receiverA; await prior["Profile target"].dispatch("change");
  prior["Profile type"].value = "beacon"; await prior["Profile type"].dispatch("change");
  prior.model.value = "stale receiver A"; await prior.model.dispatch("input");
  await component.refresh();
  assert.equal(field(component, "Receiver").value, receiverB);
  assert.equal(field(component, "Maximum distance (m)").value, "18");
  assert.equal(field(component, "Filter").value, "stationary");
  assert.equal(field(component, "Profile target").value, receiverB);
  assert.equal(field(component, "model").value, "newer receiver B");
});

test("calibration requires reselection after device loss and clears truth for a different device", async () => {
  const { server, receiverA, deviceA, deviceB } = twoTargetState();
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") calls.push(payload);
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._activeTab = 1; component._render();
  const rangeLabel = `Measured slant range to Synthetic receiver (${receiverA}) (m)`;
  await edit(component, "Independent location label", "Measured device A");
  await edit(component, "Measurement tolerance (m)", ".25");
  await edit(component, rangeLabel, "2.25");
  const removed = server.snapshot.devices[deviceA]; delete server.snapshot.devices[deviceA];
  await component.refresh();
  assert.match(field(component, "Device").textContent, /selection became unavailable/);
  assert.notEqual(field(component, "Device").value, deviceB);
  assert.equal(button(component, "Capture labelled readings").disabled, true);
  button(component, "Capture labelled readings").disabled = false;
  await button(component, "Capture labelled readings").click();
  assert.equal(calls.length, 0, "source guard rejects unavailable selections even if the button is enabled");
  server.snapshot.devices = { [deviceA]: removed, ...server.snapshot.devices };
  await component.refresh();
  assert.equal(button(component, "Capture labelled readings").disabled, true);
  await edit(component, "Device", deviceA, "change");
  assert.equal(field(component, "Independent location label").value, "Measured device A");
  await button(component, "Capture labelled readings").click();
  assert.equal(calls[0].device_id, deviceA);
  await edit(component, "Device", deviceB, "change");
  assert.equal(field(component, "Independent location label").value, "");
  assert.equal(field(component, "Measurement tolerance (m)").value, "0.1");
  assert.equal(field(component, rangeLabel).value, "");
  await button(component, "Capture labelled readings").click();
  assert.equal(calls.length, 1);
  await edit(component, "Independent location label", "Measured device B");
  await edit(component, rangeLabel, "4");
  await button(component, "Capture labelled readings").click();
  assert.equal(calls[1].device_id, deviceB);
  assert.equal(calls[1].location_id, "Measured device B");
  assert.deepEqual(calls[1].ranges, { [receiverA]: 4 });
});

for (const missing of ["device", "area"]) {
  test(`fingerprint ${missing} loss retains identity and requires explicit reselection before training`, async () => {
    const { server, deviceA, deviceB } = twoTargetState();
    server.areas = [{ area_id: "roomA", name: "Room A" }, { area_id: "roomB", name: "Room B" }];
    const calls = [];
    const component = panel(async (method, path, payload) => {
      if (method === "POST") calls.push(payload);
      return structuredClone(server);
    });
    component._state = structuredClone(server); component._activeTab = 3; component._render();
    const label = missing === "device" ? "Device" : "Independently known Home Assistant area";
    const original = missing === "device" ? deviceA : "roomA";
    const replacement = missing === "device" ? deviceB : "roomB";
    const devices = server.snapshot.devices, areas = server.areas;
    component._activeTab = 4; component._render();
    if (missing === "device") server.snapshot.devices = { [deviceB]: devices[deviceB] };
    else server.areas = [areas[1]];
    await component.refresh();
    component._activeTab = 3; component._render();
    assert.match(field(component, label).textContent, /selection became unavailable/);
    assert.notEqual(field(component, label).value, replacement);
    assert.equal(button(component, "Train labelled sample").disabled, true);
    assert.equal(button(component, "Clear device fingerprints").disabled, missing === "device");
    button(component, "Train labelled sample").disabled = false;
    await button(component, "Train labelled sample").click();
    assert.equal(calls.length, 0);
    server.snapshot.devices = devices; server.areas = areas;
    await component.refresh();
    assert.equal(button(component, "Train labelled sample").disabled, true, "reappearance does not confirm independent truth");
    await edit(component, label, original, "change");
    await button(component, "Train labelled sample").click();
    assert.equal(calls[0].device_id, deviceA);
    assert.equal(calls[0].location_id, "roomA");
    await edit(component, label, replacement, "change");
    await button(component, "Train labelled sample").click();
    assert.equal(calls[1].device_id, missing === "device" ? deviceB : deviceA);
    assert.equal(calls[1].location_id, missing === "area" ? "roomB" : "roomA");
  });
}

test("a missing history device cannot silently widen recording to all devices", async () => {
  const { server, deviceA, deviceB } = twoTargetState();
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "POST") calls.push(payload);
    return structuredClone(server);
  });
  component._state = structuredClone(server); component._activeTab = 2; component._render();
  await edit(component, "Record device (or all tracked devices)", deviceA, "change");
  const devices = server.snapshot.devices; server.snapshot.devices = {};
  component._activeTab = 4; component._render(); await component.refresh();
  component._activeTab = 2; component._render();
  assert.match(field(component, "Record device (or all tracked devices)").textContent, /selection became unavailable/);
  assert.notEqual(field(component, "Record device (or all tracked devices)").value, "");
  assert.equal(button(component, "Start recording").disabled, true);
  button(component, "Start recording").disabled = false; await button(component, "Start recording").click();
  assert.equal(calls.length, 0);
  server.snapshot.devices = devices; await component.refresh();
  assert.equal(button(component, "Start recording").disabled, true);
  await edit(component, "Record device (or all tracked devices)", deviceB, "change");
  await button(component, "Start recording").click();
  assert.deepEqual(calls[0].devices, [deviceB]);
  await edit(component, "Record device (or all tracked devices)", "", "change");
  await button(component, "Start recording").click();
  assert.deepEqual(calls[1].devices, []);
});

for (const tab of [1, 2, 3]) {
  test(`tab ${tab} keeps present targets on reorder and ignores detached mutation controls`, async () => {
    const { server, deviceA, deviceB } = twoTargetState();
    server.areas = [{ area_id: "roomA", name: "Room A" }, { area_id: "roomB", name: "Room B" }];
    const calls = [];
    const component = panel(async (method, path, payload) => {
      if (method === "POST") calls.push(payload);
      return structuredClone(server);
    });
    component._state = structuredClone(server); component._activeTab = tab; component._render();
    const selector = tab === 2 ? "Record device (or all tracked devices)" : "Device";
    if (tab === 2) await edit(component, selector, deviceA, "change");
    const action = tab === 1 ? "Capture labelled readings" : tab === 2 ? "Start recording" : "Train labelled sample";
    const detached = button(component, action);
    server.snapshot.devices = { [deviceB]: server.snapshot.devices[deviceB], [deviceA]: server.snapshot.devices[deviceA] };
    server.areas.reverse(); await component.refresh();
    assert.equal(field(component, selector).value, deviceA);
    if (tab === 3) assert.equal(field(component, "Independently known Home Assistant area").value, "roomA");
    await edit(component, selector, deviceB, "change");
    await detached.click();
    assert.equal(calls.length, 0, "detached buttons do not mutate a prior target");
  });
}

test("changing calibration devices clears truth for receivers missing from the current roster", async () => {
  const { server, receiverA, deviceB } = twoTargetState();
  const component = panel(async () => structuredClone(server));
  component._state = structuredClone(server); component._activeTab = 1; component._render();
  const rangeLabel = `Measured slant range to Synthetic receiver (${receiverA}) (m)`;
  await edit(component, "Independent location label", "Measured prior device");
  await edit(component, rangeLabel, "2.25");
  const receiver = server.snapshot.scanners[receiverA]; delete server.snapshot.scanners[receiverA];
  await component.refresh();
  await edit(component, "Device", deviceB, "change");
  server.snapshot.scanners[receiverA] = receiver; await component.refresh();
  assert.equal(field(component, "Independent location label").value, "");
  assert.equal(field(component, rangeLabel).value, "");
});

test("single-target map patches preserve concurrent edits to other receivers and public devices", async () => {
  const { server, receiverA, receiverB, deviceA, deviceB } = twoTargetState();
  const calls = [];
  const component = panel(async (method, path, payload) => {
    if (method === "GET") return structuredClone(server);
    calls.push(payload); return applySettingRequest(server, payload);
  });
  component._state = structuredClone(server); component._render();
  server.options.receiver_limits[receiverB] = 17;
  await edit(component, "Maximum distance (m)", "10");
  await button(component, "Save receiver limit").click();
  assert.deepEqual(calls[0].settings, { receiver_limits: { [receiverA]: 10 } });
  assert.equal(calls[0].patch_maps, true);
  assert.deepEqual(server.options.receiver_limits, { [receiverA]: 10, [receiverB]: 17 });
  assert.equal(component._state.options.receiver_limits[receiverB], 17);
  server.options.filters[deviceB] = "stationary";
  await edit(component, "Filter", "pet", "change");
  await button(component, "Save filter mode").click();
  assert.deepEqual(calls[1].settings, { filters: { [deviceA]: "pet" } });
  assert.equal(calls[1].patch_maps, true);
  assert.deepEqual(server.options.filters, { [deviceA]: "pet", [deviceB]: "stationary" });
  assert.equal(component._state.options.filters[deviceB], "stationary");
  assert.deepEqual(Object.keys(calls[1].settings.filters), [deviceA]);
});

test("map acknowledgements preserve newer target drafts and failed saves keep their revisions", async () => {
  const { server, receiverA, receiverB, deviceA, deviceB } = twoTargetState();
  const pending = deferred(); const calls = [];
  let failRefresh = false;
  const component = panel(async (method, path, payload) => {
    if (method === "GET") { if (failRefresh) throw new Error("Synthetic refresh failure"); return structuredClone(server); }
    calls.push(payload); await pending.promise; return applySettingRequest(server, payload);
  });
  component._state = structuredClone(server); component._render();
  await edit(component, "Maximum distance (m)", "10");
  const saving = button(component, "Save receiver limit").click();
  await edit(component, "Receiver", receiverB, "change");
  await edit(component, "Maximum distance (m)", "18");
  server.options.receiver_limits[receiverB] = 17;
  failRefresh = true; pending.resolve(); await saving;
  assert.deepEqual(calls[0].settings, { receiver_limits: { [receiverA]: 10 } });
  assert.equal(component._state.options.receiver_limits[receiverB], 17);
  assert.equal(field(component, "Maximum distance (m)").value, "18");
  assert.equal(component._drafts.get("limit"), "18");
  const rejected = deferred();
  component._hass.callApi = async method => method === "GET" ? structuredClone(server) : rejected.promise;
  await edit(component, "Filter", "pet", "change");
  const failedSave = button(component, "Save filter mode").click();
  await edit(component, "Filter", "stationary", "change");
  server.options.filters[deviceB] = "stationary";
  rejected.reject(new Error("Synthetic map save failure")); await failedSave;
  await component.refresh();
  assert.equal(server.options.filters[deviceA], "legacy");
  assert.equal(field(component, "Filter").value, "stationary");
  assert.equal(component._drafts.get("filter-mode"), "stationary");
  assert.equal(component._state.options.filters[deviceB], "stationary");
});

test("receiver lookup uses own exact keys and only unambiguous MAC case aliases", () => {
  const mac = "aa:bb:cc:dd:ee:12", upper = mac.toUpperCase(), mixed = "Aa:bB:cC:dD:eE:12";
  assert.equal(receiverValue({ [upper]: 7 }, mac, 0), 7);
  assert.equal(receiverValue({ [upper]: 0 }, mac, 9), 0);
  assert.equal(receiverValue({ [upper]: 7, [mixed]: 8 }, mac, 0), 0);
  assert.equal(receiverValue({ [mac]: 6, [upper]: 7, [mixed]: 8 }, mac, 0), 6);
  assert.equal(receiverValue(Object.create({ [mac]: 7, [upper]: 8 }), mac, 0), 0);
  assert.equal(receiverValue({ Receiver_A: 7 }, "receiver_a", 0), 0);
  assert.equal(receiverValue({ Receiver_A: 7 }, "Receiver_A", 0), 7);
  assert.equal(receiverValue({}, "constructor", 0), 0);
  assert.equal(receiverValue(JSON.parse('{"__proto__":7}'), "__proto__", 0), 7);
});

test("uppercase receiver profiles and limits populate initial controls, resets, and explicit zero patches", async () => {
  const { server, receiverA, receiverB } = twoTargetState();
  const upperA = receiverA.toUpperCase(), upperB = receiverB.toUpperCase();
  server.options.receiver_profiles = {
    [upperA]: { rssi_offset: 5, reference_power: -61, model: "Upper A" },
    [upperB]: { rssi_offset: 6, reference_power: -62, model: "Upper B" },
  };
  server.options.receiver_limits = { [upperA]: 7, [upperB]: 9 };
  const calls = [];
  const component = panel(async (method, path, payload) => { if (method === "POST") calls.push(payload); return structuredClone(server); });
  component._state = structuredClone(server); component._render();
  assert.equal(field(component, "Maximum distance (m)").value, "7");
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "5");
  assert.equal(field(component, "Reference power at 1 m (dBm)").value, "-61");
  await edit(component, "Receiver", receiverB, "change");
  assert.equal(field(component, "Maximum distance (m)").value, "9");
  await edit(component, "Profile target", receiverB, "change");
  assert.equal(field(component, "Receiver RSSI offset (dB)").value, "6");
  assert.equal(field(component, "Reference power at 1 m (dBm)").value, "-62");
  assert.equal(field(component, "model").value, "Upper B");
  await edit(component, "Receiver", receiverA, "change");
  await edit(component, "Maximum distance (m)", "0");
  await button(component, "Save receiver limit").click();
  assert.deepEqual(calls[0].settings, { receiver_limits: { [receiverA]: 0 } });
  assert.equal(calls[0].patch_maps, true);
});

for (const control of [
  { label: "model", initial: "original", submitted: "submitted metadata", draft: "profile-model", button: "Save profile" },
  { label: "Enable automatic calibration", initial: false, submitted: true, draft: "auto", button: "Save runtime settings" },
  { label: "History window (seconds)", initial: "3600", submitted: "1800", draft: "window", button: "Save runtime settings" },
]) {
  for (const outcome of ["success", "failed refresh", "failed POST"]) {
    test(`newer ${control.label} revert survives a pending save with ${outcome}`, async () => {
      const pending = deferred(); const server = state();
      server.options.receiver_profiles = { receiver1: { model: "original" } };
      server.options.history_window_s = 3600;
      let postSeen = false;
      const component = panel(async (method, path, payload) => {
        if (method === "GET") {
          if (outcome === "failed refresh" && postSeen) throw new Error("Synthetic refresh failure");
          return structuredClone(server);
        }
        postSeen = true; await pending.promise;
        if (payload.action === "profile") {
          server.options.receiver_profiles.receiver1 = { ...server.options.receiver_profiles.receiver1, ...payload.profile };
          return { profile: structuredClone(server.options.receiver_profiles.receiver1) };
        }
        return applySettingRequest(server, payload);
      });
      component._state = structuredClone(server); component._render();
      await edit(component, control.label, control.submitted);
      const saving = button(component, control.button).click();
      await edit(component, control.label, control.initial);
      if (outcome === "failed POST") pending.reject(new Error("Synthetic POST failure"));
      else pending.resolve();
      await saving;
      const input = field(component, control.label);
      assert.equal(input.type === "checkbox" ? input.checked : input.value, control.initial);
      assert.equal(component._drafts.get(control.draft), control.initial);
      assert.equal(component._busy, false);
      if (outcome === "success") assert.match(component._message, /Newer edits remain unsaved/);
      component._activeTab = 4; component._render(); component._activeTab = 0; component._render();
      assert.equal(component._drafts.get(control.draft), control.initial);
    });
  }
}

test("new entry reverts remain scoped to the new entry after an old acknowledgement", async () => {
  const pending = deferred(); const server = state(); server.options.receiver_profiles = { receiver1: { model: "original" } };
  const component = panel(async method => method === "GET" ? structuredClone(server) : pending.promise);
  component._state = structuredClone(server); component._render();
  await edit(component, "model", "old entry submission");
  const saving = button(component, "Save profile").click();
  component.panel = { config: { entry_id: "new-entry" } }; await component.refresh();
  await edit(component, "model", "new entry temporary edit"); await edit(component, "model", "original");
  pending.resolve({ profile: { model: "old entry acknowledgement" } }); await saving;
  assert.equal(field(component, "model").value, "original");
  assert.equal(component._drafts.get("profile-model"), "original");
  assert.equal(component._entryId, "new-entry");
});
