# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math

import pytest

from simple_harness import (
    ContractValidationError,
    canonical_json,
    freeze_json,
    thaw_json,
    validate_json_value,
)


def test_nested_json_validates_and_has_stable_canonical_spelling() -> None:
    value = {"unicode": "你好", "array": [None, True, 2, 3.5], "object": {"b": 2, "a": 1}}
    validate_json_value(value)
    assert canonical_json(value) == (
        '{"array":[null,true,2,3.5],"object":{"a":1,"b":2},"unicode":"你好"}'
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ContractValidationError, match="finite JSON number") as error:
        validate_json_value({"value": value})
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize(
    "value",
    [b"secret", object(), {1: "not-a-string-key"}, ("tuple-is-not-input-json",)],
)
def test_non_json_values_are_rejected(value: object) -> None:
    with pytest.raises(ContractValidationError) as error:
        validate_json_value(value)
    assert error.value.code == "invalid_json"


def test_freeze_and_thaw_defensively_copy_nested_values() -> None:
    source = {"items": [{"value": 1}]}
    frozen = freeze_json(source)
    source["items"][0]["value"] = 99

    assert thaw_json(frozen) == {"items": [{"value": 1}]}
    with pytest.raises(TypeError):
        frozen["new"] = "forbidden"  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen["items"][0]["value"] = 2  # type: ignore[index]


def test_boolean_is_not_coerced_to_integer() -> None:
    assert canonical_json({"value": True}) == '{"value":true}'

