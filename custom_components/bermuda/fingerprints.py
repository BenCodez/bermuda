"""
Bounded, explicitly supervised per-device RSSI area suggestions.

``train`` accepts one snapshot's shared observation rows, or a list of such
snapshots, with an independently asserted user location label. Repeated source
stamps cannot manufacture training samples. ``suggest`` only reads the model;
it never learns from a predicted area and never changes Home Assistant areas.
IDs are opaque stable keys, including IRK and iBeacon identities.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Any

MIN_RECEIVERS = 2
MIN_SAMPLES = 5
MAX_RECEIVERS = 32
MAX_SNAPSHOTS_PER_CALL = 128
MAX_INPUT_ROWS = MAX_RECEIVERS * MAX_SNAPSHOTS_PER_CALL
MAX_LABELS = 128
MAX_DEVICES = 64
MAX_LABELS_PER_DEVICE = 32
MAX_FRAMES_PER_LABEL = 64
MAX_TOTAL_FRAMES = 1024
MAX_STATE_BYTES = 2_000_000
MAX_ID_LENGTH = 256
MAX_AGE_S = 15.0
MIN_TRAINING_SPAN_S = 4.0
MIN_COVERAGE = 0.75
NOISE_FLOOR_DB = 4.0
MAX_TRAINING_NOISE_DB = 12.0
MAX_MATCH_SCORE = 2.5
MIN_MARGIN = 0.5
MIN_CONFIDENCE = 0.75


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and low <= numeric <= high else None


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH and not any(ord(char) < 32 for char in value)


def _reason(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


@dataclass(slots=True)
class _Frame:
    values: dict[str, float]
    stamps: dict[str, float]
    observed: dict[str, float]
    model_version: str
    truth_source: str


class FingerprintModel:
    """Supervised RSSI templates with coverage, unknown and ambiguity rejection."""

    def __init__(self) -> None:
        self._labels: dict[tuple[str, str], list[_Frame]] = {}

    def _parse_rows(
        self,
        device_id: str,
        rows: object,
        now: float,
        truth_source: str,
    ) -> tuple[_Frame | None, list[dict[str, Any]]]:
        if not isinstance(rows, list | tuple) or len(rows) > MAX_RECEIVERS:
            return None, [_reason("receiver_input_budget")]
        values: dict[str, float] = {}
        stamps: dict[str, float] = {}
        observed: dict[str, float] = {}
        versions: set[str] = set()
        reasons: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("device_id") != device_id:
                return None, [_reason("wrong_device")]
            scanner = row.get("scanner_id")
            rssi = _number(row.get("rssi"), -150.0, 20.0)
            stamp = _number(row.get("stamp"), 0.0, 1e12)
            epoch = _number(row.get("observed"), 0.0, 1e12)
            version = row.get("model_version", "")
            if (
                not _identifier(scanner)
                or rssi is None
                or stamp is None
                or epoch is None
                or not isinstance(version, str)
                or len(version) > MAX_ID_LENGTH
                or scanner in values
            ):
                return None, [_reason("invalid_observation")]
            supplied_age = row.get("age")
            age = _number(supplied_age, 0.0, 1e12) if supplied_age is not None else max(0.0, now - epoch)
            if row.get("status", "current") != "current" or age is None or age > MAX_AGE_S:
                reasons.append(_reason("stale_receiver", scanner_id=scanner))
                continue
            if epoch > now + 0.05 or now - epoch > MAX_AGE_S:
                reasons.append(_reason("stale_or_future_receiver", scanner_id=scanner))
                continue
            values[scanner] = rssi
            stamps[scanner] = stamp
            observed[scanner] = epoch
            versions.add(version)
        if len(values) < MIN_RECEIVERS:
            return None, [*reasons, _reason("insufficient_receiver_coverage", receivers=len(values))]
        if len(versions) != 1:
            return None, [_reason("mixed_model_versions")]
        return _Frame(values, stamps, observed, versions.pop(), truth_source), reasons

    def train(  # noqa: PLR0911 - independent validation/budget guards prevent any partial model mutation.
        self,
        device_id: str,
        location_id: str,
        observations: object,
        *,
        independent: bool,
        truth_source: str = "measured",
        now: float,
    ) -> dict[str, Any]:
        """
        Accumulate explicit labelled snapshots; readiness requires fresh coverage.

        A flat row list is one captured snapshot. A nested list represents
        explicit snapshot boundaries, not timestamp rounding. Each accepted
        snapshot must contain at least two newly observed receiver stamps.
        Every call requires deliberate independent truth; no runtime label is
        accepted implicitly. An instant label only applies to these inputs.
        """
        if not _identifier(device_id) or not _identifier(location_id):
            return {"accepted": False, "ready": False, "reasons": [_reason("invalid_identifier")]}
        if (
            independent is not True
            or not isinstance(truth_source, str)
            or truth_source not in {"measured", "fixed_reference"}
        ):
            return {"accepted": False, "ready": False, "reasons": [_reason("independent_label_required")]}
        epoch = _number(now, 0.0, 1e12)
        if epoch is None:
            return {"accepted": False, "ready": False, "reasons": [_reason("invalid_current_clock")]}
        batches, error = self._batches(observations)
        if error:
            return {"accepted": False, "ready": False, "reasons": [error]}
        key = (device_id, location_id)
        existing = self._labels.get(key, [])
        budget_reason = self._new_label_budget(key)
        if budget_reason:
            return {"accepted": False, "ready": False, "reasons": [budget_reason]}
        staged = list(existing)
        latest_stamps: dict[str, float] = {}
        for frame in existing:
            for scanner, stamp in frame.stamps.items():
                latest_stamps[scanner] = max(stamp, latest_stamps.get(scanner, -1.0))
        added = 0
        reasons: list[dict[str, Any]] = []
        for rows in batches:
            frame, frame_reasons = self._parse_rows(device_id, rows, epoch, truth_source)
            reasons.extend(frame_reasons)
            if frame is None:
                continue
            if staged and frame.model_version != staged[0].model_version:
                reasons.append(_reason("model_version_mismatch"))
                continue
            # Store only genuinely newer receivers. Reusing the other receivers'
            # old packets must not falsely increase their coverage/sample count.
            fresh = {scanner for scanner, stamp in frame.stamps.items() if stamp > latest_stamps.get(scanner, -1.0)}
            if len(fresh) < MIN_RECEIVERS:
                reasons.append(_reason("insufficient_fresh_receivers", fresh_receivers=len(fresh)))
                continue
            frame.values = {scanner: value for scanner, value in frame.values.items() if scanner in fresh}
            frame.stamps = {scanner: value for scanner, value in frame.stamps.items() if scanner in fresh}
            frame.observed = {scanner: value for scanner, value in frame.observed.items() if scanner in fresh}
            staged.append(frame)
            latest_stamps.update(frame.stamps)
            added += 1
        staged = staged[-MAX_FRAMES_PER_LABEL:]
        total = sum(len(frames) for frames in self._labels.values()) - len(existing) + len(staged)
        if total > MAX_TOTAL_FRAMES:
            return {"accepted": False, "ready": False, "reasons": [_reason("model_frame_budget")]}
        if added:
            proposed = {**self._labels, key: staged}
            if len(json.dumps(self._export_labels(proposed), allow_nan=False).encode()) > MAX_STATE_BYTES:
                return {"accepted": False, "ready": False, "reasons": [_reason("model_byte_budget")]}
            self._labels = proposed
        template, ready_reasons = self._template(staged)
        return {
            "accepted": added > 0,
            "ready": template is not None,
            "device_id": device_id,
            "location_id": location_id,
            "added_samples": added,
            "samples": len(staged),
            "receivers": len(template[0]) if template else 0,
            "reasons": reasons + ready_reasons,
        }

    @staticmethod
    def _batches(observations: object) -> tuple[list[Any], dict[str, Any] | None]:
        if not isinstance(observations, list | tuple):
            return [], _reason("invalid_observation_list")
        if observations and isinstance(observations[0], list | tuple):
            if len(observations) > MAX_SNAPSHOTS_PER_CALL or any(
                not isinstance(rows, list | tuple) for rows in observations
            ):
                return [], _reason("invalid_observation_list")
            if sum(len(rows) for rows in observations) > MAX_INPUT_ROWS:
                return [], _reason("observation_input_budget")
            return list(observations), None
        if len(observations) > MAX_RECEIVERS:
            return [], _reason("receiver_input_budget")
        return [observations], None

    def _new_label_budget(self, key: tuple[str, str]) -> dict[str, Any] | None:
        if key in self._labels:
            return None
        if len(self._labels) >= MAX_LABELS:
            return _reason("model_label_budget")
        devices = {device for device, _location in self._labels}
        if key[0] not in devices and len(devices) >= MAX_DEVICES:
            return _reason("model_device_budget")
        if sum(device == key[0] for device, _location in self._labels) >= MAX_LABELS_PER_DEVICE:
            return _reason("device_label_budget")
        return None

    @staticmethod
    def _template(
        frames: list[_Frame],
    ) -> tuple[tuple[dict[str, float], dict[str, float]] | None, list[dict[str, Any]]]:
        if len(frames) < MIN_SAMPLES:
            return None, [_reason("insufficient_training_samples", samples=len(frames), required=MIN_SAMPLES)]
        times = [min(frame.observed.values()) for frame in frames]
        if max(times) - min(times) < MIN_TRAINING_SPAN_S:
            return None, [_reason("insufficient_training_span", required_s=MIN_TRAINING_SPAN_S)]
        series: dict[str, list[float]] = {}
        for frame in frames:
            for scanner, rssi in frame.values.items():
                series.setdefault(scanner, []).append(rssi)
        required = max(MIN_SAMPLES, math.ceil(len(frames) * MIN_COVERAGE))
        centers: dict[str, float] = {}
        noise: dict[str, float] = {}
        for scanner, values in series.items():
            if len(values) < required:
                continue
            center = statistics.median(values)
            sigma = 1.4826 * statistics.median(abs(value - center) for value in values)
            if sigma > MAX_TRAINING_NOISE_DB:
                continue
            centers[scanner] = center
            noise[scanner] = max(NOISE_FLOOR_DB, sigma)
        if len(centers) < MIN_RECEIVERS:
            return None, [_reason("insufficient_stable_training_coverage", receivers=len(centers))]
        return (centers, noise), []

    def suggest(self, device_id: str, observations: object, *, now: float) -> dict[str, Any]:
        """Return a suggestion independently of live areas, with explicit rejection reasons."""
        result: dict[str, Any] = {
            "device_id": device_id if _identifier(device_id) else None,
            "suggested_area": None,
            "location_id": None,
            "accepted": False,
            "confidence": 0.0,
            "margin": None,
            "coverage": 0.0,
            "scores": [],
            "reasons": [],
        }
        epoch = _number(now, 0.0, 1e12)
        if not _identifier(device_id) or epoch is None:
            result["reasons"] = [_reason("invalid_identifier_or_clock")]
            return result
        live, reasons = self._parse_rows(device_id, observations, epoch, "measured")
        result["reasons"] = reasons
        if live is None:
            return result
        candidates: list[dict[str, Any]] = []
        model_reasons: list[dict[str, Any]] = []
        for (device, location), frames in self._labels.items():
            if device != device_id:
                continue
            template, rejection = self._template(frames)
            if template is None:
                model_reasons.extend([_reason(reason["code"], location_id=location) for reason in rejection])
                continue
            if live.model_version != frames[0].model_version:
                model_reasons.append(_reason("model_version_mismatch", location_id=location))
                continue
            centers, noise = template
            common = live.values.keys() & centers.keys()
            coverage = len(common) / len(centers)
            if len(common) < MIN_RECEIVERS or coverage < MIN_COVERAGE:
                model_reasons.append(_reason("insufficient_matching_coverage", location_id=location, coverage=coverage))
                continue
            missing = centers.keys() - live.values.keys()
            extra = live.values.keys() - centers.keys()
            squared = sum(((live.values[scanner] - centers[scanner]) / noise[scanner]) ** 2 for scanner in common)
            squared += 9.0 * len(missing) + 4.0 * len(extra)
            score = math.sqrt(squared / len(live.values.keys() | centers.keys()))
            candidates.append(
                {
                    "location_id": location,
                    "score": score,
                    "coverage": coverage,
                    "missing_receivers": len(missing),
                    "extra_receivers": len(extra),
                    "samples": len(frames),
                }
            )
        candidates.sort(key=lambda item: (item["score"], item["location_id"]))
        result["scores"] = candidates
        if not candidates:
            result["reasons"] += model_reasons or [_reason("no_trained_device_model")]
            return result
        best = candidates[0]
        score = best["score"]
        margin = candidates[1]["score"] - score if len(candidates) > 1 else None
        # Broad/noisy templates cannot create confidence by inflating variance;
        # the score has a fixed RSSI floor and missing/extra coverage penalties.
        confidence = math.exp(-(score**2) / 8.0) * best["coverage"]
        confidence *= 1.0 / (1.0 + best["extra_receivers"] / len(live.values))
        if margin is not None:
            confidence *= min(1.0, max(0.0, margin) / 1.5)
        else:
            confidence = min(confidence, 0.85)
        result.update({"confidence": confidence, "margin": margin, "coverage": best["coverage"]})
        if score > MAX_MATCH_SCORE:
            result["reasons"].append(_reason("unknown_pattern", score=score))
        elif margin is not None and margin < MIN_MARGIN:
            result["reasons"].append(_reason("ambiguous_locations", margin=margin))
        elif confidence < MIN_CONFIDENCE:
            result["reasons"].append(_reason("low_confidence", confidence=confidence))
        else:
            result.update({"accepted": True, "suggested_area": best["location_id"], "location_id": best["location_id"]})
        return result

    def clear(self, device_id: str | None = None, location_id: str | None = None) -> int:
        """Remove only explicitly selected model keys; no friendly-name inference."""
        if (device_id is not None and not _identifier(device_id)) or (
            location_id is not None and not _identifier(location_id)
        ):
            msg = "Invalid fingerprint identifier"
            raise ValueError(msg)
        selected = [
            key
            for key in self._labels
            if (device_id is None or key[0] == device_id) and (location_id is None or key[1] == location_id)
        ]
        for key in selected:
            del self._labels[key]
        return len(selected)

    def export_state(self) -> dict[str, Any]:
        """Export detached bounded primitive state with explicit label provenance."""
        return self._export_labels(self._labels)

    @staticmethod
    def _export_labels(labels: dict[tuple[str, str], list[_Frame]]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "labels": [
                {
                    "device_id": device,
                    "location_id": location,
                    "frames": [
                        {
                            "values": dict(frame.values),
                            "stamps": dict(frame.stamps),
                            "observed": dict(frame.observed),
                            "model_version": frame.model_version,
                            "truth_source": frame.truth_source,
                            "independent": True,
                        }
                        for frame in frames
                    ],
                }
                for (device, location), frames in sorted(labels.items())
            ],
        }

    def summary(self) -> list[dict[str, Any]]:
        """List readiness/coverage without exposing all stored radio samples."""
        result: list[dict[str, Any]] = []
        for (device, location), frames in sorted(self._labels.items()):
            template, reasons = self._template(frames)
            result.append(
                {
                    "device_id": device,
                    "location_id": location,
                    "samples": len(frames),
                    "receivers": len(template[0]) if template else 0,
                    "ready": template is not None,
                    "reasons": reasons,
                }
            )
        return result

    def import_state(self, state: object) -> dict[str, Any]:
        """Strictly validate bounded exported state before atomically replacing it."""
        try:
            restored = self._restore(state)
        except (ValueError, TypeError, OverflowError, RecursionError) as err:
            return {"accepted": False, "reasons": [_reason("invalid_model_state", detail=str(err)[:120])]}
        self._labels = restored
        return {"accepted": True, "labels": len(restored), "reasons": []}

    @staticmethod
    def _restore(state: object) -> dict[tuple[str, str], list[_Frame]]:
        if not isinstance(state, dict) or len(state) != 2 or set(state) != {"schema_version", "labels"}:
            msg = "Invalid state schema"
            raise ValueError(msg)
        if type(state["schema_version"]) is not int or state["schema_version"] != 1:
            msg = "Unsupported state version"
            raise ValueError(msg)
        labels = state["labels"]
        if not isinstance(labels, list) or len(labels) > MAX_LABELS:
            msg = "Label budget exceeded"
            raise ValueError(msg)
        restored: dict[tuple[str, str], list[_Frame]] = {}
        device_counts: dict[str, int] = {}
        total = 0
        for label in labels:
            if not isinstance(label, dict) or len(label) != 3 or set(label) != {"device_id", "location_id", "frames"}:
                msg = "Invalid label schema"
                raise ValueError(msg)
            device, location, frames = label["device_id"], label["location_id"], label["frames"]
            if not _identifier(device) or not _identifier(location) or (device, location) in restored:
                msg = "Invalid or duplicate label"
                raise ValueError(msg)
            if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES_PER_LABEL:
                msg = "Frame budget exceeded"
                raise ValueError(msg)
            total += len(frames)
            device_counts[device] = device_counts.get(device, 0) + 1
            if total > MAX_TOTAL_FRAMES or len(device_counts) > MAX_DEVICES:
                msg = "Model budget exceeded"
                raise ValueError(msg)
            if device_counts[device] > MAX_LABELS_PER_DEVICE:
                msg = "Device label budget exceeded"
                raise ValueError(msg)
            restored[(device, location)] = FingerprintModel._restore_frames(frames)
        # Check bytes only after structural counts bound the work/memory. Import
        # never serializes arbitrary caller-defined keys or unlimited collections.
        if len(json.dumps(state, allow_nan=False).encode()) > MAX_STATE_BYTES:
            msg = "Model byte budget exceeded"
            raise ValueError(msg)
        return restored

    @staticmethod
    def _restore_frames(frames: list[dict[str, Any]]) -> list[_Frame]:
        restored: list[_Frame] = []
        latest: dict[str, float] = {}
        version: str | None = None
        for frame in frames:
            expected = {"values", "stamps", "observed", "model_version", "truth_source", "independent"}
            if (
                not isinstance(frame, dict)
                or len(frame) != len(expected)
                or set(frame) != expected
                or frame["independent"] is not True
            ):
                msg = "Explicit frame provenance required"
                raise ValueError(msg)
            if frame["truth_source"] not in {"measured", "fixed_reference"}:
                msg = "Independent truth source required"
                raise ValueError(msg)
            model = frame["model_version"]
            if not isinstance(model, str) or len(model) > MAX_ID_LENGTH or (version is not None and model != version):
                msg = "Invalid or mixed model versions"
                raise ValueError(msg)
            version = model
            values, stamps, observed = frame["values"], frame["stamps"], frame["observed"]
            if not all(isinstance(part, dict) for part in (values, stamps, observed)):
                msg = "Invalid receiver maps"
                raise ValueError(msg)
            if not MIN_RECEIVERS <= len(values) <= MAX_RECEIVERS or values.keys() != stamps.keys():
                msg = "Invalid receiver coverage"
                raise ValueError(msg)
            if values.keys() != observed.keys():
                msg = "Missing actual observation timestamps"
                raise ValueError(msg)
            clean_values: dict[str, float] = {}
            clean_stamps: dict[str, float] = {}
            clean_observed: dict[str, float] = {}
            for scanner, value in values.items():
                rssi = _number(value, -150.0, 20.0)
                stamp = _number(stamps[scanner], 0.0, 1e12)
                epoch = _number(observed[scanner], 0.0, 1e12)
                if not _identifier(scanner) or rssi is None or stamp is None or epoch is None:
                    msg = "Invalid receiver value"
                    raise ValueError(msg)
                if stamp <= latest.get(scanner, -1.0):
                    msg = "Duplicate or regressing source stamps"
                    raise ValueError(msg)
                clean_values[scanner], clean_stamps[scanner], clean_observed[scanner] = rssi, stamp, epoch
            latest.update(clean_stamps)
            restored.append(_Frame(clean_values, clean_stamps, clean_observed, model, frame["truth_source"]))
        return restored
