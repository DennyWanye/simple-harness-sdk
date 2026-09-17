"""Visible tests for the sliding statistics."""
from stats.window import window_sum


def test_window_sum_of_an_empty_range_is_zero():
    assert window_sum([1, 2, 3], 2, 2) == 0


def test_window_sum_covers_the_whole_requested_range():
    # Reported by ops on 2026-09-14: the nightly totals are short by one sample.
    assert window_sum([1, 2, 3, 4, 5], 1, 4) == 9
