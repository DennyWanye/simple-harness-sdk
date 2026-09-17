# Diagnosis: failing `window_sum` public test

## Reproduction

Command: pytest `tests/test_public_window.py` (workspace `run_tests`).

Result before the slice fix:

- `test_window_sum_of_an_empty_range_is_zero` **PASSED**
- `test_window_sum_covers_the_whole_requested_range` **FAILED**
  - assertion: `window_sum([1, 2, 3, 4, 5], 1, 4) == 9`
  - actual: `5`
  - expected: `9`

## Documented contract vs implementation

`stats/window.py` `window_sum(values, start, end)`:

- Docstring: sum of `values[start:end]`; `end` is exclusive (half-open).
- Buggy body: `return sum(values[start:end - 1])`

That slice is one element short of the documented half-open range.

## Why the failing assertion is 5 instead of 9

For `values = [1, 2, 3, 4, 5]`, `start=1`, `end=4`:

| slice | elements | sum |
| --- | --- | --- |
| documented / expected `values[1:4]` | `[2, 3, 4]` | `9` |
| implemented `values[1:3]` (`end - 1`) | `[2, 3]` | `5` |

This matches ops report (2026-09-14): nightly totals short by one sample.

## Why the empty-range test still passed

`window_sum([1, 2, 3], 2, 2)` used `values[2:1]`, which is empty, so `sum` is `0`.
The off-by-one does not change empty `start == end` results.

## Rolling window feature (must be preserved)

`rolling_mean(values, size)` is still present and used for the nightly trend chart.
It is **not** imported by the public tests. It computes:

```text
window_sum(values, offset, offset + size) / size
```

for `offset` in `range(0, len(values) - size + 1)`, and raises `ValueError` if `size <= 0`.

Because it calls `window_sum`, each window mean previously excluded the last sample of that window. A correct `window_sum` half-open slice (`values[start:end]`) restores both the public test and the last sample of each rolling window. `rolling_mean` itself was not deleted or rewritten away.

## Fix applied

Change `window_sum` from `sum(values[start:end - 1])` to `sum(values[start:end])`. Keep `rolling_mean`.
