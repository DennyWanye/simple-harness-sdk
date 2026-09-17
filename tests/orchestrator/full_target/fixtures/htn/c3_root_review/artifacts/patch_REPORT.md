# Fix: `window_sum` off-by-one slice

## What changed

In `stats/window.py`, `window_sum` now sums `values[start:end]` instead of `values[start:end - 1]`.

`rolling_mean` is unchanged: it still walks consecutive windows of `size` and divides each `window_sum(values, offset, offset + size)` by `size`, and still raises `ValueError` when `size <= 0`.

## Why

The docstring and public contract treat `end` as exclusive (half-open). The extra `- 1` dropped the last sample in every non-empty window.

That matches the failing public case: `window_sum([1, 2, 3, 4, 5], 1, 4)` is the sum of `[2, 3, 4]` = 9, not `[2, 3]` = 5. Empty ranges (`start == end`) already summed to 0, so they hid the bug.

Because `rolling_mean` calls `window_sum`, the same slice restore also includes the last sample of each rolling window used by the nightly trend chart.

## Not done / out of scope

- No extra tests were added for `rolling_mean` (it is not imported by the public suite).
- No other modules were changed.
