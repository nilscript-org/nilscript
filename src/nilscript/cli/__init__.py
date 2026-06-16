"""nilscript CLI — general tooling to build and verify NIL adapters.

Reads the bundled standard only; contains zero specifics of any backend. The same tools let
any developer, anywhere, build an adapter for their own system from the standard alone:

    pip install nilscript[cli]
    nilscript verbs                 # the verb catalog (deprecated verbs flagged)
    nilscript profile <verb>        # a verb's arg-schema
    nilscript export-openapi        # the five-endpoint API surface as OpenAPI 3.1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nilscript.cli._openapi import build_openapi
from nilscript.cli._spec import SPEC_VERSION, all_verbs, load_profile


def _verb_markers(verb) -> str:  # type: ignore[no-untyped-def]
    parts = []
    if verb.deprecated:
        ref = f" — {verb.gap_ref}" if verb.gap_ref else ""
        parts.append(f"[DEPRECATED{ref} — not scaffolded]")
    if verb.tier_floor:
        parts.append(f"[floor {verb.tier_floor}]")
    return ("  " + "  ".join(parts)) if parts else ""


def _cmd_verbs(args: argparse.Namespace) -> int:
    verbs = all_verbs()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "verb": v.name,
                        "deprecated": v.deprecated,
                        "gap_ref": v.gap_ref,
                        "tier_floor": v.tier_floor,
                        "required": list(v.required),
                    }
                    for v in verbs
                ],
                indent=2,
            )
        )
        return 0
    width = max((len(v.name) for v in verbs), default=0)
    deprecated = sum(1 for v in verbs if v.deprecated)
    print(
        f"{len(verbs)} verbs in the NIL standard (v{SPEC_VERSION}) — "
        f"{len(verbs) - deprecated} active, {deprecated} deprecated (excluded from scaffolding):\n"
    )
    for v in verbs:
        required = ", ".join(v.required) if v.required else "—"
        print(f"  {v.name:<{width}}  required: {required}{_verb_markers(v)}")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    profile = load_profile(args.verb)
    if profile is None:
        print(f"unknown verb: {args.verb!r}", file=sys.stderr)
        print("run `nilscript verbs` to list the catalog.", file=sys.stderr)
        return 2
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    return 0


def _cmd_export_openapi(args: argparse.Namespace) -> int:
    doc = build_openapi()
    if args.format == "yaml":
        try:
            import yaml
        except ModuleNotFoundError:
            print(
                "YAML output needs PyYAML (pip install nilscript[cli]); emitting JSON instead.",
                file=sys.stderr,
            )
            text = json.dumps(doc, indent=2)
        else:
            text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(doc, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def _cmd_scaffold_shim(args: argparse.Namespace) -> int:
    from nilscript.cli.scaffold import scaffold_shim

    dest = Path(args.dest)
    try:
        root = scaffold_shim(args.name, dest, lang=args.lang)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"scaffolded {args.name} at {root}", file=sys.stderr)
    print(f"  next: fill src/{root.name.replace('-', '_')}/translate.py + system.py, then `pytest`", file=sys.stderr)
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from nilscript.cli.scan import build_manifest

    if not args.replay:
        # Live probing writes to a real system and must be --safe (plan §8); not wired in the MVP.
        print(
            "live --url probing is not yet wired (plan Phase-2 live mode). Use --replay FILE with "
            "captured native errors to build the manifest deterministically.",
            file=sys.stderr,
        )
        return 2

    payload = json.loads(Path(args.replay).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        samples, system, hints = payload, args.system, None
    else:
        samples = payload.get("samples", [])
        system = args.system or payload.get("system")
        hints = payload.get("resolve_hints")
    if not system:
        print("a system name is required (--system NAME or `system` in the replay file)", file=sys.stderr)
        return 2

    manifest = build_manifest(system, samples, resolve_hints=hints)
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    from nilscript.cli.manifest import shareable_violations, strip_instance, validate

    manifest = json.loads(Path(args.file).read_text(encoding="utf-8"))

    if args.action == "validate":
        errors = validate(manifest)
        leaks = shareable_violations(manifest)
        for err in errors:
            print(f"INVALID: {err}", file=sys.stderr)
        for leak in leaks:
            print(f"LEAK: {leak}", file=sys.stderr)
        if errors:
            return 1
        if leaks:
            print("shape OK, but NOT shareable (instance/secret leakage) — see LEAK lines above", file=sys.stderr)
            return 1
        print(f"valid and shareable: {manifest.get('system')} ({len(manifest.get('verbs', {}))} verbs)")
        return 0

    if args.action == "strip":
        shared = strip_instance(manifest)
        text = json.dumps(shared, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote shareable manifest to {args.output}", file=sys.stderr)
        else:
            print(text)
        return 0

    # show
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nilscript",
        description="Tooling to build and verify NIL adapters from the standard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_verbs = sub.add_parser("verbs", help="list the verb catalog from the standard")
    p_verbs.add_argument("--json", action="store_true", help="machine-readable output")
    p_verbs.set_defaults(func=_cmd_verbs)

    p_profile = sub.add_parser("profile", help="print a verb's arg-schema profile")
    p_profile.add_argument("verb", help="e.g. commerce.create_product")
    p_profile.set_defaults(func=_cmd_profile)

    p_openapi = sub.add_parser(
        "export-openapi", help="emit an OpenAPI 3.1 document for the five NIL endpoints"
    )
    p_openapi.add_argument("--format", choices=["json", "yaml"], default="json")
    p_openapi.add_argument("-o", "--output", help="write to a file instead of stdout")
    p_openapi.set_defaults(func=_cmd_export_openapi)

    p_scaffold = sub.add_parser(
        "scaffold-shim", help="generate a bootable NIL shim skeleton for a system"
    )
    p_scaffold.add_argument("--name", required=True, help="project name, e.g. acme-nil-adapter")
    p_scaffold.add_argument("--lang", default="python", choices=["python"])
    p_scaffold.add_argument("--dest", default=".", help="directory to create the project in")
    p_scaffold.set_defaults(func=_cmd_scaffold_shim)

    p_scan = sub.add_parser(
        "scan", help="discover a system's hidden requirements -> requirements-manifest.json"
    )
    p_scan.add_argument("--url", help="live system URL (live probing not yet wired; use --replay)")
    p_scan.add_argument("--replay", help="JSON file of captured native errors to infer from")
    p_scan.add_argument("--system", help="structural system id, e.g. erpnext")
    p_scan.add_argument("--safe", action="store_true", help="safe mode (required for live probing)")
    p_scan.add_argument("-o", "--output", help="write the manifest to a file instead of stdout")
    p_scan.set_defaults(func=_cmd_scan)

    p_manifest = sub.add_parser("manifest", help="work with requirements manifests")
    p_manifest.add_argument("action", choices=["validate", "show", "strip"])
    p_manifest.add_argument("file", help="path to a requirements-manifest.json")
    p_manifest.add_argument("-o", "--output", help="output file (for `strip`)")
    p_manifest.set_defaults(func=_cmd_manifest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
