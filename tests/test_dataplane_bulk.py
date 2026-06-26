"""Bulk-write spine: heavy ops (delete-many / update-many / email-all) run as bounded batches, never
one giant blind call. Each batch is governed by the caller's callback (propose->commit->gate); the
spine adds reliability — resumable (skip checkpointed ids), STOPpable between batches, and an explicit
partial-failure policy. Same spine powers "do X to all of them" from an export id-set.
"""

from __future__ import annotations

import pytest

from nilscript.dataplane import run_bulk


def test_runs_every_id_in_order_in_batches() -> None:
    seen: list[list[int]] = []
    result = run_bulk(list(range(10)), lambda batch: seen.append(list(batch)), batch_size=3)
    assert seen == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]
    assert result.processed == 10
    assert result.stopped is False


def test_stop_is_honored_between_batches() -> None:
    calls: list[list[int]] = []
    flag = {"stop": False}

    def do(batch: list[int]) -> None:
        calls.append(list(batch))
        if len(calls) == 2:
            flag["stop"] = True  # request stop after the 2nd batch

    result = run_bulk(list(range(12)), do, batch_size=3, should_stop=lambda: flag["stop"])
    assert result.stopped is True
    assert result.processed == 6  # exactly the two completed batches, no partial work past the stop


def test_resume_skips_already_checkpointed_ids() -> None:
    touched: list[int] = []
    result = run_bulk(
        list(range(10)), lambda batch: touched.extend(batch), batch_size=3, already_done={0, 1, 2, 3}
    )
    assert touched == [4, 5, 6, 7, 8, 9]  # the first four never re-applied (idempotent resume)
    assert result.processed == 6


def test_partial_failure_skips_the_bad_batch_and_reports_it() -> None:
    def do(batch: list[int]) -> None:
        if 5 in batch:
            raise RuntimeError("backend rejected the batch")

    result = run_bulk(list(range(9)), do, batch_size=3, on_error="skip")
    assert result.failed == [[3, 4, 5]]      # the offending batch is reported, not swallowed
    assert result.processed == 6             # the other two batches still completed


def test_partial_failure_stop_policy_halts_on_first_error() -> None:
    def do(batch: list[int]) -> None:
        if 4 in batch:
            raise RuntimeError("boom")

    result = run_bulk(list(range(9)), do, batch_size=3, on_error="stop")
    assert result.stopped is True
    assert result.processed == 3             # only the first clean batch; halted at the failure
