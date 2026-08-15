from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable

import pytest

import rebuild_irs990_slim_clean as rebuild


def _square(value):
    return value * value


class RecordingFuture:
    def __init__(self, owner, function: Callable[..., Any], args):
        self.owner = owner
        self.function = function
        self.args = args
        self.done = False
        self.cancelled = False

    def result(self):
        assert not self.cancelled
        try:
            return self.function(*self.args)
        finally:
            if not self.done:
                self.done = True
                self.owner.in_flight -= 1

    def cancel(self):
        if self.done or self.cancelled:
            return False
        self.cancelled = True
        self.owner.in_flight -= 1
        self.owner.cancelled_count += 1
        return True


class RecordingExecutor:
    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.cancelled_count = 0
        self.submitted_batches = []

    def submit(self, function, *args):
        batch = list(args[-1])
        self.submitted_batches.append(batch)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        return RecordingFuture(self, function, args)


class CountingIterable:
    def __init__(self, count: int):
        self.count = count
        self.consumed = 0

    def __iter__(self):
        for value in range(self.count):
            self.consumed += 1
            yield value


def test_bounded_executor_map_preserves_order_chunks_and_inflight_bound():
    executor = RecordingExecutor()

    actual = list(
        rebuild.bounded_ordered_executor_map(
            executor,
            lambda value: value * 10,
            range(17),
            chunksize=3,
            max_pending_batches=4,
        )
    )

    assert actual == [value * 10 for value in range(17)]
    assert executor.max_in_flight == 4
    assert executor.in_flight == 0
    assert [len(batch) for batch in executor.submitted_batches] == [3, 3, 3, 3, 3, 2]


def test_bounded_executor_map_runs_with_real_spawned_processes():
    with ProcessPoolExecutor(max_workers=2) as executor:
        actual = list(
            rebuild.bounded_ordered_executor_map(
                executor,
                _square,
                range(25),
                chunksize=4,
                max_pending_batches=4,
            )
        )

    assert actual == [value * value for value in range(25)]


def test_bounded_executor_map_propagates_error_and_cancels_pending():
    executor = RecordingExecutor()

    def fail_on_one(value):
        if value == 1:
            raise ValueError("fixture failure")
        return value

    with pytest.raises(ValueError, match="fixture failure"):
        list(
            rebuild.bounded_ordered_executor_map(
                executor,
                fail_on_one,
                range(100),
                chunksize=2,
                max_pending_batches=3,
            )
        )

    assert executor.max_in_flight == 3
    assert executor.cancelled_count == 2
    assert executor.in_flight == 0
    assert len(executor.submitted_batches) == 3


def test_bounded_executor_map_does_not_eagerly_consume_large_iterable():
    executor = RecordingExecutor()
    values = CountingIterable(10_000_000)
    mapped = rebuild.bounded_ordered_executor_map(
        executor,
        lambda value: value,
        values,
        chunksize=5,
        max_pending_batches=4,
    )

    assert values.consumed == 0
    assert next(mapped) == 0
    assert values.consumed == 20
    assert executor.max_in_flight == 4
    mapped.close()

    assert values.consumed == 20
    assert executor.cancelled_count == 3
    assert executor.in_flight == 0


@pytest.mark.parametrize("chunksize,pending", [(0, 1), (1, 0)])
def test_bounded_executor_map_rejects_invalid_bounds(chunksize: int, pending: int):
    mapped = rebuild.bounded_ordered_executor_map(
        RecordingExecutor(),
        lambda value: value,
        (),
        chunksize=chunksize,
        max_pending_batches=pending,
    )
    with pytest.raises(ValueError):
        next(mapped)
