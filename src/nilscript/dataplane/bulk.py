"""Bulk-write spine: walk a (possibly huge, export-derived) id set in bounded batches so heavy ops are
reliable, not one giant blind call. The caller's `do_batch` carries the governance (propose->commit->
gate) per batch; this spine adds the reliability envelope:

  • bounded   — fixed batch size, never the whole set at once;
  • resumable — skip ids already checkpointed (idempotent re-run after a crash);
  • stoppable — `should_stop` is honored BETWEEN batches (a clean halt, no partial work past it);
  • honest on failure — `on_error` policy is `skip` (report the bad batch, continue) or `stop` (halt).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BATCH_SIZE = 200


@dataclass
class BulkResult:
    processed: int = 0
    stopped: bool = False
    failed: list[list[Any]] = field(default_factory=list)


def _batches(items: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def run_bulk(
    ids: Sequence[Any],
    do_batch: Callable[[list[Any]], Any],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    should_stop: Callable[[], bool] | None = None,
    already_done: set[Any] | None = None,
    on_error: str = "skip",
    checkpoint: Callable[[list[Any]], None] | None = None,
) -> BulkResult:
    """Run `do_batch` over `ids` in batches. Returns a `BulkResult` (processed count, stop flag, failed
    batches). `do_batch` raising is governed by `on_error`: 'skip' records the batch and continues,
    'stop' halts. Already-checkpointed ids are skipped so a resume never re-applies committed work."""
    if on_error not in ("skip", "stop"):
        raise ValueError("on_error must be 'skip' or 'stop'")
    done = already_done or set()
    pending = [i for i in ids if i not in done]
    result = BulkResult()
    for batch in _batches(pending, batch_size):
        if should_stop is not None and should_stop():
            result.stopped = True
            break
        try:
            do_batch(batch)
        except Exception:  # noqa: BLE001 — the policy decides; we never silently swallow
            result.failed.append(batch)
            if on_error == "stop":
                result.stopped = True
                break
            continue
        result.processed += len(batch)
        if checkpoint is not None:
            checkpoint(batch)
    return result
