"""
Optional RSSI filtering and measurement quality, independent of Home Assistant.

Pass the unmodified receiver RSSI and its actual source stamp to ``process``.
``stamp`` and ``now`` use the same monotonic clock; ``observed`` is optional UTC
metadata. A poll without a packet must use ``snapshot`` or repeat the source
stamp. Neither operation adds observations or extends measurement freshness.
The legacy policy is a diagnostic pass-through; the integration retains its
existing legacy distance algorithm.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and low <= numeric <= high else None


@dataclass(frozen=True, slots=True)
class _Policy:
    tau_s: float
    jump_db: float
    confirmations: int


_POLICIES = {"legacy": _Policy(0, 0, 1), "pet": _Policy(2.0, 12.0, 3), "stationary": _Policy(8.0, 10.0, 4)}


def receiver_quality(
    *,
    noise_db: float | None,
    distance: float | None,
    attenuation: float = 2.0,
    expected_distance: float | None = None,
    expected_accuracy_m: float | None = None,
) -> dict[str, Any]:
    """
    Propagate RSSI noise into bounded range variance and optional truth residual.

    Expected distances/accuracy must come from explicit physical measurements.
    This describes a single receiver's range uncertainty, not position accuracy.
    A noise floor avoids claiming zero variance after constant quantized RSSI.
    """
    dist = _number(distance, 0.0, 1e6)
    loss = _number(attenuation, 0.1, 10.0)
    noise = _number(noise_db, 0.0, 170.0)
    expected = _number(expected_distance, 0.0, 1e6)
    accuracy = _number(expected_accuracy_m, 0.0, 1e6)
    result: dict[str, Any] = {
        "distance_variance_m2": None,
        "range_error_m": None,
        "within_expected_accuracy": None,
        "accuracy_reliability": None,
    }
    if dist is None or loss is None:
        return result
    sigma_db = max(noise if noise is not None else 4.0, 0.5)
    sigma_m = dist * math.log(10.0) / (10.0 * loss) * sigma_db
    variance = sigma_m * sigma_m
    if accuracy is not None:
        variance = max(variance, accuracy * accuracy)
    if expected is not None:
        residual = abs(dist - expected)
        result["range_error_m"] = residual
        if accuracy is not None:
            result["within_expected_accuracy"] = residual <= accuracy
            tolerance = max(accuracy, math.sqrt(variance), 0.01)
            result["accuracy_reliability"] = 1.0 / (1.0 + (residual / tolerance) ** 2)
            variance = max(variance, max(0.0, residual - accuracy) ** 2)
    result["distance_variance_m2"] = min(max(variance, 1e-4), 1e12)
    return result


class FilterState:
    """Bounded raw-sample history, time-aware smoothing and step confirmation."""

    def __init__(self, mode: str = "legacy", *, window_size: int = 32) -> None:
        if not isinstance(mode, str) or mode not in _POLICIES:
            msg = "Unknown RSSI filter mode"
            raise ValueError(msg)
        if isinstance(window_size, bool) or not isinstance(window_size, int) or not 5 <= window_size <= 128:
            msg = "RSSI window must contain 5 to 128 samples"
            raise ValueError(msg)
        self.mode = mode
        self._raw: deque[tuple[float, float]] = deque(maxlen=window_size)
        self._intervals: deque[float] = deque(maxlen=window_size)
        self._accepted: deque[bool] = deque(maxlen=window_size)
        self._candidate: list[float] = []
        self._filtered: float | None = None
        self.last_raw_stamp: float | None = None
        self.last_valid_stamp: float | None = None
        self.last_raw_observed: float | None = None
        self.last_valid_observed: float | None = None
        self._last_now: float | None = None

    def reset(self, mode: str | None = None) -> None:
        """Explicitly reset filter/cadence after a mode or known source-clock change."""
        chosen = self.mode if mode is None else mode
        if not isinstance(chosen, str) or chosen not in _POLICIES:
            msg = "Unknown RSSI filter mode"
            raise ValueError(msg)
        self.mode = chosen
        self._raw.clear()
        self._intervals.clear()
        self._accepted.clear()
        self._candidate.clear()
        self._filtered = None
        self.last_raw_stamp = self.last_valid_stamp = None
        self.last_raw_observed = self.last_valid_observed = None
        self._last_now = None

    @property
    def cadence_s(self) -> float | None:
        """Median elapsed time between actual fresh observations, never poll cycles."""
        return statistics.median(self._intervals) if len(self._intervals) >= 3 else None

    @property
    def timeout_s(self) -> float:
        """A bounded tag-data timeout; it does not establish proxy connectivity."""
        if self.mode == "legacy" or self.cadence_s is None:
            return 30.0
        return min(120.0, max(30.0, self.cadence_s * 4.0))

    def process(
        self,
        rssi: float,
        stamp: float,
        now: float,
        *,
        observed: float | None = None,
        raw_distance: float | None = None,
        expected_distance: float | None = None,
        attenuation: float = 2.0,
        expected_accuracy_m: float | None = None,
    ) -> dict[str, Any]:
        """Consume one actual observation, retaining raw RSSI separately from its filter."""
        value = _number(rssi, -150.0, 20.0)
        source = _number(stamp, 0.0, 1e12)
        current = _number(now, 0.0, 1e12)
        epoch = _number(observed, 0.0, 1e12) if observed is not None else None
        reasons: list[dict[str, Any]] = []
        fresh = accepted = False
        if value is None or source is None or current is None or (observed is not None and epoch is None):
            reasons.append({"code": "invalid_observation"})
        elif source > current + 0.05:
            reasons.append({"code": "future_source_stamp"})
        elif self._last_now is not None and current < self._last_now:
            reasons.append({"code": "current_clock_regression"})
        elif self.last_raw_stamp is not None and source <= self.last_raw_stamp:
            reasons.append({"code": "duplicate_stamp" if source == self.last_raw_stamp else "out_of_order_stamp"})
        elif current - source > self.timeout_s:
            reasons.append({"code": "stale_observation"})
        else:
            fresh = True
            gap = source - self.last_raw_stamp if self.last_raw_stamp is not None else None
            timed_out = self.last_valid_stamp is not None and source - self.last_valid_stamp > self.timeout_s
            if gap is not None:
                if self._candidate and gap > max(5.0, (self.cadence_s or 3.0) * 3.0):
                    self._candidate.clear()
                    reasons.append({"code": "jump_confirmation_gap", "gap_s": gap})
                self._intervals.append(gap)
            if timed_out:
                self._raw.clear()
                self._accepted.clear()
                self._candidate.clear()
                self._filtered = None
                reasons.append({"code": "reseed_after_gap", "gap_s": gap})
            self._raw.append((source, value))
            self.last_raw_stamp = source
            self.last_raw_observed = epoch
            accepted = self._update_filter(value, source, reasons)
            self._accepted.append(accepted)
            if accepted:
                self.last_valid_stamp = source
                self.last_valid_observed = epoch
        if current is not None and (self._last_now is None or current >= self._last_now):
            self._last_now = current
        result = self.snapshot(
            current,
            raw_distance=raw_distance,
            expected_distance=expected_distance,
            attenuation=attenuation,
            expected_accuracy_m=expected_accuracy_m,
        )
        result.update({"raw_rssi": value, "fresh": fresh, "accepted": accepted})
        result["reasons"] = reasons + result["reasons"]
        return result

    def _update_filter(self, value: float, stamp: float, reasons: list[dict[str, Any]]) -> bool:
        policy = _POLICIES[self.mode]
        if self._filtered is None or self.mode == "legacy":
            self._filtered = value
            return True
        # Compare with the last valid filtered signal, not a history of clamped
        # values. A separate consecutive raw candidate prevents endless clamps.
        difference = value - self._filtered
        threshold = policy.jump_db
        if abs(difference) > threshold:
            if self._candidate and (
                (value - self._filtered) * (statistics.median(self._candidate) - self._filtered) <= 0
                or abs(value - statistics.median(self._candidate)) > threshold / 2
            ):
                self._candidate.clear()
            self._candidate.append(value)
            if len(self._candidate) < policy.confirmations:
                reasons.append({"code": "unconfirmed_jump", "fresh_confirmations": len(self._candidate)})
                return False
            self._filtered = statistics.median(self._candidate)
            self._candidate.clear()
            reasons.append({"code": "sustained_step_accepted", "fresh_confirmations": policy.confirmations})
            return True
        self._candidate.clear()
        elapsed = stamp - self.last_valid_stamp if self.last_valid_stamp is not None else 0.0
        alpha = -math.expm1(-max(elapsed, 0.0) / policy.tau_s)
        self._filtered += alpha * difference
        return True

    def snapshot(
        self,
        now: float,
        *,
        raw_distance: float | None = None,
        expected_distance: float | None = None,
        attenuation: float = 2.0,
        expected_accuracy_m: float | None = None,
    ) -> dict[str, Any]:
        """Read quality without adding samples or extending last-valid freshness."""
        current = _number(now, 0.0, 1e12)
        age = (
            max(0.0, current - self.last_valid_stamp)
            if current is not None and self.last_valid_stamp is not None
            else None
        )
        clock_valid = (
            current is not None
            and (self.last_valid_stamp is None or current >= self.last_valid_stamp)
            and (self._last_now is None or current >= self._last_now)
        )
        if clock_valid:
            self._last_now = current
        valid = clock_valid and age is not None and age <= self.timeout_s
        values = [row[1] for row in self._raw]
        variance = statistics.pvariance(values) if len(values) >= 3 else None
        noise = math.sqrt(variance) if variance is not None else None
        reliability = 0.0
        if valid:
            support = min(1.0, len(values) / 5.0)
            noise_factor = 1.0 / (1.0 + ((noise if noise is not None else 4.0) / 6.0) ** 2)
            acceptance = sum(self._accepted) / len(self._accepted) if self._accepted else 0.0
            reliability = support * noise_factor * acceptance * math.exp(-age / self.timeout_s)
        physical = receiver_quality(
            noise_db=noise,
            distance=raw_distance,
            attenuation=attenuation,
            expected_distance=expected_distance,
            expected_accuracy_m=expected_accuracy_m,
        )
        if physical["accuracy_reliability"] is not None:
            reliability *= physical["accuracy_reliability"]
        reasons: list[dict[str, Any]] = []
        if not clock_valid:
            reasons.append({"code": "invalid_current_clock"})
        elif self.last_valid_stamp is None:
            reasons.append({"code": "no_valid_sample"})
        elif not valid:
            reasons.append({"code": "accepted_data_timeout"})
        if len(values) < 5:
            reasons.append({"code": "insufficient_quality_samples", "samples": len(values)})
        return {
            "mode": self.mode,
            "raw_rssi": values[-1] if values else None,
            "filtered_rssi": self._filtered if valid else None,
            "fresh": False,
            "accepted": False,
            "status": "current" if valid else ("stale" if self.last_valid_stamp is not None else "missing"),
            "last_raw_stamp": self.last_raw_stamp,
            "last_valid_stamp": self.last_valid_stamp,
            "last_raw_observed": self.last_raw_observed,
            "last_valid_observed": self.last_valid_observed,
            "age": age,
            "samples": len(values),
            "noise_db": noise,
            "variance_db2": variance,
            "cadence_s": self.cadence_s,
            "timeout_s": self.timeout_s,
            "reliability": reliability,
            "reasons": reasons,
            **physical,
        }
