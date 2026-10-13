# Measurement toolkit panel

Home Assistant administrators can open **Bermuda toolkit** in the sidebar after Bermuda loads. The panel reads the selected integration entry and uses Home Assistant authentication for every request. Refresh updates the source packet ages; it does not make old readings fresh.

The **Settings** tab contains optional automatic calibration, reliability weighting, fingerprint area selection, device filter modes, and receiver distance limits. A receiver limit of `0` follows the existing global limit. Legacy filter mode keeps normal Bermuda behavior. Saving one receiver limit or filter patches that target only, preserving other targets' latest settings. Hardware and antenna details are descriptive metadata; only explicitly entered radio model parameters change range calculations. Clearing a beacon's reference-power override in the panel also clears its verification.

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

**Health** shows receiver freshness and current observations, and loads or copies full diagnostics including quality and rejection reasons. Raw RSSI and raw distance remain distinct from filtered readings. Private key fields are excluded from toolkit responses.
