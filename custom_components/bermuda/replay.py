"""
Frozen-input range replay and observational shadow promotion checks.

Range residuals describe range models only. They are never position accuracy or
area-selection accuracy. Movement/filter lag needs successive independent labels;
unlabelled recordings explicitly leave those metrics unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from copy import deepcopy
from itertools import pairwise
from typing import Any, NoReturn

from .profiles import receiver_value
from .recording import MAX_FRAMES, MAX_IMPORT_BYTES, MAX_ROWS, finite_number, json_size, opaque_id, validate_frame
from .signal_quality import FilterState

FilterCallback = Callable[[dict[str, Any], float], float | None]


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


def _source_identity(identifier: str) -> str:
    """Share replay clock state between MAC aliases while keeping opaque IDs exact."""
    canonical = identifier.lower()
    return receiver_value({canonical: canonical}, identifier, identifier)


def _range_scale(delta_db: float, attenuation: float) -> float:
    """Reject unrepresentable candidates even for supported tiny legacy exponents."""
    try:
        scale = finite_number(10.0 ** (delta_db / (10.0 * attenuation)), "candidate range scale", minimum=0)
    except OverflowError:
        _fail("candidate range exceeds numerical safety bounds")
    if scale == 0:
        _fail("candidate range falls below numerical safety bounds")
    return scale


def _scaled_distance(distance: float, scale: float) -> float:
    """A positive captured range cannot become an unavailable zero through rounding."""
    candidate = finite_number(distance * scale, "candidate distance", minimum=0)
    if distance > 0 and candidate == 0:
        _fail("candidate range falls below numerical safety bounds")
    return candidate


def _offsets(proposed: dict[str, float] | None) -> dict[str, float]:
    if proposed is None:
        return {}
    if type(proposed) is not dict or len(proposed) > 128:
        _fail("candidate offsets must be a bounded scanner map")
    result = {}
    for scanner, value in proposed.items():
        offset = finite_number(value, "candidate offset")
        if abs(offset) > 100:
            _fail("candidate offset exceeds its permitted bound")
        canonical = _source_identity(opaque_id(scanner, "candidate scanner_id"))
        if canonical in result and result[canonical] != offset:
            _fail("conflicting candidate offsets refer to the same receiver")
        result[canonical] = offset
    return result


def _filter_modes(modes: dict[str, str] | None) -> dict[str, str]:
    if modes is None:
        return {}
    if type(modes) is not dict or len(modes) > 128:
        _fail("filter modes must be a bounded device map")
    result = {}
    for device, mode in modes.items():
        if mode not in ("legacy", "pet", "stationary"):
            _fail("candidate filter mode is invalid")
        canonical = _source_identity(opaque_id(device, "candidate device_id"))
        if canonical in result and result[canonical] != mode:
            _fail("conflicting candidate filter modes refer to the same device")
        result[canonical] = mode
    return result


def candidate_signature(
    model_version: str, proposed_offsets: dict[str, float] | None = None, filter_modes: dict[str, str] | None = None
) -> str:
    """Bind absolute proposed receiver offsets to the frozen baseline model."""
    body = {
        "model_version": opaque_id(model_version, "model_version"),
        "proposed_offsets": _offsets(proposed_offsets),
        "filter_modes": _filter_modes(filter_modes),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    labelled = [row for row in rows if row["expected_distance"] is not None and row[key] is not None]
    if not labelled:
        return None
    errors = [row[key] - row["expected_distance"] for row in labelled]
    absolute = sorted(abs(error) for error in errors)
    return {
        "count": len(errors),
        "mae_m": sum(absolute) / len(errors),
        "rmse_m": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "bias_m": sum(errors) / len(errors),
        "max_error_m": absolute[-1],
        "p90_error_m": absolute[min(len(absolute) - 1, math.ceil(len(absolute) * 0.9) - 1)],
        "within_label_accuracy_count": sum(
            abs(error) <= row["accuracy_m"] for row, error in zip(labelled, errors, strict=True)
        ),
    }


def _comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {key: _metrics(rows, key) for key in ("raw", "current", "candidate")}


def _groups(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups = {}
    for row in rows:
        if row["expected_distance"] is not None:
            groups.setdefault(row[key], []).append(row)
    return {value: _comparison(groups[value]) for value in sorted(groups)}


def _device_locations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    for row in rows:
        if row["expected_distance"] is not None:
            groups.setdefault(row["device_id"], []).append(row)
    return {device: _groups(groups[device], "location_id") for device in sorted(groups)}


def _device_location_scanners(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    for row in rows:
        if row["expected_distance"] is not None:
            groups.setdefault(row["device_id"], {}).setdefault(row["location_id"], []).append(row)
    return {
        device: {location: _groups(groups[device][location], "scanner_id") for location in sorted(groups[device])}
        for device in sorted(groups)
    }


class ReplayEngine:
    """Evaluate actual frozen observations; offsets are absolute effective dB values."""

    def evaluate(
        self,
        frames: list[dict[str, Any]],
        *,
        proposed_offsets: dict[str, float] | None = None,
        filter_callback: FilterCallback | None = None,
        filter_modes: dict[str, str] | None = None,
        holdout_locations: list[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """
        Replay detached rows, optionally applying a caller-owned independent filter.

        The callback receives a detached observation with candidate_raw_distance
        and candidate_rssi_offset plus normalized source time (seconds). It must
        return filtered metres or None, and maintain separate per-source state.
        """
        if type(frames) is not list or len(frames) > MAX_FRAMES:
            _fail("replay frames must be a bounded list")
        offsets = _offsets(proposed_offsets)
        modes = _filter_modes(filter_modes)
        if modes and filter_callback is not None:
            _fail("select either built-in candidate filter modes or a filter callback")
        if holdout_locations is not None and (type(holdout_locations) is not list or len(holdout_locations) > 128):
            _fail("holdout locations must be a bounded list")
        holdout = {opaque_id(value, "holdout location") for value in holdout_locations or []}
        # No system clock is needed for already trusted internal captures, but
        # callers importing data must first use DiagnosticHistory.import_data.
        boundary = (
            finite_number(now, "now", minimum=0)
            if now is not None
            else max((finite_number(frame.get("captured_at"), "captured_at", minimum=0) for frame in frames), default=0)
        )
        frozen = [validate_frame(frame, now=boundary) for frame in frames]
        if sum(len(frame["observations"]) for frame in frozen) > MAX_ROWS:
            _fail("replay exceeds the row budget")
        versions = sorted({frame["model_version"] for frame in frozen})
        rows, gaps, duplicate_count = self._replay(frozen, offsets, filter_callback, modes)
        labelled = [row for row in rows if row["expected_distance"] is not None]
        held_out = [row for row in labelled if row["location_id"] in holdout]
        training = [row for row in labelled if row["location_id"] not in holdout]
        movement = self._movement(rows, has_candidate_filter=filter_callback is not None or bool(modes))
        return {
            "schema_version": 1,
            "model_versions": versions,
            "proposed_offsets": offsets,
            "filter_modes": modes,
            "counts": {
                "frames": len(frozen),
                "observations": len(rows),
                "labelled": len(labelled),
                "duplicates_ignored": duplicate_count,
            },
            "range_metrics": _comparison(labelled),
            "per_device": _groups(labelled, "device_id"),
            "per_location": _groups(labelled, "location_id"),
            "per_device_location": _device_locations(labelled),
            "per_device_location_scanner": _device_location_scanners(labelled),
            "per_scanner": _groups(labelled, "scanner_id"),
            "holdout": {
                "locations": sorted(holdout),
                "count": len(held_out),
                "range_metrics": _comparison(held_out),
                "per_device_location": _device_locations(held_out),
            },
            "remaining_labelled": {"count": len(training), "range_metrics": _comparison(training)},
            "packet_gaps": gaps,
            "movement": movement,
            "position_accuracy": {
                "available": False,
                "reason": "range residuals do not measure tracking position accuracy",
            },
            "unavailable": [] if labelled else ["independent range truth is missing"],
            "observations": rows,
        }

    @staticmethod
    def _replay(
        frames: list[dict[str, Any]], offsets: dict[str, float], callback: FilterCallback | None, modes: dict[str, str]
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        rows = []
        clocks = {}
        filter_states = {}
        gaps = []
        duplicate_count = 0
        previous_capture = -1.0
        for frame in frames:
            if frame["captured_at"] < previous_capture:
                _fail("replay captures must be ordered")
            previous_capture = frame["captured_at"]
            labels = {truth["device_id"]: truth for truth in frame["truth"]}
            for observation in frame["observations"]:
                stream = (
                    frame["session_epoch"],
                    _source_identity(observation["device_id"]),
                    _source_identity(observation["scanner_id"]),
                )
                previous = clocks.get(stream)
                if observation["status"] != "current" or (
                    previous
                    and (observation["stamp"] <= previous["stamp"] or observation["observed"] < previous["observed"])
                ):
                    duplicate_count += 1
                    continue
                origin = previous["origin"] if previous else observation["stamp"]
                normalized = observation["stamp"] - origin
                if previous:
                    gaps.append(
                        {
                            "device_id": observation["device_id"],
                            "scanner_id": observation["scanner_id"],
                            "session_epoch": frame["session_epoch"],
                            "seconds": observation["stamp"] - previous["stamp"],
                        }
                    )
                clocks[stream] = {"stamp": observation["stamp"], "observed": observation["observed"], "origin": origin}
                candidate_offset = receiver_value(offsets, observation["scanner_id"], observation["rssi_offset"])
                offset_scale = _range_scale(observation["rssi_offset"] - candidate_offset, observation["attenuation"])
                raw_candidate = _scaled_distance(observation["raw_distance"], offset_scale)
                if raw_candidate > 1e6:
                    _fail("candidate range exceeds the diagnostic model bound")
                mode = receiver_value(modes, observation["device_id"])
                current_mode = observation["quality"].get("mode", "legacy")
                pipeline = "preserved_live_filter"
                unavailable_reason = "frozen live filtered distance is unavailable"
                # An offset-only candidate keeps the actual live filter pipeline.
                # Re-seeding even the same mode would change its captured state
                # and could manufacture improvement over the real current output.
                candidate = (
                    _scaled_distance(observation["filtered_distance"], offset_scale)
                    if observation["filtered_distance"] is not None
                    else None
                )
                if mode == "legacy" and mode != current_mode:
                    # Frozen pet/stationary output cannot reconstruct the stock
                    # legacy histories needed by an explicit pipeline switch.
                    candidate = None
                    pipeline = "unavailable_legacy_history"
                    unavailable_reason = "legacy filter history cannot be reconstructed from this recording"
                elif mode is not None and mode != current_mode:
                    state = filter_states.get(stream)
                    if state is None:
                        state = filter_states[stream] = FilterState(mode)
                    result = state.process(
                        observation["rssi"],
                        normalized,
                        normalized + observation["age"],
                        observed=observation["observed"],
                    )
                    filtered_rssi = result["filtered_rssi"]
                    candidate = (
                        _scaled_distance(
                            raw_candidate, _range_scale(observation["rssi"] - filtered_rssi, observation["attenuation"])
                        )
                        if filtered_rssi is not None
                        else None
                    )
                    pipeline = f"replayed_{mode}_filter"
                    unavailable_reason = "candidate filter output is unavailable"
                if callback is not None:
                    callback_row = deepcopy(observation)
                    callback_row.update(
                        {
                            "candidate_raw_distance": raw_candidate,
                            "candidate_rssi_offset": candidate_offset,
                            "session_epoch": frame["session_epoch"],
                        }
                    )
                    candidate = callback(callback_row, normalized)
                    if candidate is not None:
                        candidate = finite_number(candidate, "candidate filter distance", minimum=0)
                    pipeline = "explicit_filter_callback"
                    unavailable_reason = "candidate filter callback output is unavailable"
                if candidate is not None:
                    candidate = finite_number(candidate, "candidate distance", minimum=0)
                    if candidate > 1e6:
                        _fail("candidate range exceeds the diagnostic model bound")
                truth = receiver_value(labels, observation["device_id"])
                expected = receiver_value(truth["ranges"], observation["scanner_id"]) if truth else None
                rows.append(
                    {
                        **deepcopy(observation),
                        "sequence": frame["sequence"],
                        "session_epoch": frame["session_epoch"],
                        "device_id": observation["device_id"],
                        "scanner_id": observation["scanner_id"],
                        "observed": observation["observed"],
                        "stamp": observation["stamp"],
                        "normalized_source_s": normalized,
                        "model_version": frame["model_version"],
                        "rssi": observation["rssi"],
                        "raw": observation["raw_distance"],
                        "current": observation["filtered_distance"],
                        "candidate": candidate,
                        "candidate_pipeline": pipeline,
                        "candidate_unavailable_reason": unavailable_reason if candidate is None else None,
                        "expected_distance": expected,
                        "location_id": truth["location_id"] if expected is not None else None,
                        "truth_source": truth["source"] if expected is not None else None,
                        "accuracy_m": truth["accuracy_m"] if expected is not None else None,
                    }
                )
        return (
            rows,
            {"count": len(gaps), "max_s": max((gap["seconds"] for gap in gaps), default=None), "intervals": gaps},
            duplicate_count,
        )

    @staticmethod
    def _movement(rows: list[dict[str, Any]], *, has_candidate_filter: bool) -> dict[str, Any]:
        streams = {}
        for row in rows:
            stream = row["session_epoch"], _source_identity(row["device_id"]), _source_identity(row["scanner_id"])
            streams.setdefault(stream, []).append(row)
        transitions = []
        for stream in streams.values():
            labelled = [row for row in stream if row["expected_distance"] is not None]
            for before, after in pairwise(labelled):
                if before["location_id"] == after["location_id"] or abs(
                    after["expected_distance"] - before["expected_distance"]
                ) <= max(before["accuracy_m"], after["accuracy_m"], 0.05):
                    continue
                tolerance = max(after["accuracy_m"], 0.05)
                transition = {
                    "device_id": after["device_id"],
                    "scanner_id": after["scanner_id"],
                    "from_location": before["location_id"],
                    "to_location": after["location_id"],
                    "label_interval_s": after["normalized_source_s"] - before["normalized_source_s"],
                    "timing": "upper bound from first labelled new location; actual movement time is unknown",
                }
                # An instant label does not establish the target for subsequent
                # unlabelled samples. Require independent labels of the new range
                # for every candidate settling observation.
                for key in ("current", "candidate"):
                    episode = []
                    for row in labelled:
                        if row["normalized_source_s"] < after["normalized_source_s"]:
                            continue
                        if (
                            row["location_id"] != after["location_id"]
                            or abs(row["expected_distance"] - after["expected_distance"]) > tolerance
                        ):
                            break
                        episode.append(row)
                    settled = next(
                        (
                            row
                            for row in episode
                            if row[key] is not None and abs(row[key] - row["expected_distance"]) <= tolerance
                        ),
                        None,
                    )
                    transition[f"{key}_settling_upper_bound_s"] = (
                        settled["normalized_source_s"] - after["normalized_source_s"] if settled else None
                    )
                    transition[f"{key}_response_observed"] = settled is not None
                transitions.append(transition)
        return {
            "available": bool(transitions),
            "reason": None if transitions else "successive independent labels at different ranges are missing",
            "candidate_filter_compared": has_candidate_filter,
            "transitions": transitions,
            "true_response_lag_available": False,
            "true_response_lag_reason": "capture labels do not establish the actual movement start time",
        }


class ShadowSession:
    """
    Frozen baseline and candidate observations with no live-settings writes.

    Only new independently labelled samples can qualify promotion. The settings
    owner must persist a successful change before calling mark_promoted(). A
    failed comparison cancels this session and cannot affect live tracking.
    """

    def __init__(
        self,
        frames: list[dict[str, Any]],
        *,
        model_version: str,
        proposed_offsets: dict[str, float] | None = None,
        filter_callback: FilterCallback | None = None,
        filter_modes: dict[str, str] | None = None,
        min_labelled_rows: int = 6,
        min_locations: int = 2,
        regression_tolerance_m: float = 0.05,
        min_relative_improvement: float = 0.02,
        latency_tolerance_s: float = 1,
    ) -> None:
        self.model_version = opaque_id(model_version, "model_version")
        self._offsets = _offsets(proposed_offsets)
        self._callback = filter_callback
        self._filter_modes = _filter_modes(filter_modes)
        if self._filter_modes and filter_callback is not None:
            _fail("select either built-in candidate filter modes or a filter callback")
        self.signature = candidate_signature(model_version, self._offsets, self._filter_modes)
        if type(min_labelled_rows) is not int or not 1 <= min_labelled_rows <= MAX_ROWS:
            _fail("min_labelled_rows must be a bounded positive integer")
        if type(min_locations) is not int or not 2 <= min_locations <= 128:
            _fail("promotion requires at least two independent locations")
        self.min_labelled_rows = min_labelled_rows
        self.min_locations = min_locations
        self.regression_tolerance_m = finite_number(regression_tolerance_m, "regression tolerance", minimum=0)
        self.min_relative_improvement = finite_number(min_relative_improvement, "minimum improvement", minimum=0)
        self.latency_tolerance_s = finite_number(latency_tolerance_s, "latency tolerance", minimum=0)
        if self.min_relative_improvement > 1:
            _fail("minimum improvement cannot exceed one")
        self.state = "active"
        self.cancel_reason: str | None = None
        initial = ReplayEngine().evaluate(frames)
        if initial["model_versions"] and initial["model_versions"] != [self.model_version]:
            _fail("shadow baseline does not match its bound model version")
        self._baseline = deepcopy(frames)
        if json_size(self._baseline) > MAX_IMPORT_BYTES:
            _fail("shadow baseline exceeds its byte budget")
        self._new: list[dict[str, Any]] = []
        self._initial_sources = {
            (row["device_id"], row["scanner_id"], row["stamp"]) for frame in frames for row in frame["observations"]
        }
        self._new_keys: set[tuple[Any, ...]] = set()
        self._shadow_bytes = json_size(self._baseline)
        self._shadow_rows = sum(len(frame["observations"]) for frame in self._baseline)

    @staticmethod
    def _source_keys(frames: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
        return {
            (frame["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"])
            for frame in frames
            for row in frame["observations"]
        }

    @property
    def proposed_offsets(self) -> dict[str, float]:
        return self._offsets.copy()

    @property
    def filter_modes(self) -> dict[str, str]:
        return self._filter_modes.copy()

    def summary(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.cancel_reason,
            "model_version": self.model_version,
            "signature": self.signature,
            "baseline_frames": len(self._baseline),
            "new_frames": len(self._new),
            "proposed_offsets": self.proposed_offsets,
            "filter_modes": self.filter_modes,
        }

    def cancel(self, reason: str) -> None:
        self.state = "cancelled"
        self.cancel_reason = reason

    def stop(self) -> None:
        if self.state == "active":
            self.state = "stopped"

    def add_frame(self, frame: dict[str, Any]) -> int:
        if self.state != "active":
            return 0
        try:
            frozen = validate_frame(frame, now=finite_number(frame.get("captured_at"), "captured_at", minimum=0))
            if frozen["model_version"] != self.model_version:
                _fail("live model version changed during shadow collection")
            labelled_pairs = {(truth["device_id"], scanner) for truth in frozen["truth"] for scanner in truth["ranges"]}
            relabelled_keys = {
                (frozen["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"])
                for row in frozen["observations"]
                if (row["device_id"], row["scanner_id"]) in labelled_pairs
                and (frozen["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]) in self._new_keys
            }
            for old in self._new if relabelled_keys else ():
                for row in old["observations"]:
                    key = old["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]
                    if key not in relabelled_keys:
                        continue
                    previous_truth = next(
                        (
                            label
                            for label in old["truth"]
                            if label["device_id"] == row["device_id"] and row["scanner_id"] in label["ranges"]
                        ),
                        None,
                    )
                    incoming_truth = next(
                        (label for label in frozen["truth"] if label["device_id"] == row["device_id"]), None
                    )
                    if previous_truth is not None:
                        if previous_truth != incoming_truth:
                            _fail("shadow source observation already has different independent truth")
                        relabelled_keys.discard(key)
            rows = [
                row
                for row in frozen["observations"]
                if (row["device_id"], row["scanner_id"], row["stamp"]) not in self._initial_sources
                and (
                    (frozen["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]) not in self._new_keys
                    or (frozen["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]) in relabelled_keys
                )
            ]
            if not rows:
                return 0
            frozen["observations"] = rows
            pairs = {(row["device_id"], row["scanner_id"]) for row in rows}
            frozen["truth"] = [
                truth
                for truth in frozen["truth"]
                if any((truth["device_id"], scanner) in pairs for scanner in truth["ranges"])
            ]
            retained = self._new
            if relabelled_keys:
                retained = []
                for old in self._new:
                    remaining = []
                    for row in old["observations"]:
                        if (old["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]) in relabelled_keys:
                            if any(
                                label["device_id"] == row["device_id"] and row["scanner_id"] in label["ranges"]
                                for label in old["truth"]
                            ):
                                _fail("shadow source observation already has independent truth")
                        else:
                            remaining.append(row)
                    if remaining:
                        retained.append({**old, "observations": remaining})
                previous_rows = sum(len(old["observations"]) for old in self._baseline + retained)
                previous_bytes = json_size(self._baseline + retained)
            else:
                previous_rows, previous_bytes = self._shadow_rows, self._shadow_bytes
            new_size = json_size(frozen) + (1 if self._baseline or retained else 0)
            if (
                len(self._baseline) + len(retained) >= MAX_FRAMES
                or previous_rows + len(rows) > MAX_ROWS
                or previous_bytes + new_size > MAX_IMPORT_BYTES
            ):
                _fail("shadow recording budget exhausted")
            self._new = retained
            self._new.append(frozen)
            self._new_keys.update(self._source_keys([frozen]))
            self._shadow_rows = previous_rows + len(rows)
            self._shadow_bytes = previous_bytes + new_size
        except (ValueError, TypeError, KeyError, OverflowError) as err:
            self.cancel(str(err))
            return 0
        return len(rows)

    def evaluate(self) -> dict[str, Any]:
        try:
            report = ReplayEngine().evaluate(
                self._baseline + self._new,
                proposed_offsets=self._offsets,
                filter_callback=self._callback,
                filter_modes=self._filter_modes,
            )
        except Exception as err:  # noqa: BLE001 - Third-party diagnostic callbacks must fail closed.
            self.cancel(f"shadow comparison failed: {type(err).__name__}")
            return {"state": self.state, "available": False, "reason": self.cancel_reason}
        return {
            "state": self.state,
            "signature": self.signature,
            "model_version": self.model_version,
            "new_frames": len(self._new),
            "report": report,
        }

    def promotion_check(self, *, current_model_version: str) -> dict[str, Any]:
        reasons = []
        if self.state not in ("active", "stopped"):
            reasons.append(self.cancel_reason or f"shadow session is {self.state}")
        if current_model_version != self.model_version:
            reasons.append("candidate is bound to a different live model version")
        evaluated = self.evaluate()
        if not evaluated.get("report"):
            reasons.append(self.cancel_reason or "shadow comparison is unavailable")
            return {"eligible": False, "reasons": reasons, "evaluation": evaluated}
        keys = self._new_keys
        rows = [
            row
            for row in evaluated["report"]["observations"]
            if (row["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]) in keys
            and row["expected_distance"] is not None
        ]
        if len(rows) < self.min_labelled_rows:
            reasons.append("insufficient new independently labelled observations")
        locations = {row["location_id"] for row in rows}
        if len(locations) < self.min_locations:
            reasons.append("insufficient new independent location coverage")
        if any(row["candidate"] is None for row in rows):
            reasons.append("candidate omitted labelled observations")
        if any(row["current"] is None for row in rows):
            reasons.append("frozen live range output is unavailable for labelled observations")
        scanners = {row["scanner_id"]: True for row in rows}
        devices = {row["device_id"]: True for row in rows}
        reasons.extend(
            f"candidate scanner {scanner} lacks new labelled coverage"
            for scanner in sorted(self._offsets)
            if not receiver_value(scanners, scanner, False)
        )
        for scanner in sorted(self._offsets):
            if not receiver_value(scanners, scanner, False):
                continue
            scanner_rows = [row for row in rows if receiver_value({scanner: True}, row["scanner_id"], False)]
            if (
                len(scanner_rows) < self.min_labelled_rows
                or len({row["location_id"] for row in scanner_rows}) < self.min_locations
            ):
                reasons.append(f"candidate scanner {scanner} lacks sufficient independent coverage")
        reasons.extend(
            f"candidate filter device {device} lacks new labelled coverage"
            for device in sorted(self._filter_modes)
            if not receiver_value(devices, device, False)
        )
        comparison = _comparison(rows)
        current, candidate = comparison["current"], comparison["candidate"]
        if current is None or candidate is None:
            reasons.append("new labelled range metrics are unavailable")
        elif current["mae_m"] <= 0 or candidate["mae_m"] > current["mae_m"] * (1 - self.min_relative_improvement):
            reasons.append("candidate does not meet the overall range improvement gate")
        for device, groups in _device_locations(rows).items():
            if len(groups) < self.min_locations:
                reasons.append(f"device {device} lacks independent location coverage")
            if (
                sum(metrics["current"]["count"] if metrics["current"] is not None else 0 for metrics in groups.values())
                < self.min_labelled_rows
            ):
                reasons.append(f"device {device} lacks sufficient new labelled observations")
            for location, metrics in groups.items():
                baseline, proposed = metrics["current"], metrics["candidate"]
                if (
                    baseline is not None
                    and proposed is not None
                    and (
                        proposed["mae_m"] > baseline["mae_m"] + self.regression_tolerance_m
                        or abs(proposed["bias_m"]) > abs(baseline["bias_m"]) + self.regression_tolerance_m
                    )
                ):
                    reasons.append(f"range or bias regression for device {device} at location {location}")
        for device, device_locations in _device_location_scanners(rows).items():
            for location, scanner_groups in device_locations.items():
                for scanner, metrics in scanner_groups.items():
                    baseline, proposed = metrics["current"], metrics["candidate"]
                    if (
                        baseline is not None
                        and proposed is not None
                        and (
                            proposed["mae_m"] > baseline["mae_m"] + self.regression_tolerance_m
                            or abs(proposed["bias_m"]) > abs(baseline["bias_m"]) + self.regression_tolerance_m
                        )
                    ):
                        reasons.append(
                            f"range or bias regression for device {device} at location {location} scanner {scanner}"
                        )
        new_movement = ReplayEngine._movement(  # noqa: SLF001 - Shared internal evaluator in the same module.
            rows, has_candidate_filter=bool(self._filter_modes) or self._callback is not None
        )
        for transition in new_movement["transitions"]:
            baseline_lag = transition["current_settling_upper_bound_s"]
            candidate_lag = transition["candidate_settling_upper_bound_s"]
            if baseline_lag is not None and (
                candidate_lag is None or candidate_lag > baseline_lag + self.latency_tolerance_s
            ):
                reasons.append(f"observed response regression for device {transition['device_id']}")
        return {
            "eligible": not reasons,
            "reasons": reasons,
            "signature": self.signature,
            "model_version": self.model_version,
            "proposed_offsets": self.proposed_offsets,
            "filter_modes": self.filter_modes,
            "new_data_range_metrics": comparison,
            "new_data_movement": new_movement,
            "new_labelled_rows": len(rows),
            "new_locations": sorted(locations),
            "evaluation": evaluated,
        }

    def mark_promoted(self, *, current_model_version: str) -> None:
        """Mark completion only after the settings owner has persisted promotion."""
        if not self.promotion_check(current_model_version=current_model_version)["eligible"]:
            _fail("shadow candidate is not eligible for promotion")
        self.state = "promoted"
