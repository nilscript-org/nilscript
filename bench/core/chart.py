"""Render a benchmark matrix to a dependency-free SVG bar chart (best practice: a committed,
regenerable image driven by the results file — diffable in git, no runtime deps).

    python bench/core/chart.py bench/safety/matrix.json bench/assets/injecagent_safety.svg

Shows, per (model, setting): the raw unauthorized-write rate vs the rate with NIL (≈0). The whole
point is visible at a glance — the NIL bars vanish while benign stays at 100%.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

W, H = 720, 380
PAD_L, PAD_B, PAD_T = 48, 64, 56
PLOT_H = H - PAD_B - PAD_T
# Light theme — white background, high-contrast ink, semantic red (raw risk) / green (NIL = 0).
BG, INK, MUTED, LINE, RAW, NIL = "#ffffff", "#0a0a0a", "#52525b", "#e4e4e7", "#dc2626", "#16a34a"


def render(matrix: dict) -> str:
    rows = matrix["rows"]
    ymax = max((r["uwr_raw"] for r in rows), default=0.05) or 0.05
    ymax = max(ymax * 1.25, 0.01)
    n = len(rows)
    group_w = (W - PAD_L - 24) / n
    bar_w = group_w * 0.28

    def y(v: float) -> float:
        return PAD_T + PLOT_H * (1 - v / ymax)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'font-family="ui-sans-serif,system-ui,sans-serif">',
        f'<rect width="{W}" height="{H}" fill="{BG}"/>',
        f'<text x="{PAD_L}" y="28" fill="{INK}" font-size="16" font-weight="600">'
        f'InjecAgent — unauthorized-write rate: raw vs NIL</text>',
        f'<text x="{PAD_L}" y="46" fill="{MUTED}" font-size="11">'
        f'{matrix["totals"]["evaluations"]} evaluations · benign task-success 100% in every arm · UWR via NIL = 0%</text>',
    ]
    # y gridlines (0, ymax/2, ymax)
    for frac in (0.0, 0.5, 1.0):
        v = ymax * frac
        yy = y(v)
        parts.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W-12}" y2="{yy:.1f}" stroke="{LINE}" stroke-width="1"/>')
        parts.append(f'<text x="{PAD_L-6}" y="{yy+3:.1f}" fill="{MUTED}" font-size="10" text-anchor="end">{v*100:.1f}%</text>')

    for i, r in enumerate(rows):
        gx = PAD_L + 8 + i * group_w
        cx = gx + group_w / 2
        # raw bar
        raw_h = PLOT_H * (r["uwr_raw"] / ymax)
        parts.append(f'<rect x="{cx-bar_w-3:.1f}" y="{y(r["uwr_raw"]):.1f}" width="{bar_w:.1f}" height="{raw_h:.1f}" fill="{RAW}" rx="2"/>')
        parts.append(f'<text x="{cx-bar_w/2-3:.1f}" y="{y(r["uwr_raw"])-4:.1f}" fill="{RAW}" font-size="10" text-anchor="middle">{r["uwr_raw"]*100:.2f}%</text>')
        # nil bar (≈0 → draw a 2px floor so it's visible as "≈0")
        nil_h = max(PLOT_H * (r["uwr_nil"] / ymax), 2)
        parts.append(f'<rect x="{cx+3:.1f}" y="{PAD_T+PLOT_H-nil_h:.1f}" width="{bar_w:.1f}" height="{nil_h:.1f}" fill="{NIL}" rx="2"/>')
        parts.append(f'<text x="{cx+bar_w/2+3:.1f}" y="{PAD_T+PLOT_H-nil_h-4:.1f}" fill="{NIL}" font-size="10" text-anchor="middle">0%</text>')
        # x labels
        model = r["model"].split("/")[-1]
        parts.append(f'<text x="{cx:.1f}" y="{H-PAD_B+18}" fill="{INK}" font-size="10.5" text-anchor="middle">{model}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{H-PAD_B+33}" fill="{MUTED}" font-size="10" text-anchor="middle">{r["setting"]}</text>')

    # legend
    ly = H - 18
    parts += [
        f'<rect x="{PAD_L}" y="{ly-9}" width="11" height="11" fill="{RAW}" rx="2"/>',
        f'<text x="{PAD_L+16}" y="{ly}" fill="{MUTED}" font-size="11">raw (ungated)</text>',
        f'<rect x="{PAD_L+130}" y="{ly-9}" width="11" height="11" fill="{NIL}" rx="2"/>',
        f'<text x="{PAD_L+146}" y="{ly}" fill="{MUTED}" font-size="11">with NIL (propose→approve→commit)</text>',
        "</svg>",
    ]
    return "\n".join(parts)


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "bench/safety/matrix.json")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "bench/assets/injecagent_safety.svg")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(json.loads(src.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
