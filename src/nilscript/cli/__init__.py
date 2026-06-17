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
import os
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


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute a DSL program locally against a mounted NIL adapter — the headless kernel.

    Validate (optionally, with --context) → walk the graph → drive the adapter via PROPOSE/COMMIT →
    emit the execution trace. Headless and Temporal-free; durability is the Wosool Cloud upgrade.
    """
    import asyncio

    from nilscript.kernel import LocalExecutor
    from nilscript.sdk.client import NilClient
    from nilscript.sdk.grants import GrantRef
    from nilscript.sdk.transport import NilTransport

    program = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    inputs = json.loads(args.input) if args.input else None

    if args.context:
        from nilscript.kernel import ValidationContext, validate

        ctx = ValidationContext.from_corpus(
            json.loads(Path(args.context).read_text(encoding="utf-8"))
        )
        result = validate(program, ctx)
        for d in result.diagnostics:
            print(f"  {d.severity} {d.code}: {d.message}", file=sys.stderr)
        if not result.ok:
            print("refused: program failed validation", file=sys.stderr)
            return 1

    grant = GrantRef.from_secret(
        grant_id=args.grant_id,
        workspace=args.workspace or program.get("workspace", ""),
        secret=args.bearer or "",
        scopes=frozenset(args.scope) if args.scope else frozenset({"*"}),
    )
    transport = NilTransport(base_url=args.adapter_url, bearer_secret=args.bearer or "")
    client = NilClient(transport=transport, grant=grant)
    executor = LocalExecutor(
        client, session_id="cli-session", run_id="cli-run", locale=program.get("locale", "ar")
    )

    async def _go():  # type: ignore[no-untyped-def]
        try:
            return await executor.execute(program, input=inputs)
        finally:
            await transport.aclose()

    result = asyncio.run(_go())

    if args.json:
        print(
            json.dumps(
                {
                    "completed": result.completed,
                    "partial": result.partial,
                    "blocked_at": result.blocked_at,
                    "refusal": result.refusal,
                    "compensated": result.compensated,
                    "notifications": result.notifications,
                    "context": result.context,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        status = (
            "completed"
            if result.completed
            else ("compensated (partial)" if result.partial else "halted")
        )
        print(f"run {status}")
        for nid, entry in result.context.items():
            if nid == "input":
                continue
            print(f"  {nid}: {entry.get('output')}")
        if result.refusal:
            print(f"  refused at {result.refusal['node']}: {result.refusal['code']}")
        if result.compensated:
            print(f"  compensated: {', '.join(result.compensated)}")
        for note in result.notifications:
            print(f"  notify: {note.get('ar') or note.get('en')}")
    return 0 if result.completed else 1


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
    from nilscript.cli.manifest import diff, merge, shareable_violations, strip_instance, validate

    def _load(path: str) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    if args.action == "merge":
        if len(args.files) < 2:
            print("merge needs a base manifest + at least one override", file=sys.stderr)
            return 2
        manifests = [_load(f) for f in args.files]
        merged = merge(manifests[0], *manifests[1:])
        text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote merged manifest to {args.output}", file=sys.stderr)
        else:
            print(text)
        return 0

    if args.action == "diff":
        if len(args.files) != 2:
            print("diff needs exactly two manifests: OLD NEW", file=sys.stderr)
            return 2
        report = diff(_load(args.files[0]), _load(args.files[1]))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if report["changed"] else 0  # non-zero on drift, for CI gating

    manifest = _load(args.files[0])

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


def _verb_reversibility(verb: str) -> str | None:
    """The verb's declared reversibility tier from its profile, or None if it declares none.

    None means the rollback-honesty rows are skipped (a verb with no declared tier is IRREVERSIBLE
    in the standard, but we don't probe a shim for a capability the standard never asked it to add)."""
    try:
        return load_profile(verb).get("reversibility")
    except (FileNotFoundError, KeyError, ValueError):
        return None


def _cmd_conformance_test(args: argparse.Namespace) -> int:
    from nilscript.cli.conformance import run_conformance, summarize

    try:
        import httpx
    except ModuleNotFoundError:
        print("conformance-test needs httpx (pip install nilscript[sdk])", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.bearer}"} if args.bearer else {}
    http = httpx.Client(base_url=base, headers=headers, timeout=20.0)

    def _env(verb: str, extra: dict) -> dict:
        return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, **extra}}

    class _HttpProbe:
        def describe(self):
            return http.get("/nil/v0.1/describe").json()

        def propose(self, verb, payload_args):
            return http.post("/nil/v0.1/propose", json=_env(verb, {"args": payload_args})).json()

        def commit(self, proposal_id, idempotency_key):
            body = {"proposal": proposal_id, "idempotency_key": idempotency_key}
            return http.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w", "body": body}).json()

        def query(self, verb, payload_args):
            return http.post("/nil/v0.1/query", json=_env(verb, {"args": payload_args})).json()

        def status(self, proposal_id):
            return http.get(f"/nil/v0.1/status/{proposal_id}").json()

        def rollback(self, compensation_token, reason):
            body = {"compensation_token": compensation_token, "reason": reason}
            return http.post(
                "/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w", "body": body}
            ).json()

    write_args = json.loads(args.args) if args.args else {}
    # The rollback-honesty rows run only when a tier is known: an explicit --reversibility wins,
    # else the verb's declared tier from its profile (absent -> IRREVERSIBLE, the honest default).
    reversibility = getattr(args, "reversibility", None) or _verb_reversibility(args.verb)
    checks = run_conformance(
        _HttpProbe(),
        write_verb=args.verb,
        write_args=write_args,
        query_verb=args.query_verb,
        query_args=json.loads(args.query_args) if args.query_args else {},
        reversibility=reversibility,
    )
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}  ({check.detail})")
    passed, total = summarize(checks)
    print(f"\n{passed}/{total} conformance checks passed", file=sys.stderr)
    return 0 if passed == total else 1


def _find_demo_dir() -> Path | None:
    """Locate the reference Playground's demo/ directory.

    The demo ships alongside the source tree (repo_root/demo/), not inside the importable
    package, so an editable/source checkout finds it by walking up from this module. An
    explicit NILSCRIPT_DEMO_DIR override wins (e.g. a packaged or relocated install)."""
    override = os.environ.get("NILSCRIPT_DEMO_DIR")
    if override:
        cand = Path(override)
        return cand if (cand / "demo_ui.py").exists() else None
    # cli/__init__.py -> cli -> nilscript -> src -> repo_root
    for base in Path(__file__).resolve().parents:
        cand = base / "demo"
        if (cand / "demo_ui.py").exists():
            return cand
    return None


def _cmd_demo(args: argparse.Namespace) -> int:
    """Launch the reference Playground (demo/demo_ui.py) — needs `pip install nilscript[demo]`."""
    demo_dir = _find_demo_dir()
    if demo_dir is None:
        print(
            "demo/ not found. The Playground ships with the source tree; run from a checkout, "
            "or set NILSCRIPT_DEMO_DIR to the directory containing demo_ui.py.",
            file=sys.stderr,
        )
        return 2
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError:
        print("the demo needs FastAPI + uvicorn + litellm (pip install nilscript[demo])", file=sys.stderr)
        return 2

    import subprocess

    env = {**os.environ, "UI_PORT": str(args.port)}
    print(f"launching the nilscript Playground at http://127.0.0.1:{args.port}  (demo/ = {demo_dir})", file=sys.stderr)
    return subprocess.call([sys.executable, "demo_ui.py"], cwd=str(demo_dir), env=env)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nilscript",
        description="Tooling to build and verify NIL adapters from the standard.",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    p_verbs = sub.add_parser("verbs", help="list the verb catalog from the standard")
    p_verbs.add_argument("--json", action="store_true", help="machine-readable output")
    p_verbs.set_defaults(func=_cmd_verbs)

    p_profile = sub.add_parser("profile", help="print a verb's arg-schema profile")
    p_profile.add_argument("verb", help="e.g. commerce.create_product")
    p_profile.set_defaults(func=_cmd_profile)

    p_openapi = sub.add_parser(
        "export-openapi", help="emit an OpenAPI 3.1 document for the six NIL endpoints"
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

    p_conf = sub.add_parser("conformance-test", help="run the conformance matrix against a live shim")
    p_conf.add_argument("--url", required=True, help="base URL of a running shim")
    p_conf.add_argument("--verb", required=True, help="a write verb to exercise, e.g. services.create_invoice")
    p_conf.add_argument("--args", help="JSON object of valid args for --verb")
    p_conf.add_argument("--query-verb", help="optional query verb to exercise the bare-{data} rule")
    p_conf.add_argument("--query-args", help="JSON object of args for --query-verb")
    p_conf.add_argument("--bearer", help="bearer token if the shim requires auth")
    p_conf.add_argument(
        "--reversibility",
        choices=["REVERSIBLE", "COMPENSABLE", "IRREVERSIBLE"],
        help="exercise the rollback-honesty rows at this tier (else read from --verb's profile)",
    )
    p_conf.set_defaults(func=_cmd_conformance_test)

    p_manifest = sub.add_parser("manifest", help="work with requirements manifests")
    p_manifest.add_argument("action", choices=["validate", "show", "strip", "merge", "diff"])
    p_manifest.add_argument("files", nargs="+", help="manifest path(s); merge: base + overrides, diff: OLD NEW")
    p_manifest.add_argument("-o", "--output", help="output file (for `strip`/`merge`)")
    p_manifest.set_defaults(func=_cmd_manifest)

    p_run = sub.add_parser(
        "run", help="execute a DSL program locally against a mounted NIL adapter (the kernel)"
    )
    p_run.add_argument("plan", help="path to the DSL program (e.g. plan.nil.json)")
    p_run.add_argument("--adapter-url", required=True, help="base URL of a running NIL shim")
    p_run.add_argument("--grant-id", default="local", help="agent-plane grant id")
    p_run.add_argument("--workspace", help="workspace (defaults to the program's `workspace`)")
    p_run.add_argument("--bearer", help="bearer token if the shim requires auth")
    p_run.add_argument("--scope", action="append", help="grant scope (repeatable; default '*')")
    p_run.add_argument("--input", help="JSON object seeding $.input.* references")
    p_run.add_argument("--context", help="optional validation-context JSON; validates before running")
    p_run.add_argument("--json", action="store_true", help="machine-readable execution trace")
    p_run.set_defaults(func=_cmd_run)

    p_demo = sub.add_parser("demo", help="launch the reference Playground UI (needs nilscript[demo])")
    p_demo.add_argument("--port", type=int, default=8770, help="port to serve the Playground on (default 8770)")
    p_demo.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        # bare `nilscript` -> the banner, then the command list (never an error).
        from nilscript.cli._banner import render

        print(render())
        parser.print_help()  # the command list, then a clean exit 0
        return 0
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
