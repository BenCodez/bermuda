"""
Bounded, detached diagnostic observations and deliberately supplied range truth.

History retains source-clock watermarks when recording resumes; a confirmed host
clock reset starts a new epoch. A measured label belongs to one capture; fixed
references must be supplied explicitly for each capture by the settings owner.
Import accepts JSON data, never file paths.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from copy import deepcopy
from typing import Any, NoReturn

SCHEMA_VERSION = 1
MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000
MAX_FRAMES = 2_000
MAX_DEVICES = 128
MAX_WINDOW_S = 86_400
MAX_CLOCK_EPOCH = 1_000_000_000
OPTIONAL_OBSERVATION_FIELDS = frozenset(
    {"source_id", "observed_clock", "reference_power_verified", "filtered_rssi", "unavailable"}
)
OBSERVATION_FIELDS = (
    frozenset(
        {
            "device_id",
            "scanner_id",
            "observed",
            "stamp",
            "rssi",
            "rssi_offset",
            "reference_power",
            "attenuation",
            "raw_distance",
            "filtered_distance",
            "age",
            "status",
            "quality",
            "model_version",
        }
    )
    | OPTIONAL_OBSERVATION_FIELDS
)
TRUTH_FIELDS = frozenset({"source", "device_id", "location_id", "ranges", "accuracy_m", "observed", "independent"})
PRIVATE_KEYS = frozenset(
    {
        "irk",
        "irks",
        "private_key",
        "private_keys",
        "privatekey",
        "privatekeys",
        "identity_resolving_key",
        "identity_resolving_keys",
        "identityresolvingkey",
        "identityresolvingkeys",
        "secret",
        "secrets",
        "token",
        "access_token",
        "password",
        "credentials",
    }
)


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


def finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    """Validate JSON numbers without accepting booleans or nonfinite values."""
    if type(value) not in (int, float):
        _fail(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail(f"{name} must be a finite number")
    if not math.isfinite(number):
        _fail(f"{name} must be a finite number")
    if minimum is not None and number < minimum:
        _fail(f"{name} must be at least {minimum}")
    return number


def opaque_id(value: Any, name: str) -> str:
    """Validate opaque identifiers without interpreting friendly names or addresses."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        _fail(f"{name} must be a nonempty bounded identifier")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{name} is outside its permitted integer bounds")
    return value


def _json_copy(value: Any, *, depth: int = 0) -> Any:
    """Detach JSON data and redact private keys recursively, with complexity limits."""
    if depth > 8:
        _fail("diagnostic metadata is too deeply nested")
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        finite_number(value, "metadata value")
        return value
    if isinstance(value, str):
        if len(value) > 2048:
            _fail("diagnostic metadata string is too long")
        return value
    if type(value) is list:
        if len(value) > 256:
            _fail("diagnostic metadata list is too long")
        return [_json_copy(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > 128:
            _fail("diagnostic metadata object is too large")
        result = {}
        for key, item in value.items():
            opaque_id(key, "metadata key")
            if key.casefold().replace("-", "_") not in PRIVATE_KEYS:
                result[key] = _json_copy(item, depth=depth + 1)
        return result
    return _fail("diagnostic metadata must contain JSON primitives")


def json_size(value: Any) -> int:
    """Return the actual compact JSON UTF-8 size used by the history byte budget."""
    try:
        return len(json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        return _fail("diagnostics must be finite bounded JSON data")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("history JSON contains duplicate object keys")
        result[key] = value
    return result


def validate_observation(value: Any, *, captured_at: float, model_version: str) -> dict[str, Any]:
    """Validate and detach the shared observation contract."""
    if type(value) is not dict or set(value) - OBSERVATION_FIELDS:
        _fail("observation has unsupported fields")
    missing = OBSERVATION_FIELDS - OPTIONAL_OBSERVATION_FIELDS - {"quality", "filtered_distance"} - set(value)
    if missing:
        _fail(f"observation is missing {', '.join(sorted(missing))}")
    result = {
        "device_id": opaque_id(value["device_id"], "device_id"),
        "scanner_id": opaque_id(value["scanner_id"], "scanner_id"),
        "model_version": opaque_id(value["model_version"], "model_version"),
    }
    if result["model_version"] != model_version:
        _fail("observation model version differs from its frozen frame")
    for key in ("observed", "stamp", "age", "raw_distance"):
        result[key] = finite_number(value[key], key, minimum=0)
    if result["observed"] > captured_at:
        _fail("source observation cannot be after capture")
    for key in ("rssi", "rssi_offset", "reference_power"):
        result[key] = finite_number(value[key], key)
    # Frozen diagnostics preserve effective legacy settings. New profiles and
    # calibration proposals enforce their own narrower numeric bounds.
    result["attenuation"] = finite_number(value["attenuation"], "attenuation")
    if result["attenuation"] <= 0:
        _fail("attenuation must be positive")
    if not -200 <= result["rssi"] <= 20:
        _fail("RSSI exceeds its permitted bound")
    result["filtered_distance"] = value.get("filtered_distance")
    if result["filtered_distance"] is not None:
        result["filtered_distance"] = finite_number(result["filtered_distance"], "filtered_distance", minimum=0)
    if result["raw_distance"] > 1e6 or (result["filtered_distance"] or 0) > 1e6:
        _fail("diagnostic range exceeds its permitted bound")
    if result["stamp"] > 1e12:
        _fail("source stamp exceeds its permitted clock bound")
    if value["status"] not in ("current", "stale", "missing"):
        _fail("observation status is invalid")
    result["status"] = value["status"]
    quality = value.get("quality", {})
    if type(quality) is not dict:
        _fail("quality must be a JSON object")
    result["quality"] = _json_copy(quality)
    if "source_id" in value:
        result["source_id"] = opaque_id(value["source_id"], "source_id")
    if "observed_clock" in value:
        if value["observed_clock"] not in ("source_epoch", "derived_monotonic"):
            _fail("observation clock identifier is invalid")
        result["observed_clock"] = value["observed_clock"]
    if "reference_power_verified" in value:
        if type(value["reference_power_verified"]) is not bool:
            _fail("reference_power_verified must be a boolean")
        result["reference_power_verified"] = value["reference_power_verified"]
    if "filtered_rssi" in value:
        result["filtered_rssi"] = value["filtered_rssi"]
        if result["filtered_rssi"] is not None:
            result["filtered_rssi"] = finite_number(result["filtered_rssi"], "filtered_rssi")
    if "unavailable" in value:
        if type(value["unavailable"]) is not list or len(value["unavailable"]) > 32:
            _fail("unavailable must be a bounded diagnostic list")
        result["unavailable"] = [opaque_id(item, "unavailable metric") for item in value["unavailable"]]
    return result


def validate_truth(value: Any, *, captured_at: float) -> dict[str, Any]:
    """Validate deliberate independent labels; a predicted area is never truth."""
    if type(value) is not dict or set(value) != TRUTH_FIELDS:
        _fail("truth must contain exactly the shared truth fields")
    if value["source"] not in ("measured", "fixed_reference") or value["independent"] is not True:
        _fail("truth requires an independent measured or fixed reference label")
    if type(value["ranges"]) is not dict or not 1 <= len(value["ranges"]) <= MAX_DEVICES:
        _fail("truth ranges must be a nonempty bounded scanner map")
    ranges = {}
    for scanner, distance in value["ranges"].items():
        ranges[opaque_id(scanner, "truth scanner_id")] = finite_number(distance, "truth range", minimum=0.001)
        if ranges[scanner] > 1e6:
            _fail("truth range exceeds its permitted bound")
    observed = finite_number(value["observed"], "truth observed", minimum=0)
    if observed > captured_at:
        _fail("truth observation cannot be after capture")
    # Instant measurements cannot silently attach old labels to a later capture.
    if value["source"] == "measured" and captured_at - observed > 5:
        _fail("measured truth must belong to this capture")
    accuracy = finite_number(value["accuracy_m"], "truth accuracy_m", minimum=0)
    if accuracy > 1e6:
        _fail("truth accuracy exceeds its permitted bound")
    return {
        "source": value["source"],
        "device_id": opaque_id(value["device_id"], "truth device_id"),
        "location_id": opaque_id(value["location_id"], "truth location_id"),
        "ranges": ranges,
        "accuracy_m": accuracy,
        "observed": observed,
        "independent": True,
    }


def validate_frame(value: Any, *, now: float) -> dict[str, Any]:
    """Validate one complete frozen frame without reading external data."""
    fields = {"sequence", "captured_at", "session_epoch", "model_version", "observations", "truth"}
    if type(value) is not dict or set(value) != fields:
        _fail("frame has unsupported or missing fields")
    captured = finite_number(value["captured_at"], "captured_at", minimum=0)
    if captured > now:
        _fail("future captures cannot be imported")
    version = opaque_id(value["model_version"], "model_version")
    rows = value["observations"]
    truth = value["truth"]
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        _fail("frame observations must be a nonempty bounded list")
    if type(truth) is not list or len(truth) > MAX_DEVICES:
        _fail("frame truth must be a bounded list")
    result = {
        "sequence": _integer(value["sequence"], "sequence", 1, MAX_CLOCK_EPOCH),
        "captured_at": captured,
        "session_epoch": _integer(value["session_epoch"], "session_epoch", 1, MAX_CLOCK_EPOCH),
        "model_version": version,
        "observations": [validate_observation(row, captured_at=captured, model_version=version) for row in rows],
        "truth": [validate_truth(label, captured_at=captured) for label in truth],
    }
    _validate_label_links(result)
    return result


def _validate_label_links(frame: dict[str, Any]) -> None:
    pairs = {(row["device_id"], row["scanner_id"]) for row in frame["observations"]}
    seen = set()
    for truth in frame["truth"]:
        if truth["device_id"] in seen:
            _fail("a device cannot have conflicting truth labels in one capture")
        seen.add(truth["device_id"])
        if not any((truth["device_id"], scanner) in pairs for scanner in truth["ranges"]):
            _fail("truth has no corresponding observation in its capture")


class DiagnosticHistory:
    """A bounded circular history; callers own authentication and persistence."""

    def __init__(
        self,
        *,
        window_s: float = 900,
        max_frames: int = 600,
        max_rows: int = 10_000,
        max_bytes: int = 4 * 1024 * 1024,
        enabled: bool = False,
        selected_devices: list[str] | None = None,
    ) -> None:
        self.window_s = finite_number(window_s, "window_s", minimum=1)
        if self.window_s > MAX_WINDOW_S:
            _fail("history window exceeds one day")
        self.max_frames = _integer(max_frames, "max_frames", 1, MAX_FRAMES)
        self.max_rows = _integer(max_rows, "max_rows", 1, MAX_ROWS)
        self.max_bytes = _integer(max_bytes, "max_bytes", 512, MAX_IMPORT_BYTES)
        if type(enabled) is not bool:
            _fail("enabled must be a boolean")
        self.enabled = enabled
        self.selected_devices = self._selection(selected_devices)
        self.session_epoch = 1
        self._sequence = 0
        self._frames: list[dict[str, Any]] = []
        self._clocks: dict[tuple[str, str], tuple[float, float]] = {}
        self._source_clocks: dict[tuple[str, str], tuple[float, float]] = {}
        self._live_boundaries: dict[tuple[str, str], tuple[float, float]] = {}
        self._redaction_salt = secrets.token_hex(16)
        self._frame_sizes: list[int] = []
        self._frame_budgets: list[int] = []
        self._row_count = 0
        self._byte_count = 2
        self._budget_count = 2
        self.dropped_rows = 0

    @staticmethod
    def _selection(devices: list[str] | None) -> list[str]:
        if devices is None:
            return []
        if type(devices) is not list or len(devices) > MAX_DEVICES:
            _fail("selected_devices must be a bounded list")
        return sorted({opaque_id(device, "selected device") for device in devices})

    def start(self, *, selected_devices: list[str] | None = None, clear: bool = True) -> None:
        selection = self.selected_devices
        if selected_devices is not None:
            selection = self._selection(selected_devices)
            if not clear and selection != self.selected_devices:
                _fail("changing the selected devices requires clearing history")
        if clear:
            self.clear()
        self.selected_devices = selection
        self.enabled = True

    def new_source_session(self, *, compact: bool = False) -> bool:
        """Start a confirmed host-clock epoch, retaining historical frames."""
        if type(compact) is not bool:
            _fail("compact must be a boolean")
        rebased = self.session_epoch >= MAX_CLOCK_EPOCH and compact
        if self.session_epoch >= MAX_CLOCK_EPOCH and not rebased:
            _fail("history source-clock epoch exhausted; cannot start a new session")
        if rebased:
            # Only a confirmed reboot may renumber exhausted epochs. Frozen
            # packet, observation and truth data stay exactly as recorded.
            epochs = sorted({frame["session_epoch"] for frame in self._frames})
            next_epoch = _integer(len(epochs) + 1, "session_epoch", 1, MAX_CLOCK_EPOCH)
            identities = {epoch: index for index, epoch in enumerate(epochs, start=1)}
            self._frames = [{**frame, "session_epoch": identities[frame["session_epoch"]]} for frame in self._frames]
            self.session_epoch = next_epoch
            self._refresh_counts()
        else:
            self.session_epoch += 1
        self._clocks.clear()
        self._source_clocks.clear()
        self._live_boundaries.clear()
        if self._frames:
            self._prune(self._frames[-1]["captured_at"])
        return rebased

    def stop(self) -> None:
        self.enabled = False

    def reconfigure(self, *, window_s: float, now: float) -> None:
        """Validate a new window and prune existing evidence without new captures."""
        window = finite_number(window_s, "window_s", minimum=1)
        current = finite_number(now, "now", minimum=0)
        if window > MAX_WINDOW_S:
            _fail("history window exceeds one day")
        if self._frames and current < self._frames[-1]["captured_at"]:
            _fail("history reconfiguration cannot precede its latest capture")
        self.window_s = window
        self._prune(current)

    def clear(self) -> None:
        if self.session_epoch >= MAX_CLOCK_EPOCH:
            _fail("history source-clock epoch exhausted; cannot clear the history")
        self._frames.clear()
        self._frame_sizes.clear()
        self._frame_budgets.clear()
        self._row_count = 0
        self._byte_count = 2
        self._budget_count = 2
        self._clocks.clear()
        self._source_clocks.clear()
        self._live_boundaries.clear()
        self._sequence = 0
        self.dropped_rows = 0
        self.session_epoch += 1
        self._redaction_salt = secrets.token_hex(16)

    @staticmethod
    def _clock_id(identifier: str) -> str:
        """Canonicalize MAC case while retaining opaque public identifiers."""
        parts = identifier.split(":")
        if len(parts) == 6 and all(
            len(part) == 2 and all(char in "0123456789abcdefABCDEF" for char in part) for part in parts
        ):
            return identifier.lower()
        return identifier

    @classmethod
    def _source_key(cls, row: dict[str, Any]) -> tuple[str, str]:
        return cls._clock_id(row.get("source_id", row["device_id"])), cls._clock_id(row["scanner_id"])

    def retain_live_sources(
        self, previous: DiagnosticHistory, observations: list[dict[str, Any]], *, now: float
    ) -> None:
        """Seed an imported history from trusted current-host clocks, never imported stamps."""
        current = finite_number(now, "now", minimum=0)
        epoch = max(self.session_epoch, previous.session_epoch + 1)
        _integer(epoch, "session_epoch", 1, MAX_CLOCK_EPOCH)
        sources = previous._source_clocks.copy()
        for row in observations:
            try:
                device = opaque_id(row.get("source_id", row.get("device_id")), "source_id")
                scanner = opaque_id(row.get("scanner_id"), "scanner_id")
                stamp = finite_number(row.get("stamp"), "stamp", minimum=0)
                observed = finite_number(row.get("observed"), "observed", minimum=0)
                finite_number(row.get("age"), "age", minimum=0)
            except ValueError:
                continue
            if stamp > 1e12 or observed > current:
                continue
            pair = self._clock_id(device), self._clock_id(scanner)
            old = sources.get(pair, (0.0, 0.0))
            sources[pair] = max(old[0], stamp), max(old[1], observed)
        if len(sources) > self.max_rows or len(previous._clocks) > self.max_rows:
            _fail("current live clock watermarks exceed the row budget")
        if self._budget_count + self._header_size(session_epoch=epoch) - 2 > self.max_bytes:
            _fail("normalized import exceeds the history byte budget")
        self.session_epoch = epoch
        self._clocks = previous._clocks.copy()
        self._source_clocks = sources
        self._live_boundaries = sources.copy()

    def record(
        self,
        observations: list[dict[str, Any]],
        *,
        captured_at: float,
        model_version: str,
        truth: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> int:
        """Record actual new source observations, returning the accepted row count."""
        if type(force) is not bool:
            _fail("force must be a boolean")
        if force and not truth:
            _fail("explicit capture while history is disabled requires independent truth")
        if not self.enabled and not force:
            return 0
        captured = finite_number(captured_at, "captured_at", minimum=0)
        version = opaque_id(model_version, "model_version")
        if type(observations) is not list or len(observations) > MAX_ROWS:
            _fail("observations must be a bounded list")
        if truth is not None and (type(truth) is not list or len(truth) > MAX_DEVICES):
            _fail("truth must be a bounded list")
        if self._frames and captured < self._frames[-1]["captured_at"]:
            _fail("capture clock moved backwards; start a new history session")
        rows = []
        labels = [validate_truth(label, captured_at=captured) for label in truth or []]
        labelled_pairs = {(label["device_id"], scanner) for label in labels for scanner in label["ranges"]}
        relocated = set()
        clocks = self._clocks.copy()
        source_clocks = self._source_clocks.copy()
        for item in observations:
            try:
                row = validate_observation(item, captured_at=captured, model_version=version)
            except ValueError:
                # A live source outside the recording contract must not prevent
                # other sources from supplying history or independent evidence.
                self.dropped_rows += 1
                continue
            pair = row["device_id"], row["scanner_id"]
            source_pair = self._source_key(row)
            if (
                self.selected_devices
                and pair[0] not in self.selected_devices
                and (not force or pair not in labelled_pairs)
            ):
                continue
            boundary = self._live_boundaries.get(source_pair)
            if boundary and (row["stamp"] <= boundary[0] or row["observed"] < boundary[1]):
                continue
            previous = clocks.get(pair)
            if (previous is None and len(clocks) >= self.max_rows) or (
                source_pair not in source_clocks and len(source_clocks) >= self.max_rows
            ):
                # Bound clock metadata as well as stored observations. Clearing
                # the session permits a new set of independently tracked sources.
                self.dropped_rows += 1
                continue
            if row["status"] != "current":
                continue
            if previous and (row["stamp"] <= previous[0] or row["observed"] < previous[1]):
                if not force or pair not in labelled_pairs or row["stamp"] != previous[0]:
                    continue
                if all(
                    label["source"] == "fixed_reference"
                    for label in labels
                    if label["device_id"] == pair[0] and pair[1] in label["ranges"]
                ):
                    continue
                key = self.session_epoch, pair[0], pair[1], row["stamp"]
                matching = [
                    frame
                    for frame in self._frames
                    if frame["session_epoch"] == key[0]
                    and any(
                        (old["device_id"], old["scanner_id"], old["stamp"]) == key[1:] for old in frame["observations"]
                    )
                ]
                if not matching:
                    _fail("cannot relabel an evicted source observation")
                if any(
                    label["device_id"] == pair[0] and pair[1] in label["ranges"]
                    for frame in matching
                    for label in frame["truth"]
                ):
                    _fail("source observation already has independent truth")
                # Move an unlabelled source into this explicit labelled capture.
                # Its original source clock is unchanged and remains counted once.
                relocated.add(key)
            rows.append(row)
            clocks[pair] = row["stamp"], row["observed"]
            old_source = source_clocks.get(source_pair, (0.0, 0.0))
            source_clocks[source_pair] = max(old_source[0], row["stamp"]), max(old_source[1], row["observed"])
        if not rows:
            self._prune(captured)
            return 0
        rows = rows[: self.max_rows]
        relocated.intersection_update(
            {(self.session_epoch, row["device_id"], row["scanner_id"], row["stamp"]) for row in rows}
        )
        if self._sequence >= MAX_CLOCK_EPOCH:
            _fail("history sequence exhausted; clear the history")
        accepted_pairs = {(row["device_id"], row["scanner_id"]) for row in rows}
        # Sources can have independent cadences. A repeated source contributes
        # neither a new observation nor a label to this capture; another source
        # in the same batch can still supply valid independent evidence.
        labels = [
            label
            for label in labels
            if any((label["device_id"], scanner) in accepted_pairs for scanner in label["ranges"])
        ]
        frame = {
            "sequence": self._sequence + 1,
            "captured_at": captured,
            "session_epoch": self.session_epoch,
            "model_version": version,
            "observations": rows,
            "truth": labels,
        }
        _validate_label_links(frame)
        if self._frame_budget(frame) + self._header_size() > self.max_bytes:
            self.dropped_rows += len(rows)
            return 0
        if relocated:
            retained = []
            for old in self._frames:
                remaining = [
                    row
                    for row in old["observations"]
                    if (old["session_epoch"], row["device_id"], row["scanner_id"], row["stamp"]) not in relocated
                ]
                if remaining:
                    retained.append({**old, "observations": remaining})
            self._frames = retained
            self._refresh_counts()
        self._sequence += 1
        # Remember only accepted clocks, including clocks of subsequently pruned
        # rows, so an old packet does not become new evidence after eviction.
        for row in rows:
            self._clocks[row["device_id"], row["scanner_id"]] = row["stamp"], row["observed"]
            pair = self._source_key(row)
            old_source = self._source_clocks.get(pair, (0.0, 0.0))
            self._source_clocks[pair] = max(old_source[0], row["stamp"]), max(old_source[1], row["observed"])
        self._frames.append(frame)
        self._frame_sizes.append(json_size(frame))
        self._frame_budgets.append(self._frame_budget(frame))
        self._row_count += len(rows)
        self._byte_count += self._frame_sizes[-1] + (1 if len(self._frames) > 1 else 0)
        self._budget_count += self._frame_budgets[-1] + (1 if len(self._frames) > 1 else 0)
        self._prune(captured)
        return len(rows)

    def _prune(self, now: float) -> None:
        while self._frames and (
            self._frames[0]["captured_at"] < now - self.window_s
            or len(self._frames) > self.max_frames
            or self._row_count > self.max_rows
            or self._budget_count + self._header_size() - 2 > self.max_bytes
        ):
            removed = len(self._frames.pop(0)["observations"])
            self.dropped_rows += removed
            self._row_count -= removed
            self._byte_count -= self._frame_sizes.pop(0) + (1 if self._frames else 0)
            self._budget_count -= self._frame_budgets.pop(0) + (1 if self._frames else 0)

    def _pseudonym(self, identifier: str) -> str:
        return "id_" + hashlib.sha256((self._redaction_salt + identifier).encode()).hexdigest()[:24]

    @classmethod
    def _validate_receiver_clocks(cls, frames: list[dict[str, Any]]) -> None:
        """Reject clock collisions that canonical receiver pseudonyms would merge."""
        clocks = {}
        for frame in frames:
            for row in frame["observations"]:
                key = frame["session_epoch"], row["device_id"], cls._clock_id(row["scanner_id"])
                previous = clocks.get(key)
                if previous and (row["stamp"] <= previous[0] or row["observed"] < previous[1]):
                    _fail("history contains duplicate or backwards receiver clocks after MAC normalization")
                clocks[key] = row["stamp"], row["observed"]

    def _redact_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(frame)
        for row in result["observations"]:
            source = row.get("source_id", row["device_id"])
            canonical_source = self._clock_id(source)
            # Logical device IDs remain exact. Preserve a legacy row's physical
            # MAC fallback explicitly when its logical pseudonym would differ.
            if "source_id" in row or canonical_source != source:
                row["source_id"] = self._pseudonym(canonical_source)
            row["device_id"] = self._pseudonym(row["device_id"])
            row["scanner_id"] = self._pseudonym(self._clock_id(row["scanner_id"]))
        for label in result["truth"]:
            label["device_id"] = self._pseudonym(label["device_id"])
            label["location_id"] = self._pseudonym(label["location_id"])
            ranges = {}
            for scanner, distance in label["ranges"].items():
                canonical_scanner = self._clock_id(scanner)
                if canonical_scanner in ranges:
                    _fail("truth ranges contain MAC case aliases that cannot be redacted")
                ranges[canonical_scanner] = distance
            label["ranges"] = {self._pseudonym(scanner): distance for scanner, distance in ranges.items()}
        return result

    def _frame_budget(self, frame: dict[str, Any]) -> int:
        return max(json_size(frame), json_size(self._redact_frame(frame)))

    def _header_size(self, *, session_epoch: int | None = None, selected_devices: list[str] | None = None) -> int:
        epoch = self.session_epoch if session_epoch is None else session_epoch
        selection = self.selected_devices if selected_devices is None else selected_devices
        base = {
            "schema_version": SCHEMA_VERSION,
            "session_epoch": epoch,
            "selected_devices": selection,
            "redacted": True,
            "frames": [],
        }
        redacted = {**base, "selected_devices": [self._pseudonym(device) for device in selection]}
        return max(json_size(base), json_size(redacted))

    def _refresh_counts(self) -> None:
        self._frame_sizes = [json_size(frame) for frame in self._frames]
        self._frame_budgets = [self._frame_budget(frame) for frame in self._frames]
        self._row_count = sum(len(frame["observations"]) for frame in self._frames)
        self._byte_count = 2 + sum(self._frame_sizes) + max(0, len(self._frames) - 1)
        self._budget_count = 2 + sum(self._frame_budgets) + max(0, len(self._frames) - 1)

    def attach_truth(self, truth: dict[str, Any], *, sequence: int | None = None) -> None:
        """Label exactly one stored capture; never propagate measured truth forward."""
        frames = [frame for frame in self._frames if sequence is None or frame["sequence"] == sequence]
        if not frames:
            _fail("there is no capture to label")
        frame = deepcopy(frames[-1])
        label = validate_truth(truth, captured_at=frame["captured_at"])
        frame["truth"] = [old for old in frame["truth"] if old["device_id"] != label["device_id"]] + [label]
        _validate_label_links(frame)
        index = self._frames.index(frames[-1])
        proposed = self._frames.copy()
        proposed[index] = frame
        if (
            sum(self._frame_budget(item) for item in proposed) + max(0, len(proposed) - 1) + self._header_size()
            > self.max_bytes
        ):
            _fail("label exceeds the history byte budget")
        self._frames[index] = frame
        self._refresh_counts()

    def frames(self) -> list[dict[str, Any]]:
        return deepcopy(self._frames)

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "session_epoch": self.session_epoch,
            "frames": len(self._frames),
            "rows": self._row_count,
            "bytes": self._byte_count,
            "selected_devices": self.selected_devices.copy(),
            "dropped_rows": self.dropped_rows,
            "limits": {
                "window_s": self.window_s,
                "max_frames": self.max_frames,
                "max_rows": self.max_rows,
                "max_bytes": self.max_bytes,
            },
            "persistence": "in memory; integration controls explicit import and local persistence",
        }

    def export(self, *, redact: bool = True) -> dict[str, Any]:
        """Export already-redacted frozen data without any path or private key fields."""
        if type(redact) is not bool:
            _fail("redact must be a boolean")
        if redact:
            self._validate_receiver_clocks(self._frames)
        result = {
            "schema_version": SCHEMA_VERSION,
            "session_epoch": self.session_epoch,
            "selected_devices": self.selected_devices.copy(),
            "redacted": True,
            "frames": self.frames(),
        }
        if redact:
            result["selected_devices"] = [self._pseudonym(identifier) for identifier in result["selected_devices"]]
            result["frames"] = [self._redact_frame(frame) for frame in self._frames]
        return result

    def export_clocks(self) -> dict[str, Any]:
        """Detach local-only watermarks, including sources whose frames were evicted."""
        return {
            "session_epoch": self.session_epoch,
            "clocks": [
                [device, scanner, stamp, observed]
                for (device, scanner), (stamp, observed) in sorted(self._clocks.items())
            ],
            "source_clocks": [
                [source, scanner, stamp, observed]
                for (source, scanner), (stamp, observed) in sorted(self._source_clocks.items())
            ],
            "live_boundaries": [
                [source, scanner, stamp, observed]
                for (source, scanner), (stamp, observed) in sorted(self._live_boundaries.items())
            ],
        }

    def _restore_clock_rows(
        self, rows: Any, existing: dict[tuple[str, str], tuple[float, float]], *, now: float, physical: bool = False
    ) -> dict[tuple[str, str], tuple[float, float]]:
        if type(rows) is not list or len(rows) > self.max_rows:
            _fail("local clock watermarks exceed the row budget")
        restored = existing.copy()
        seen = set()
        for row in rows:
            if type(row) is not list or len(row) != 4:
                _fail("local clock watermarks require device, scanner, stamp and observed")
            pair = opaque_id(row[0], "clock device_id"), opaque_id(row[1], "clock scanner_id")
            if physical:
                pair = self._clock_id(pair[0]), self._clock_id(pair[1])
            if pair in seen:
                _fail("local clock watermarks contain duplicate sources")
            seen.add(pair)
            stamp = finite_number(row[2], "clock stamp", minimum=0)
            observed = finite_number(row[3], "clock observed", minimum=0)
            if stamp > 1e12:
                _fail("source stamp exceeds its permitted clock bound")
            if observed > now:
                _fail("local clock watermarks cannot contain future observations")
            previous = restored.get(pair)
            if previous and (stamp < previous[0] or observed < previous[1]):
                _fail("local clock watermarks cannot move existing evidence backwards")
            restored[pair] = stamp, observed
        if len(restored) > self.max_rows:
            _fail("combined local clock watermarks exceed the row budget")
        return restored

    def restore_clocks(self, data: dict[str, Any], *, now: float) -> None:
        """
        Atomically merge trusted local clock watermarks after a same-host resume.

        This local Store metadata is deliberately absent from public exports and
        imports. Resumed frame watermarks are retained, and every supplied marker
        must be at least as recent as existing evidence in the same session.
        """
        current = finite_number(now, "now", minimum=0)
        if (
            type(data) is not dict
            or not {"session_epoch", "clocks"} <= data.keys()
            or data.keys() - {"session_epoch", "clocks", "source_clocks", "live_boundaries"}
        ):
            _fail("unsupported local history clock fields")
        epoch = _integer(data["session_epoch"], "session_epoch", 1, MAX_CLOCK_EPOCH)
        if epoch != self.session_epoch:
            _fail("local clock watermarks must match the resumed history session")
        clocks = self._restore_clock_rows(data["clocks"], self._clocks, now=current)
        sources = self._restore_clock_rows(
            data.get("source_clocks", []), self._source_clocks, now=current, physical=True
        )
        boundaries = self._restore_clock_rows(
            data.get("live_boundaries", []), self._live_boundaries, now=current, physical=True
        )
        for pair, boundary in boundaries.items():
            source = sources.get(pair)
            if source is None or boundary[0] > source[0] or boundary[1] > source[1]:
                _fail("live boundaries must be covered by trusted source clock watermarks")
        self._clocks, self._source_clocks, self._live_boundaries = clocks, sources, boundaries

    def import_data(self, payload: dict[str, Any] | str, *, now: float, resume: bool = False) -> None:
        """
        Atomically replace memory with strictly validated bounded JSON history.

        Trusted local Store reloads can resume the current epoch's source clocks
        on the same host. Explicit user imports always default to a new epoch;
        imported monotonic stamps must not seed another host's live deduplication.
        """
        if type(resume) is not bool:
            _fail("resume must be a boolean")
        current = finite_number(now, "now", minimum=0)
        if isinstance(payload, str):
            if len(payload.encode("utf-8")) > self.max_bytes:
                _fail("import exceeds the history byte budget")
            try:
                payload = json.loads(
                    payload,
                    parse_constant=lambda value: _fail(f"invalid JSON constant {value}"),
                    object_pairs_hook=_unique_object,
                )
            except (json.JSONDecodeError, RecursionError) as err:
                message = "history import requires JSON data, not a file path"
                raise ValueError(message) from err
        fields = {"schema_version", "session_epoch", "selected_devices", "redacted", "frames"}
        if type(payload) is not dict or set(payload) != fields:
            _fail("unsupported history import fields")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
            _fail("unsupported history schema version")
        if payload["redacted"] is not True:
            _fail("only redacted histories can be imported")
        epoch = _integer(
            payload["session_epoch"], "session_epoch", 1, MAX_CLOCK_EPOCH if resume else MAX_CLOCK_EPOCH - 1
        )
        selection = self._selection(payload["selected_devices"])
        raw_frames = payload["frames"]
        if type(raw_frames) is not list or len(raw_frames) > self.max_frames:
            _fail("import exceeds the frame budget")
        if json_size(payload) > self.max_bytes:
            _fail("import exceeds the history byte budget")
        frames = [validate_frame(frame, now=current) for frame in raw_frames]
        if sum(len(frame["observations"]) for frame in frames) > self.max_rows:
            _fail("import exceeds the row budget")
        if frames and frames[-1]["captured_at"] - frames[0]["captured_at"] > self.window_s:
            _fail("import exceeds the time window")
        previous_capture = -1.0
        previous_sequence = 0
        previous_epoch = 0
        clocks = {}
        for frame in frames:
            if frame["sequence"] <= previous_sequence or frame["captured_at"] < previous_capture:
                _fail("import frame clocks must be ordered")
            if frame["session_epoch"] < previous_epoch or frame["session_epoch"] > epoch:
                _fail("import session epochs must be ordered and valid")
            previous_sequence, previous_capture, previous_epoch = (
                frame["sequence"],
                frame["captured_at"],
                frame["session_epoch"],
            )
            for row in frame["observations"]:
                labelled = any(
                    label["device_id"] == row["device_id"] and row["scanner_id"] in label["ranges"]
                    for label in frame["truth"]
                )
                if selection and row["device_id"] not in selection and not labelled:
                    _fail("imported observation is outside the selected devices")
                key = frame["session_epoch"], row["device_id"], row["scanner_id"]
                previous = clocks.get(key)
                if previous and (row["stamp"] <= previous[0] or row["observed"] < previous[1]):
                    _fail("import contains duplicate or backwards source clocks")
                clocks[key] = row["stamp"], row["observed"]
        self._validate_receiver_clocks(frames)
        imported_epoch = epoch if resume else epoch + 1
        # Numeric normalization and identifier redaction can enlarge valid raw
        # JSON. Apply the producer's canonical/redacted budget to detached data,
        # including the new live epoch's header, before replacing any history.
        normalized_budget = (
            sum(self._frame_budget(frame) for frame in frames)
            + max(0, len(frames) - 1)
            + self._header_size(session_epoch=imported_epoch, selected_devices=selection)
        )
        if normalized_budget > self.max_bytes:
            _fail("normalized import exceeds the history byte budget")
        self._frames = frames
        self.selected_devices = selection
        # Trusted local reloads retain evidence clocks. A user import instead
        # starts a distinct live epoch and never trusts another host's stamps.
        self.session_epoch = imported_epoch
        self._sequence = previous_sequence
        self._clocks = {
            (device, scanner): clock
            for (clock_epoch, device, scanner), clock in clocks.items()
            if resume and clock_epoch == epoch
        }
        self._source_clocks = {}
        self._live_boundaries = {}
        if resume:
            for frame in frames:
                if frame["session_epoch"] != epoch:
                    continue
                for row in frame["observations"]:
                    pair = self._source_key(row)
                    previous = self._source_clocks.get(pair, (0.0, 0.0))
                    self._source_clocks[pair] = max(previous[0], row["stamp"]), max(previous[1], row["observed"])
        self.dropped_rows = 0
        self._refresh_counts()
