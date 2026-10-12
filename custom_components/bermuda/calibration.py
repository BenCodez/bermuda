"""
Independent-label receiver calibration with held-out range validation.

The engine never reads a predicted area or advertised TX power. It fits receiver
RSSI offsets with the frozen observation's reference power and path-loss exponent.
Unknown transmitter power permits relative receiver offsets only: a zero-mean
delta gauge prevents a shared transmitter error becoming a global receiver shift.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import mean, median

from .profiles import finite_number, receiver_value


@dataclass(frozen=True)
class CalibrationPolicy:
    """Bounded evidence, validation, and automatic application policy."""

    max_age_s: float = 30.0
    max_capture_age_s: float = 86400.0
    min_rssi: float = -95.0
    min_training_samples: int = 3
    min_holdout_samples: int = 2
    min_relative_devices: int = 3
    max_target_disagreement_db: float = 2.5
    max_scatter_db: float = 4.0
    max_offset_db: float = 20.0
    max_auto_step_db: float = 1.0
    cooldown_s: float = 900.0
    minimum_improvement_m: float = 0.02
    minimum_improvement_fraction: float = 0.02
    regression_tolerance_m: float = 0.02
    minimum_reliability: float = 0.2
    max_observations: int = 20000

    def __post_init__(self) -> None:
        """Reject malformed or unsafe policy overrides before fitting."""
        numbers = (
            self.max_age_s,
            self.max_capture_age_s,
            self.min_rssi,
            self.max_target_disagreement_db,
            self.max_scatter_db,
            self.max_offset_db,
            self.max_auto_step_db,
            self.cooldown_s,
            self.minimum_improvement_m,
            self.minimum_improvement_fraction,
            self.regression_tolerance_m,
            self.minimum_reliability,
        )
        if not all(finite_number(value) for value in numbers):
            msg = "Calibration policy numbers must be finite and cannot be booleans"
            raise ValueError(msg)
        counts = (self.min_training_samples, self.min_holdout_samples, self.min_relative_devices, self.max_observations)
        if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in counts):
            msg = "Calibration evidence counts must be positive integers"
            raise ValueError(msg)
        if (
            not 0 < self.max_age_s <= 86400
            or not self.max_age_s <= self.max_capture_age_s <= 86400
            or not -127 <= self.min_rssi <= -1
            or not 0 < self.max_auto_step_db <= self.max_offset_db <= 30
            or self.cooldown_s < 0
            or self.max_scatter_db <= 0
            or self.max_target_disagreement_db <= 0
            or not 0 <= self.minimum_reliability <= 1
            or not 0 <= self.minimum_improvement_fraction <= 1
            or self.minimum_improvement_m < 0
            or self.regression_tolerance_m < 0
            or self.min_relative_devices < 3
            or self.max_observations > 100000
        ):
            msg = "Calibration policy is outside safe bounds"
            raise ValueError(msg)


def range_from_rssi(rssi: float, reference_power: float, attenuation: float, offset: float = 0.0) -> float:
    """Convert dBm and a dimensionless path-loss exponent into metres."""
    if not all(finite_number(value) for value in (rssi, reference_power, attenuation, offset)) or attenuation <= 0:
        msg = "The radio model requires finite numbers and positive attenuation"
        raise ValueError(msg)
    exponent = (reference_power - (rssi + offset)) / (10.0 * attenuation)
    if exponent > 6 or exponent < -6:
        msg = "The radio model predicts a range outside numerical safety bounds"
        raise ValueError(msg)
    return 10.0**exponent


def _residual(row: Mapping) -> float:
    """The absolute receiver offset required by a frozen independently known range."""
    return row["reference_power"] - 10.0 * row["attenuation"] * math.log10(row["expected_distance"]) - row["rssi"]


def _balanced_median(rows: list[dict], value_key: str) -> float:
    cells = defaultdict(list)
    for row in rows:
        cells[(row["device_id"], row["location_id"])].append(row[value_key])
    return median(median(values) for values in cells.values())


def _error(row: Mapping, offset: float) -> float:
    distance = range_from_rssi(row["rssi"], row["reference_power"], row["attenuation"], offset)
    return max(0.0, abs(distance - row["expected_distance"]) - row["accuracy_m"])


def _metrics(rows: list[dict], offsets: Mapping) -> dict:
    if not rows:
        return {"available": False, "count": 0, "raw_mae_m": None, "current_mae_m": None, "proposed_mae_m": None}
    errors = {"raw": [], "current": [], "proposed": []}
    invalid = Counter()
    for row in rows:
        models = {
            "raw": 0.0,
            "current": row["rssi_offset"],
            "proposed": receiver_value(offsets, row["scanner_id"], row["rssi_offset"]),
        }
        for name, offset in models.items():
            try:
                errors[name].append(_error(row, offset))
            except ValueError:
                invalid[name] += 1
    averages = {name: mean(values) if values and not invalid[name] else None for name, values in errors.items()}
    return {
        "available": True,
        "count": len(rows),
        "raw_mae_m": averages["raw"],
        "current_mae_m": averages["current"],
        "proposed_mae_m": averages["proposed"],
        "current_max_error_m": max(errors["current"]) if errors["current"] and not invalid["current"] else None,
        "proposed_max_error_m": max(errors["proposed"]) if errors["proposed"] and not invalid["proposed"] else None,
        "invalid_predictions": dict(invalid),
    }


def _groups(rows: list[dict], offsets: Mapping, key: str) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return {identifier: _metrics(group, offsets) for identifier, group in sorted(groups.items())}


def _receiver_key(identifier: str) -> str:
    """Normalize MAC case only; every other scanner source remains opaque."""
    parts = identifier.split(":")
    if len(parts) == 6 and all(len(part) == 2 and all(c in "0123456789abcdefABCDEF" for c in part) for part in parts):
        return identifier.lower()
    return identifier


class CalibrationEngine:
    """Pure proposals and decisions; the HA manager owns persistence and rollback."""

    def __init__(self, policy: CalibrationPolicy | None = None) -> None:
        self.policy = policy or CalibrationPolicy()

    def _prepare(
        self,
        observations: Iterable[Mapping],
        *,
        now: float,
        model_version: str,
        current_offsets: Mapping | None = None,
        verified_reference_devices: Iterable[str] = (),
    ) -> tuple[list[dict], dict]:
        if not finite_number(now) or not isinstance(model_version, str) or not model_version:
            msg = "A finite capture time and explicit model_version are required"
            raise ValueError(msg)
        verified = set(verified_reference_devices)
        rejected = Counter()
        result = []
        seen = set()
        for index, source in enumerate(observations):
            if index >= self.policy.max_observations:
                rejected["observation_limit"] += 1
                break
            if not isinstance(source, Mapping):
                rejected["invalid_observation"] += 1
                continue
            reason = self._rejection(source, now, model_version)
            if reason:
                rejected[reason] += 1
                continue
            identity = (source["device_id"], source["scanner_id"], source.get("stamp", source["observed"]))
            if identity in seen:
                rejected["duplicate_source_stamp"] += 1
                continue
            seen.add(identity)
            row = {
                key: source[key]
                for key in (
                    "device_id",
                    "scanner_id",
                    "location_id",
                    "observed",
                    "rssi",
                    "reference_power",
                    "attenuation",
                    "expected_distance",
                    "rssi_offset",
                    "model_version",
                )
            }
            row["stamp"] = source.get("stamp", source["observed"])
            row["accuracy_m"] = source.get("accuracy_m", 0.0)
            row["verified"] = (
                source["device_id"] in verified
                or source.get("reference_power_verified") is True
                or source.get("quality", {}).get("reference_power_verified") is True
            )
            if current_offsets is not None:
                baseline = receiver_value(current_offsets, source["scanner_id"], row["rssi_offset"])
                if not finite_number(baseline) or abs(baseline - row["rssi_offset"]) > 1e-6:
                    rejected["offset_baseline_mismatch"] += 1
                    continue
            row["required_offset"] = _residual(row)
            row["required_delta"] = row["required_offset"] - row["rssi_offset"]
            result.append(row)
        return result, dict(rejected)

    def _rejection(self, row: Mapping, now: float, model_version: str) -> str | None:  # noqa: PLR0911
        for key in ("device_id", "scanner_id", "location_id"):
            value = row.get(key)
            if not isinstance(value, str) or not value or len(value) > 256:
                return "missing_independent_label"
        source = row.get("truth_source")
        if (
            row.get("independent") is not True
            or not isinstance(source, str)
            or source not in {"measured", "fixed_reference"}
        ):
            return "untrusted_truth"
        if row.get("model_version") != model_version:
            return "model_version_mismatch"
        fields = {
            "rssi": (-127.0, 0.0),
            "reference_power": (-120.0, 0.0),
            "attenuation": (0.1, 8.0),
            "rssi_offset": (-30.0, 30.0),
            "expected_distance": (0.05, 1000.0),
            "observed": (0.0, now + 1.0),
        }
        for key, (low, high) in fields.items():
            value = row.get(key)
            if not finite_number(value) or not low <= value <= high:
                return "invalid_numeric_input"
        accuracy = row.get("accuracy_m", 0.0)
        if not finite_number(accuracy) or not 0 <= accuracy <= 100 or accuracy >= row["expected_distance"]:
            return "uncertain_truth"
        stamp = row.get("stamp", row["observed"])
        if not finite_number(stamp) or stamp < 0:
            return "untrusted_source_clock"
        # Frozen measured frames preserve age at capture. A new publication time
        # must never turn a packet that was stale at capture into fresh evidence.
        reference_time = now
        if row.get("frozen") is True:
            captured_at = row.get("captured_at")
            if (
                not finite_number(captured_at)
                or not 0 <= captured_at <= now + 1.0
                or row["observed"] > captured_at + 1.0
                or now - captured_at > self.policy.max_capture_age_s
            ):
                return "untrusted_capture_clock"
            reference_time = captured_at
        age = row.get("age", reference_time - row["observed"])
        if not finite_number(age) or age < 0 or max(age, reference_time - row["observed"]) > self.policy.max_age_s:
            return "stale_observation"
        if row.get("status") != "current":
            return "stale_observation"
        if row["rssi"] < self.policy.min_rssi:
            return "weak_signal"
        quality = row.get("quality", {})
        if not isinstance(quality, Mapping):
            return "untrusted_source"
        if any(quality.get(key) is False for key in ("valid", "trusted", "source_trusted", "reliable")):
            return "untrusted_source"
        if any(quality.get(key) is True for key in ("clock_reset", "clock_jump", "stale", "missing")):
            return "untrusted_source"
        reliability = quality.get("reliability")
        if reliability is not None and (
            not finite_number(reliability) or not self.policy.minimum_reliability <= reliability <= 1
        ):
            return "unreliable_signal"
        try:
            range_from_rssi(row["rssi"], row["reference_power"], row["attenuation"], row["rssi_offset"])
        except ValueError:
            return "unsafe_model"
        return None

    def _fit(self, rows: list[dict], mode: str) -> tuple[dict, list[str]]:
        by_scanner = defaultdict(list)
        for row in rows:
            by_scanner[row["scanner_id"]].append(row)
        offsets, reasons = {}, []
        if any(len(group) < self.policy.min_training_samples for group in by_scanner.values()) or not by_scanner:
            return {}, ["insufficient_training_samples"]
        if mode == "absolute":
            for scanner, group in by_scanner.items():
                proposal = _balanced_median(group, "required_offset")
                scatter = median(abs(row["required_offset"] - proposal) for row in group)
                if scatter > self.policy.max_scatter_db:
                    reasons.append(f"unstable_signal:{scanner}")
                offsets[scanner] = proposal
        else:
            receivers = sorted(by_scanner)
            devices = {row["device_id"] for row in rows}
            if len(receivers) < 2 or len(devices) < self.policy.min_relative_devices:
                return {}, ["unverified_reference_power"]
            # Require a complete target/receiver graph: an unknown transmitter
            # must be compared by the same receivers, not silently imputed.
            cells = defaultdict(list)
            for row in rows:
                cells[(row["device_id"], row["scanner_id"])].append(row)
            if any((device, scanner) not in cells for device in devices for scanner in receivers):
                return {}, ["insufficient_shared_target_coverage"]
            relative = defaultdict(list)
            for device in sorted(devices):
                deltas = {
                    scanner: _balanced_median(cells[(device, scanner)], "required_delta") for scanner in receivers
                }
                center = mean(deltas.values())
                for scanner, delta in deltas.items():
                    relative[scanner].append(delta - center)
            deltas = {scanner: median(values) for scanner, values in relative.items()}
            gauge = mean(deltas.values())
            for scanner in receivers:
                delta = deltas[scanner] - gauge
                if max(abs(value - delta) for value in relative[scanner]) > self.policy.max_target_disagreement_db:
                    reasons.append(f"inconsistent_target_bias:{scanner}")
                offsets[scanner] = by_scanner[scanner][0]["rssi_offset"] + delta
        if any(abs(offset) > self.policy.max_offset_db for offset in offsets.values()):
            reasons.append("offset_out_of_bounds")
        return offsets, reasons

    def preview(
        self,
        observations: Iterable[Mapping],
        *,
        now: float,
        model_version: str,
        current_offsets: Mapping | None = None,
        verified_reference_devices: Iterable[str] = (),
    ) -> dict:
        """
        Fit using training rows and validate separate target/location holdouts.

        The lexically last explicit target and location are wholly excluded from
        fitting. Both validation dimensions, every receiver, and every target must
        improve or avoid regression. A pending result never invents an improvement.
        """
        rows, rejected = self._prepare(
            observations,
            now=now,
            model_version=model_version,
            current_offsets=current_offsets,
            verified_reference_devices=verified_reference_devices,
        )
        verified_rows = [row for row in rows if row["verified"]]
        mode = "absolute" if verified_rows else "relative"
        if verified_rows:
            rejected["unverified_reference_power"] = len(rows) - len(verified_rows)
            rows = verified_rows
        payload = [
            [row[key] for key in ("device_id", "scanner_id", "stamp", "observed", "location_id", "rssi")]
            for row in rows
        ]
        data_id = hashlib.sha256(json.dumps(sorted(payload), separators=(",", ":")).encode()).hexdigest()[:24]
        result = {
            "eligible": False,
            "status": "pending",
            "reasons": [],
            "offsets": {},
            "deltas": {},
            "model_version": model_version,
            "data_id": data_id,
            "created_at": now,
            "latest_observed": max((row["observed"] for row in rows), default=None),
            "mode": mode,
            "rejected": rejected,
            "coverage": {
                "samples": len(rows),
                "receivers": sorted({row["scanner_id"] for row in rows}),
                "devices": sorted({row["device_id"] for row in rows}),
                "locations": sorted({row["location_id"] for row in rows}),
            },
            "metrics": {},
        }
        locations = result["coverage"]["locations"]
        devices = result["coverage"]["devices"]
        needed_devices = 2 if mode == "absolute" else self.policy.min_relative_devices + 1
        if len(locations) < 2 or len(devices) < needed_devices:
            result["reasons"] = ["insufficient_independent_locations_or_targets"]
            result["metrics"]["all"] = _metrics(rows, {})
            return result
        location, device = locations[-1], devices[-1]
        training = [row for row in rows if row["location_id"] != location and row["device_id"] != device]
        held_location = [row for row in rows if row["location_id"] == location]
        held_target = [row for row in rows if row["device_id"] == device]
        offsets, reasons = self._fit(training, mode)
        expected_scanners = set(result["coverage"]["receivers"])
        if set(offsets) != expected_scanners:
            reasons.append("insufficient_training_receiver_coverage")
        for label, holdout in (("location", held_location), ("target", held_target)):
            counts = Counter(row["scanner_id"] for row in holdout)
            if any(counts[scanner] < self.policy.min_holdout_samples for scanner in expected_scanners):
                reasons.append(f"insufficient_{label}_holdout")
        result["holdouts"] = {"location_id": location, "device_id": device, "training_samples": len(training)}
        result["offsets"] = offsets
        baselines = {row["scanner_id"]: row["rssi_offset"] for row in rows}
        if any(abs(row["rssi_offset"] - baselines[row["scanner_id"]]) > 1e-6 for row in rows):
            reasons.append("offset_baseline_mismatch")
        result["baseline_offsets"] = baselines
        result["deltas"] = {scanner: offset - baselines[scanner] for scanner, offset in offsets.items()}
        result["metrics"] = {
            "training": _metrics(training, offsets),
            "location_holdout": _metrics(held_location, offsets),
            "target_holdout": _metrics(held_target, offsets),
            "all": _metrics(rows, offsets),
            "per_receiver": _groups(rows, offsets, "scanner_id"),
            "per_device": _groups(rows, offsets, "device_id"),
            "per_location": _groups(rows, offsets, "location_id"),
        }
        if offsets:
            reasons.extend(self._validation_reasons(result["metrics"]))
        result["reasons"] = sorted(set(reasons))
        result["eligible"] = not reasons and bool(offsets)
        result["status"] = "validated" if result["eligible"] else "rejected" if offsets else "pending"
        return result

    def _validation_reasons(self, metrics: Mapping) -> list[str]:
        reasons = []
        for name in ("location_holdout", "target_holdout"):
            metric = metrics[name]
            if not metric["available"]:
                reasons.append(f"unavailable_{name}")
                continue
            before, after = metric["current_mae_m"], metric["proposed_mae_m"]
            if before is None or after is None:
                reasons.append(f"unsafe_prediction:{name}")
                continue
            required = max(self.policy.minimum_improvement_m, before * self.policy.minimum_improvement_fraction)
            if before - after < required:
                reasons.append(f"no_validated_improvement:{name}")
        for name in ("per_receiver", "per_device", "per_location"):
            for identifier, metric in metrics[name].items():
                before, after = metric["current_mae_m"], metric["proposed_mae_m"]
                if before is None or after is None:
                    reasons.append(f"unsafe_prediction:{name}:{identifier}")
                elif after > before + self.policy.regression_tolerance_m:
                    reasons.append(f"holdout_regression:{name}:{identifier}")
        return reasons

    def automatic_decision(
        self,
        proposal: Mapping,
        *,
        now: float,
        model_version: str,
        current_offsets: Mapping,
        last_applied: float | None = None,
        last_data_id: str | None = None,
        observations: Iterable[Mapping] | None = None,
        verified_reference_devices: Iterable[str] = (),
    ) -> dict:
        """
        Return a fresh, bounded automatic step only for a validated proposal.

        Supply the same labelled observations used by preview when a correction
        requires capping. The actual capped step must pass both frozen holdouts;
        validation of a full correction does not establish that a partial helps.
        """
        reasons = []
        if not finite_number(now):
            msg = "now must be finite"
            raise ValueError(msg)
        if proposal.get("eligible") is not True:
            reasons.append("proposal_not_validated")
        if proposal.get("model_version") != model_version:
            reasons.append("model_version_mismatch")
        observed = proposal.get("latest_observed")
        created = proposal.get("created_at")
        if (
            not finite_number(observed)
            or not finite_number(created)
            or observed > now + 1
            or created > now + 1
            or max(now - observed, now - created) > self.policy.max_age_s
        ):
            reasons.append("stale_proposal")
        if last_applied is not None:
            if not finite_number(last_applied) or now - last_applied < self.policy.cooldown_s:
                reasons.append("cooldown")
        if not proposal.get("data_id") or proposal.get("data_id") == last_data_id:
            reasons.append("no_new_independent_data")
        offsets, deltas = {}, {}
        baseline = proposal.get("baseline_offsets", {})
        requested = proposal.get("offsets", {})
        if not isinstance(baseline, Mapping) or not isinstance(requested, Mapping) or not requested:
            reasons.append("invalid_proposal")
            requested = {}
            baseline = {}
        raw_deltas = {}
        for scanner, proposed in requested.items():
            current = receiver_value(current_offsets, scanner, 0.0)
            original = baseline.get(scanner)
            if not all(finite_number(value) for value in (proposed, current, original)):
                reasons.append("invalid_proposal")
                continue
            if abs(current - original) > 1e-6:
                reasons.append("offset_baseline_mismatch")
            raw_deltas[scanner] = proposed - current
        # Scale all receivers uniformly: independent clamps would break the
        # zero-mean gauge used with unverified transmitter powers.
        maximum = max((abs(value) for value in raw_deltas.values()), default=0)
        scale = min(1.0, self.policy.max_auto_step_db / maximum) if maximum else 1.0
        for scanner, delta in raw_deltas.items():
            step = delta * scale
            offsets[scanner] = receiver_value(current_offsets, scanner, 0.0) + step
            deltas[scanner] = step
            if abs(offsets[scanner]) > self.policy.max_offset_db:
                reasons.append("offset_out_of_bounds")
        if not deltas or max(abs(value) for value in deltas.values()) < 1e-6:
            reasons.append("no_effective_change")
        step_metrics = None
        if scale < 1.0:
            if observations is None:
                reasons.append("capped_step_requires_validation")
            else:
                rows, _ = self._prepare(
                    observations,
                    now=now,
                    model_version=model_version,
                    current_offsets=current_offsets,
                    verified_reference_devices=verified_reference_devices,
                )
                if proposal.get("mode") == "absolute":
                    rows = [row for row in rows if row["verified"]]
                holdouts = proposal.get("holdouts", {})
                location = [row for row in rows if row["location_id"] == holdouts.get("location_id")]
                target = [row for row in rows if row["device_id"] == holdouts.get("device_id")]
                step_metrics = {
                    "location_holdout": _metrics(location, offsets),
                    "target_holdout": _metrics(target, offsets),
                    "per_receiver": _groups(rows, offsets, "scanner_id"),
                    "per_device": _groups(rows, offsets, "device_id"),
                    "per_location": _groups(rows, offsets, "location_id"),
                }
                reasons.extend(self._validation_reasons(step_metrics))
        return {
            "apply": not reasons,
            "reasons": sorted(set(reasons)),
            "offsets": offsets,
            "deltas": deltas,
            "model_version": model_version,
            "data_id": proposal.get("data_id"),
            "step_scale": scale,
            "requires_step_validation": scale < 1.0 and step_metrics is None,
            "step_metrics": step_metrics,
        }

    def evaluate_trial(
        self,
        observations: Iterable[Mapping],
        *,
        previous_offsets: Mapping,
        proposed_offsets: Mapping,
        now: float,
        model_version: str,
        verified_reference_devices: Iterable[str] = (),
    ) -> dict:
        """
        Compare an applied or capped trial on fresh independent evidence.

        ``model_version`` is the frozen evaluation observation version. The manager
        separately checks the applied settings signature before an atomic rollback.
        Rows keep raw RSSI; only the comparison baseline is replaced in a detached
        copy. Insufficient evidence requests continued observation, never success.
        Only new proposed corrections are policy-bounded. A used inherited baseline
        must be finite but may exceed those bounds; unrelated legacy values do not
        participate in this comparison or block monitoring.
        """
        rows, rejected = self._prepare(
            observations, now=now, model_version=model_version, verified_reference_devices=verified_reference_devices
        )
        if not isinstance(previous_offsets, Mapping) or not isinstance(proposed_offsets, Mapping):
            msg = "Trial offsets must be mappings"
            raise ValueError(msg)  # noqa: TRY004 - one validation exception contract for toolkit requests
        proposals = {}
        for scanner, value in proposed_offsets.items():
            if (
                not isinstance(scanner, str)
                or not scanner
                or len(scanner) > 256
                or any(ord(c) < 32 for c in scanner)
                or not finite_number(value)
                or abs(value) > self.policy.max_offset_db
            ):
                msg = "Proposed trial offsets need valid receiver IDs and finite values within calibration bounds"
                raise ValueError(msg)
            key = _receiver_key(scanner)
            if key in proposals and proposals[key] != value:
                msg = "Proposed trial offsets conflict for receiver aliases"
                raise ValueError(msg)
            proposals[key] = float(value)
        selected = []
        missing = object()
        seen = set()
        for row in rows:
            key = _receiver_key(row["scanner_id"])
            if key not in proposals:
                continue
            previous = receiver_value(previous_offsets, row["scanner_id"], missing)
            if previous is missing:
                continue
            if not finite_number(previous):
                msg = "A used previous trial offset must be finite and cannot be a boolean"
                raise ValueError(msg)
            identity = (row["device_id"], key, row["stamp"])
            if identity in seen:
                rejected["duplicate_source_stamp"] = rejected.get("duplicate_source_stamp", 0) + 1
                continue
            seen.add(identity)
            copy = dict(row)
            copy["scanner_id"] = key
            copy["rssi_offset"] = float(previous)
            selected.append(copy)
        locations = {row["location_id"] for row in selected}
        devices = {row["device_id"] for row in selected}
        counts = Counter(row["scanner_id"] for row in selected)
        sufficient = (
            len(locations) >= 2
            and len(devices) >= 2
            and set(counts) == set(proposals)
            and all(count >= self.policy.min_holdout_samples for count in counts.values())
        )
        metrics = {
            "all": _metrics(selected, proposals),
            "per_receiver": _groups(selected, proposals, "scanner_id"),
            "per_device": _groups(selected, proposals, "device_id"),
            "per_location": _groups(selected, proposals, "location_id"),
        }
        regressions = []
        if sufficient:
            for group in ("per_receiver", "per_device", "per_location"):
                for identifier, metric in metrics[group].items():
                    before, after = metric["current_mae_m"], metric["proposed_mae_m"]
                    if before is None or after is None:
                        regressions.append(f"unsafe_prediction:{group}:{identifier}")
                    elif after > before + self.policy.regression_tolerance_m:
                        regressions.append(f"regression:{group}:{identifier}")
        return {
            "sufficient": sufficient,
            "rollback": bool(regressions),
            "accepted": sufficient and not regressions,
            "reasons": regressions if sufficient else ["insufficient_independent_trial_data"],
            "metrics": metrics,
            "rejected": rejected,
            "model_version": model_version,
        }
