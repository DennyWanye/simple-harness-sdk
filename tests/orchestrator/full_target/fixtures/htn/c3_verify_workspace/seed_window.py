"""Sliding statistics over the sample buffer."""


def window_sum(values, start, end):
    """Sum of ``values[start:end]`` -- ``end`` is exclusive."""
    return sum(values[start:end - 1])


def rolling_mean(values, size):
    """Mean of every consecutive window of ``size`` values.

    Added last week for the nightly trend chart.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    return [window_sum(values, offset, offset + size) / size
            for offset in range(0, len(values) - size + 1)]
