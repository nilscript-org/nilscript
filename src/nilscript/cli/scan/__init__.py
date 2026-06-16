"""The Capability Scan (plan §3.2) — discover a system's hidden requirements once, encode them in a
shareable manifest so no developer re-learns them by collision.

The probe loop (live `--url` mode) is layered on top of the same engine the deterministic replay
mode uses; the intelligence — turning a native error into a structured requirement — lives in
`inference.py` and is fully testable without a live system.
"""

from __future__ import annotations

from nilscript.cli.scan.inference import Finding, build_manifest, infer

__all__ = ["Finding", "build_manifest", "infer"]
