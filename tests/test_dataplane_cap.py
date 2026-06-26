"""The byte-cap invariant: an oversized read is REFUSED, never truncated.

A naive cap that returns the first N bytes silently drops the row the agent needed (the match at
record #500,000) and yields a confident wrong "not found". The cap must instead refuse — correctness
comes from server-side selection, the cap only forbids the silent lie.
"""

from __future__ import annotations

import pytest

from nilscript.dataplane import BYTE_CAP, ResultTooLarge, enforce_byte_cap


def test_oversized_result_is_refused_not_truncated() -> None:
    # ~1 MB page — far over any sane cap.
    items = [{"id": i, "blob": "x" * 1000} for i in range(1000)]
    page = {"items": items, "total": len(items)}

    with pytest.raises(ResultTooLarge) as exc:
        enforce_byte_cap(page, cap=200_000)

    # The refusal is an honest answer: it carries the real size and actionable guidance,
    # and crucially it does NOT hand back a truncated subset of the rows.
    assert exc.value.code == "RESULT_TOO_LARGE"
    assert exc.value.bytes > 200_000
    assert exc.value.cap == 200_000
    guidance = exc.value.message.lower()
    assert "narrow" in guidance or "export" in guidance


def test_within_cap_result_passes_through_unchanged() -> None:
    page = {"items": [{"id": 1, "name": "رغد عبدالله"}], "total": 1}
    assert enforce_byte_cap(page, cap=BYTE_CAP) is page
