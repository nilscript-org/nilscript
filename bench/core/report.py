"""One report schema for every NIL benchmark axis — JSON + markdown, stamped for reproducibility.

Every published number flows through here so model snapshot, dataset/benchmark commit, and the
NIL kernel version are always attached (release-plan §7 credibility rule). See ../README.md.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Stamp:
    """Reproducibility provenance — never publish a number without it."""

    kernel_version: str
    model: str = "n/a (no LLM in the loop)"
    dataset_commit: str = "n/a"
    seed: int | None = None
    notes: str = ""


@dataclass
class BenchResult:
    axis: str  # "task-success" | "safety" | "conformance" | "performance"
    name: str
    metrics: dict[str, Any]
    stamp: Stamp
    arms: dict[str, dict[str, Any]] = field(default_factory=dict)  # ARM_RAW / ARM_NIL deltas

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, default=str)

    def to_markdown(self) -> str:
        lines = [f"### {self.name}  ·  axis: {self.axis}", ""]
        for k, v in self.metrics.items():
            lines.append(f"- **{k}**: {v}")
        if self.arms:
            lines += ["", "| arm | " + " | ".join(next(iter(self.arms.values())).keys()) + " |",
                      "|---|" + "---|" * len(next(iter(self.arms.values()))) ]
            for arm, m in self.arms.items():
                lines.append(f"| {arm} | " + " | ".join(str(x) for x in m.values()) + " |")
        s = self.stamp
        lines += ["", f"> kernel {s.kernel_version} · model {s.model} · dataset {s.dataset_commit}"
                      f" · seed {s.seed}" + (f" · {s.notes}" if s.notes else "")]
        return "\n".join(lines)


def pass_k(per_task_runs: list[list[bool]]) -> dict[int, float]:
    """τ-bench's reliability metric. Input: per task, a list of k boolean trial outcomes.

    pass^k for a task = 1.0 iff ALL k trials passed; the reported pass^k is that averaged over tasks.
    We compute it for every k' from 1..min_trials so the decay curve is visible (the honest story:
    a deterministic shim holds at 1.0; an LLM-in-the-loop arm decays — that contrast is the point).
    """
    if not per_task_runs:
        return {}
    kmax = min(len(r) for r in per_task_runs)
    out: dict[int, float] = {}
    for k in range(1, kmax + 1):
        passing = sum(1 for runs in per_task_runs if all(runs[:k]))
        out[k] = round(passing / len(per_task_runs), 4)
    return out
