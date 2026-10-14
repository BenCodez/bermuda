# Measurement toolkit panel

Home Assistant administrators can open `/bermuda-toolkit` on their Home Assistant server after Bermuda loads. The page has no sidebar item. The panel reads the selected integration entry and uses Home Assistant authentication for every request. Refresh updates the source packet ages; it does not make old readings fresh.

Use **Settings → Devices & services → Bermuda BLE Trilateration → Configure** for Bermuda's original controls. **Global Options** sets the shared distance model, area radius, velocity limit, presence timeout, update interval and smoothing. **Select Devices** chooses the Bluetooth devices and beacons to track. **Calibration 1: Global** previews the shared distance model, and **Calibration 2: Scanner RSSI Offsets** adjusts the existing per-scanner corrections. The toolkit keeps these controls available in the native configuration dialog.

The **Settings** tab starts with the radio profile: receiver RSSI offset, reference power, path-loss exponent and antenna placement. **Hardware details** contains descriptive fields. **Advanced** contains optional automatic calibration, reliability weighting, fingerprint area selection, history-window tuning, device filter modes, receiver distance limits, fixed references, scanner bindings and identity recovery. Both sections start collapsed; opening or closing them changes no saved settings. A receiver limit of `0` follows the existing global limit. Legacy filter mode keeps normal Bermuda behavior. Saving one receiver limit or filter patches that target only, preserving other targets' latest settings. Hardware and antenna details are descriptive metadata; only explicitly entered radio model parameters change range calculations. Clearing a beacon's reference-power override in the panel also clears its verification.

**Hardware and radio profile** fills the controls with saved profile values, detected manufacturer/model/hardware type, or the effective receiver radio settings. Each populated field identifies its source. Only fields you edit are saved: changing an antenna setting or description leaves inherited numeric values inherited. Physical antenna placement cannot be autodetected: choose **Unknown**, **Internal** or **External**. Unknown removes the optional `external_antenna` setting; Internal saves `false`, External saves `true`. No antenna choice changes ranging or radio corrections.

The receiver RSSI offset control and current radio-model summary show inherited **Calibration 2** offsets alongside effective reference power and attenuation. Existing settings stay saved where they were; displaying them creates no profile override. Edit the receiver RSSI offset to override it, or clear and save that field to restore inheritance. After saving a cleared field, its current inherited value reappears with the source label.

To restore an inherited profile model, clear the corresponding numeric field and save the profile. Empty metadata fields are removed too. The API accepts partial profile changes and an explicit removal list:

```json
{
  "action": "profile",
  "entry_id": "selected-entry-id",
  "kind": "receiver",
  "id": "selected-scanner-id",
  "profile": {"model": "Synthetic receiver"},
  "remove": ["attenuation", "reference_power"]
}
```

Use **Calibration** to capture measured slant ranges in metres from a beacon to each receiver, including their height difference. Give each independently known location its own label, and capture multiple locations and devices. Leave ranges you have not measured blank. Preview shows coverage, held-out errors and rejection reasons. Apply becomes available for an eligible proposal; the server checks eligibility again. Rollback restores the saved calibration baseline. A range error score does not establish position accuracy.

Fixed reference JSON under **Settings** is an explicit, persistent label for future samples. Only use it for beacons that remain at their measured position. An instant calibration capture labels that capture alone. Scanner bindings explicitly map Bluetooth source IDs to binding objects such as `{"scanner-source":{"device_registry_id":"receiver-registry-id"}}`; matching a friendly name does not establish receiver identity.

**History & replay** starts and stops bounded recording, clears history, imports recordings, and exports or copies JSON. Identifier redaction is selected by default for sharing. Imports are limited to 2 MiB and validated by the server. Replay compares frozen measurements against the current or entered candidate receiver offsets. Empty candidate JSON uses the current eligible proposal when available. Shadow observes a candidate before promotion; unavailable metrics remain unavailable when independent truth is missing.

**Fingerprints** trains the selected device in an independently known Home Assistant area chosen from the selector. Train several positions and times, waiting for new receiver packets after changing areas or clearing templates. Cached packets cannot count toward more than one room label for the same device. Results show missing coverage, ambiguity and unknown outcomes. Live area selection remains optional and requires a valid, confident result.

**Health** shows receiver hardware, antenna placement, hardware source, freshness and current observations, and loads or copies full diagnostics including quality and rejection reasons. Missing hardware information is shown as unknown. Raw RSSI and raw distance remain distinct from filtered readings. Private key fields are excluded from toolkit responses.
