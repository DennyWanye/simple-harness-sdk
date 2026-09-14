# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Descriptive aggregation for a frozen AppWorld experiment receipt.

This is deliberately a receipt reader, not an evaluator: it makes no provider,
model, network, or benchmark calls.  It refuses identities that cannot be tied
back to the frozen manifest and reports incomplete units instead of removing
them from a paired comparison.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from itertools import combinations
from math import isfinite
from random import Random
from statistics import fmean
from typing import Any, cast

from .experiment import ARMS, ArmSpec, ExperimentBudget, ExperimentManifest

_TERMINAL = frozenset(("success", "failure", "deadline", "interrupted"))
_COST_PAIRING_INCOMPLETE = (
    "utility pairing may be complete, but every arm needs complete actual usage "
    "and known counters before a paired cost comparison is available"
)
_BOOTSTRAP_METHOD = "task-block-bootstrap-percentile-linear-v1"
_MIN_BOOTSTRAP_SAMPLES = 100
_MAX_BOOTSTRAP_SAMPLES = 100_000
_MAX_BOOTSTRAP_SEED = 2**64 - 1


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _number(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or not isfinite(value)
    ):
        return None
    return float(value)


def _nonnegative_integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _matrix_identity_sha256(identity: Mapping[str, Any]) -> str:
    """Hash every persisted identity field without copying possible URLs into the report."""
    try:
        canonical = json.dumps(
            identity, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ValueError("matrix identity must be canonical JSON") from error
    return sha256(canonical.encode("utf-8")).hexdigest()


def _usage_is_complete(matrix: Mapping[str, Any]) -> bool:
    usage = _mapping(matrix.get("usage"), "matrix.usage")
    unknown = _nonnegative_integer(
        usage.get("unknown_usage_calls"), "matrix.usage.unknown_usage_calls"
    )
    complete = _bool(usage.get("actual_tokens_complete"))
    if complete is None:
        raise ValueError("matrix.usage.actual_tokens_complete must be a boolean")
    if complete != (unknown == 0):
        raise ValueError("matrix usage completeness contradicts unknown_usage_calls")
    return complete


def _has_complete_cost_counters(matrix: Mapping[str, Any]) -> bool:
    """Whether a complete-usage row also carries every actual counter needed for cost work."""
    usage = _mapping(matrix.get("usage"), "matrix.usage")
    counters = usage.get("known_counters")
    if not isinstance(counters, Mapping):
        return False
    names = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "calls",
        "peak_physical_slots",
    )
    if any(type(counters.get(name)) is not int or counters[name] < 0 for name in names):
        return False
    return counters["total_tokens"] == counters["input_tokens"] + counters["output_tokens"]


def _manifest(receipt: Mapping[str, Any]) -> ExperimentManifest:
    raw = _mapping(receipt.get("manifest"), "receipt.manifest")
    try:
        manifest = ExperimentManifest(
            experiment_id=raw["experiment_id"],
            provider=raw["provider"],
            model=raw["model"],
            budget=ExperimentBudget(**_mapping(raw["budget"], "receipt.manifest.budget")),
            task_ids=tuple(raw["task_ids"]),
            repetitions=raw["repetitions"],
            seed=raw["seed"],
            physical_slots=raw["physical_slots"],
            arms=tuple(ArmSpec(**_mapping(arm, "receipt.manifest.arms[]")) for arm in raw["arms"]),
            schedule_version=raw.get("schedule_version", "latin-square-v1"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid frozen manifest") from error
    if receipt.get("manifest_sha256") != manifest.fingerprint:
        raise ValueError("receipt manifest fingerprint mismatch")
    return manifest


def _identity(identity: Mapping[str, Any], manifest: ExperimentManifest) -> dict[str, str]:
    if identity.get("manifest_sha256") != manifest.fingerprint:
        raise ValueError("matrix identity manifest mismatch")
    result = {}
    for field in ("source_fingerprint", "profile_fingerprint", "config_fingerprint"):
        value = identity.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"matrix identity missing {field}")
        result[field] = value
    return result


def _validate_runs(
    receipt: Mapping[str, Any], manifest: ExperimentManifest
) -> list[Mapping[str, Any]]:
    rows = receipt.get("runs")
    if not isinstance(rows, list) or len(rows) != len(manifest.runs()):
        raise ValueError("receipt runs do not match frozen manifest")
    expected = [
        {
            "run_id": run.run_id,
            "arm": run.arm,
            "task_id": run.task_id,
            "repetition": run.repetition,
            "seed": run.seed,
        }
        for run in manifest.runs()
    ]
    units: set[tuple[str, int, str]] = set()
    checked = []
    for row, run in zip(rows, expected, strict=True):
        record = _mapping(row, "receipt.runs[]")
        if dict(_mapping(record.get("run"), "receipt.runs[].run")) != run:
            raise ValueError("receipt run identity mismatch")
        unit = (
            cast(str, run["task_id"]),
            cast(int, run["repetition"]),
            cast(str, run["arm"]),
        )
        if unit in units:
            raise ValueError("duplicate task/repetition/arm unit")
        units.add(unit)
        if record.get("status") not in _TERMINAL | {"pending", "running"}:
            raise ValueError("unknown receipt run status")
        counters = record.get("counters")
        if counters is not None:
            counter = _mapping(counters, "receipt.runs[].counters")
            if (counter.get("provider"), counter.get("model")) != (
                manifest.provider,
                manifest.model,
            ):
                raise ValueError("mixed provider/model identities")
        matrix = record.get("matrix")
        if isinstance(matrix, Mapping):
            _usage_is_complete(matrix)
            usage = _mapping(matrix.get("usage"), "matrix.usage")
            known = usage.get("known_counters")
            if isinstance(known, Mapping) and (known.get("provider"), known.get("model")) != (
                manifest.provider,
                manifest.model,
            ):
                raise ValueError("mixed provider/model identities in known counters")
        checked.append(record)
    return checked


def _measurement(values: list[float]) -> dict[str, float | int | None]:
    return {
        "known": len(values),
        "sum": sum(values) if values else None,
        "mean": fmean(values) if values else None,
    }


def _bootstrap_analysis_identity(
    *,
    matrix_identity_sha256: str,
    samples: int | None,
    seed: int,
    confidence_level: float,
) -> dict[str, Any] | None:
    if samples is None:
        return None
    if type(samples) is not int or not _MIN_BOOTSTRAP_SAMPLES <= samples <= _MAX_BOOTSTRAP_SAMPLES:
        raise ValueError(
            f"bootstrap_samples must be an integer from {_MIN_BOOTSTRAP_SAMPLES} "
            f"through {_MAX_BOOTSTRAP_SAMPLES}"
        )
    if type(seed) is not int or not 0 <= seed <= _MAX_BOOTSTRAP_SEED:
        raise ValueError(f"bootstrap_seed must be an integer from 0 through {_MAX_BOOTSTRAP_SEED}")
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, (int, float))
        or not isfinite(confidence_level)
        or not 0.5 <= confidence_level < 1.0
    ):
        raise ValueError("confidence_level must be finite and in [0.5, 1.0)")
    return {
        "matrix_identity_sha256": matrix_identity_sha256,
        "uncertainty_method": _BOOTSTRAP_METHOD,
        "analysis_unit": "task",
        "bootstrap_samples": samples,
        "bootstrap_seed": seed,
        "confidence_level": float(confidence_level),
    }


def _linear_percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _task_block_bootstrap_interval(
    task_paired_deltas: Mapping[str, float],
    *,
    samples: int,
    seed: int,
    confidence_level: float,
) -> dict[str, float]:
    """Resample whole task means; repetitions have already been averaged within task."""
    deltas = tuple(task_paired_deltas.values())
    generator = Random(seed)
    estimates = [
        fmean(deltas[generator.randrange(len(deltas))] for _ in deltas) for _ in range(samples)
    ]
    tail = (1.0 - confidence_level) / 2.0
    return {
        "lower": _linear_percentile(estimates, tail),
        "upper": _linear_percentile(estimates, 1.0 - tail),
    }


def _arm_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    matrices = [
        _mapping(row["matrix"], "receipt.runs[].matrix")
        if isinstance(row.get("matrix"), Mapping)
        else None
        for row in rows
    ]

    def count(field: str) -> int:
        return sum(_bool(matrix.get(field)) is True for matrix in matrices if matrix is not None)

    token_values: dict[str, list[float]] = {
        name: [] for name in ("input_tokens", "output_tokens", "total_tokens", "calls")
    }
    latency: list[float] = []
    cache: list[float] = []
    uncached: list[float] = []
    cache_available = True
    for row, matrix in zip(rows, matrices, strict=True):
        if (elapsed := _number(row.get("elapsed_seconds"))) is not None:
            latency.append(elapsed)
        counters = matrix.get("usage", {}).get("known_counters") if matrix else row.get("counters")
        if isinstance(counters, Mapping):
            for name, values in token_values.items():
                if (value := _number(counters.get(name))) is not None:
                    values.append(value)
        calls = matrix.get("provider_calls") if matrix else None
        if not isinstance(calls, list):
            cache_available = False
            continue
        input_total = 0.0
        cache_total = 0.0
        for call in calls:
            if (
                not isinstance(call, Mapping)
                or (used := _number(call.get("input_tokens"))) is None
                or (cached := _number(call.get("cache_tokens"))) is None
                or cached > used
            ):
                cache_available = False
                break
            input_total += used
            cache_total += cached
        else:
            cache.append(cache_total)
            uncached.append(input_total - cache_total)
    unknown = sum(
        row.get("status") == "pending"
        or matrix is None
        or _bool(matrix.get("valid_success")) is None
        for row, matrix in zip(rows, matrices, strict=True)
    )
    return {
        "attempted": sum(row.get("status") != "pending" for row in rows),
        "terminal": sum(row.get("status") in _TERMINAL for row in rows),
        "pending": sum(row.get("status") == "pending" for row in rows),
        "valid_success": count("valid_success"),
        "official_success": count("official_success"),
        "false_completion": count("false_completion"),
        "unknown": unknown,
        "unknown_usage_calls": sum(
            _nonnegative_integer(
                _mapping(matrix.get("usage"), "matrix.usage").get("unknown_usage_calls"),
                "matrix.usage.unknown_usage_calls",
            )
            for matrix in matrices
            if matrix is not None
        ),
        "usage_complete": all(
            matrix is not None and _usage_is_complete(matrix) for matrix in matrices
        ),
        "token_lower_bounds": {name: _measurement(values) for name, values in token_values.items()},
        "cached_tokens": _measurement(cache) if cache_available else None,
        "uncached_input_tokens": _measurement(uncached) if cache_available else None,
        "latency_seconds": _measurement(latency),
    }


def analyze_frozen_matrix(
    receipt: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    task_families: Mapping[str, str] | None = None,
    bootstrap_samples: int | None = None,
    bootstrap_seed: int = 0,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Return deterministic, descriptive task-level results for one frozen matrix.

    ``identity`` is the adjacent ``appworld-matrix.json`` document.  Optional
    ``task_families`` lets callers aggregate task deltas without inventing a
    family from a task identifier.  Setting ``bootstrap_samples`` adds a
    reproducible percentile interval by resampling whole task means.
    """
    frozen = _mapping(receipt, "receipt")
    manifest = _manifest(frozen)
    full_identity = _mapping(identity, "identity")
    identity_fields = _identity(full_identity, manifest)
    if frozen.get("matrix_identity") != identity:
        raise ValueError("receipt source/profile/config identity mismatch")
    identity_sha256 = _matrix_identity_sha256(full_identity)
    analysis_identity = _bootstrap_analysis_identity(
        matrix_identity_sha256=identity_sha256,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
        confidence_level=confidence_level,
    )
    if analysis_identity is not None and len(manifest.task_ids) < 2:
        raise ValueError("task-block bootstrap requires at least 2 independent tasks")
    rows = _validate_runs(frozen, manifest)
    if task_families is not None and set(task_families) != set(manifest.task_ids):
        raise ValueError("task_families must name every frozen task exactly once")

    by_arm: dict[str, list[Mapping[str, Any]]] = {arm: [] for arm in ARMS}
    units: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        run = _mapping(row["run"], "receipt.runs[].run")
        by_arm[run["arm"]].append(row)
        units.setdefault((run["task_id"], run["repetition"]), {})[run["arm"]] = row
    utility_complete = all(
        len(unit) == len(ARMS)
        and all(
            row.get("status") in _TERMINAL
            and isinstance(row.get("matrix"), Mapping)
            and _bool(row["matrix"].get("valid_success")) is not None
            for row in unit.values()
        )
        for unit in units.values()
    )
    cost_complete = utility_complete and all(
        isinstance(row.get("matrix"), Mapping)
        and _usage_is_complete(_mapping(row.get("matrix"), "receipt.runs[].matrix"))
        and _has_complete_cost_counters(_mapping(row.get("matrix"), "receipt.runs[].matrix"))
        for unit in units.values()
        for row in unit.values()
    )
    incomplete = [
        {
            "task_id": task,
            "repetition": repetition,
            "missing_arms": [arm for arm in ARMS if arm not in unit],
            "unscored_arms": [
                arm
                for arm, row in unit.items()
                if not isinstance(row.get("matrix"), Mapping)
                or _bool(row["matrix"].get("valid_success")) is None
            ],
            "nonterminal_arms": [
                arm for arm in ARMS if arm in unit and unit[arm].get("status") not in _TERMINAL
            ],
        }
        for (task, repetition), unit in sorted(units.items())
        if len(unit) != len(ARMS)
        or any(
            row.get("status") not in _TERMINAL
            or not isinstance(row.get("matrix"), Mapping)
            or _bool(row["matrix"].get("valid_success")) is None
            for row in unit.values()
        )
    ]
    pairs: dict[str, Any] = {}
    if utility_complete:
        for left, right in combinations(ARMS, 2):
            task_deltas: dict[str, float] = {}
            for task in manifest.task_ids:
                differences = []
                for repetition in range(manifest.repetitions):
                    left_value = _bool(
                        _mapping(units[(task, repetition)][left].get("matrix"), "matrix").get(
                            "valid_success"
                        )
                    )
                    right_value = _bool(
                        _mapping(units[(task, repetition)][right].get("matrix"), "matrix").get(
                            "valid_success"
                        )
                    )
                    if left_value is None or right_value is None:
                        raise ValueError("terminal paired unit has unknown valid_success")
                    differences.append(int(left_value) - int(right_value))
                task_deltas[task] = fmean(differences)
            family_deltas: dict[str, list[float]] = {}
            if task_families is not None:
                for task, delta in task_deltas.items():
                    family_deltas.setdefault(task_families[task], []).append(delta)
            pairs[f"{left}-{right}"] = {
                "left_arm": left,
                "right_arm": right,
                "task_paired_deltas": task_deltas,
                "task_count": len(task_deltas),
                "mean_task_paired_delta": fmean(task_deltas.values()),
                "family_paired_deltas": {
                    family: fmean(values) for family, values in sorted(family_deltas.items())
                },
            }
        if analysis_identity is not None:
            for comparison in pairs.values():
                comparison["task_bootstrap_percentile_interval"] = _task_block_bootstrap_interval(
                    comparison["task_paired_deltas"],
                    samples=analysis_identity["bootstrap_samples"],
                    seed=analysis_identity["bootstrap_seed"],
                    confidence_level=analysis_identity["confidence_level"],
                )
    paired: dict[str, Any] = {
        # ``confirmed`` remains the existing utility/success comparison gate.
        # Complete scoring is meaningful even when a separate cost observation
        # is unavailable; callers must not turn that into a priced comparison.
        "confirmed": utility_complete,
        "utility_confirmed": utility_complete,
        "incomplete_units": incomplete,
        "reason": None
        if utility_complete
        else "all arms must be terminal and scored for every task/repetition",
        "comparisons": pairs,
        "cost_pairing": {
            "confirmed": cost_complete,
            "reason": None if cost_complete else _COST_PAIRING_INCOMPLETE,
        },
    }
    if analysis_identity is not None:
        paired["uncertainty"] = {
            "confirmed": utility_complete,
            "reason": (
                None
                if utility_complete
                else "complete utility pairing is required for bootstrap intervals"
            ),
            "small_sample_caveat": (
                f"Only {len(manifest.task_ids)} independent tasks are resampled; repetitions "
                "are averaged within task and do not increase the independent-unit count. "
                "The interval is descriptive, does not establish benefit, and provides no p-value."
            ),
        }
    report = {
        "schema_version": 1,
        "identity": {
            "manifest_sha256": manifest.fingerprint,
            "provider": manifest.provider,
            "model": manifest.model,
            "matrix_identity_sha256": identity_sha256,
            **identity_fields,
        },
        "design": {
            "task_count": len(manifest.task_ids),
            "repetitions": manifest.repetitions,
            "arms": list(ARMS),
            "planned_runs": len(rows),
            "analysis_unit": "task",
        },
        "arms": {arm: _arm_summary(by_arm[arm]) for arm in ARMS},
        "paired": paired,
    }
    if analysis_identity is not None:
        report["analysis_identity"] = analysis_identity
    return report


__all__ = ("analyze_frozen_matrix",)
