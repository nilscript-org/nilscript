"""The NIL language-services layer — the LSP brain for the Source(.nil) editor.

A PROJECTION over the frozen Cycle AST v0.2. It owns NO state: the parser (`parse_nil`), the
compiler (`compile_cycle`), and the symbol index (`ProtocolRegistry`) do all the work. This module
just re-presents their answers in editor terms — live diagnostics, context-aware completion, hover,
and semantic tokens.

Every function is pure and total — it never raises. Mid-edit `.nil` text routinely fails to parse;
these functions degrade gracefully (best-effort keyword/verb completion, a single syntax diagnostic)
rather than throwing, because an editor calls them on every keystroke.

  - `diagnostics`      — parse + compile + dead-reference findings as editor diagnostics
  - `completions`      — context-aware suggestions from the token(s) before the cursor
  - `hover`            — the symbol under the cursor, resolved to its definition detail
  - `semantic_tokens`  — token classification driven off the parser's own tokenizer
"""

from __future__ import annotations

import json
import re

from nilscript.cycle.compile import compile_cycle
from nilscript.cycle.nil_parser import NilSyntaxError, _tokenize, parse_nil
from nilscript.cycle.registry import ProtocolRegistry
from nilscript.kernel.context import ValidationContext

# Step-body keywords (the head keyword of a step decides its type).
_STEP_KEYWORDS = ("use", "query", "decision", "await", "notify", "output", "next")
# Section keywords + structural keywords classified as `keyword` in semantic tokens.
_KEYWORDS = frozenset(
    {
        "cycle",
        "triggers_on",
        "triggers",
        "manual",
        "schedule",
        "where",
        "workspace",
        "intent",
        "documentation",
        "meta",
        "let",
        "context",
        "roles",
        "policies",
        "policy",
        "resources",
        "flow",
        "entry",
        "step",
        "outcomes",
        "use",
        "query",
        "decision",
        "when",
        "on_true",
        "on_false",
        "await",
        "approval",
        "notify",
        "output",
        "next",
        "on",
        "role",
        "applies_to",
        "raises_tier",
    }
)
# A bare identifier path, mirroring registry/_PATH — the unit hover/completion reason over.
_IDENT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_BOOL_NULL = frozenset({"true", "false", "null"})
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


# ── diagnostics ──────────────────────────────────────────────────────────────────────────────


def diagnostics(text: str, ctx: ValidationContext | None = None) -> list[dict]:
    """Editor diagnostics for `.nil` source. Each diag is
    `{severity, code, message, line, col}` (severity ∈ error|warning|info).

    1. Parse. A `NilSyntaxError` yields ONE error diag at its position and returns — a cycle that
       does not parse cannot be analysed further.
    2. If a verb catalog (`ctx`) is given, compile (lower → V1–V6) and map each ValidationResult
       diagnostic to an editor diag, locating the offending step's name in the text. With no ctx
       we cannot validate verbs (V4/V5 need the catalog), so we add one info diag instead.
    3. Always add dead-reference findings (undefined steps/refs as errors, unused outputs/variables
       as warnings).
    """
    try:
        cycle = parse_nil(text)
    except NilSyntaxError as exc:
        return [
            {
                "severity": "error",
                "code": "NIL_SYNTAX",
                "message": exc.message,
                "line": exc.line,
                "col": exc.col,
            }
        ]

    diags: list[dict] = []

    if ctx is not None:
        result = compile_cycle(cycle, ctx)
        id_to_name = {sid: name for name, sid in result.step_ids.items()}
        for d in result.diagnostics.diagnostics:
            step_name = id_to_name.get(d.node) if d.node else None
            line, col = _locate(text, step_name)
            diags.append(
                {
                    "severity": "error" if d.severity == "ERROR" else "warning",
                    "code": d.code,
                    "message": d.message,
                    "line": line,
                    "col": col,
                }
            )
    else:
        diags.append(
            {
                "severity": "info",
                "code": "NIL_VERBS_UNVALIDATED",
                "message": "verbs not validated (no active adapter connected)",
                "line": 1,
                "col": 1,
            }
        )

    registry = ProtocolRegistry.from_cycle(cycle, ctx)
    for ref in registry.dead_references():
        is_error = ref.problem in ("undefined_step", "undefined_ref")
        line, col = _locate(text, ref.name)
        diags.append(
            {
                "severity": "error" if is_error else "warning",
                "code": f"NIL_{ref.problem.upper()}",
                "message": f"{ref.problem.replace('_', ' ')}: {ref.name!r}",
                "line": line,
                "col": col,
            }
        )

    return diags


def _locate(text: str, name: str | None) -> tuple[int, int]:
    """Best-effort 1-based (line, col) of `name` as a whole word in the source. Falls back to 1/1
    when the name is unknown or not found — diagnostics are still attached, just at the top."""
    if not name:
        return 1, 1
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    for i, raw_line in enumerate(text.splitlines(), start=1):
        m = pattern.search(raw_line)
        if m:
            return i, m.start() + 1
    return 1, 1


# ── completion ───────────────────────────────────────────────────────────────────────────────


def completions(text: str, line: int, col: int, ctx: ValidationContext | None = None) -> list[dict]:
    """Context-aware completions at (1-based line, 1-based col). Pragmatic heuristic on the token(s)
    before the cursor (see `_completion_context`). Never raises — if parse fails mid-edit, falls
    back to keyword + catalog-verb completions so the editor always gets something useful."""
    prefix = _line_prefix(text, line, col)
    word_before = _last_keyword(prefix)
    partial = _partial_token(prefix)

    registry, parsed = _safe_registry(text, ctx)

    # after `use ` / `query ` → catalog verbs
    if word_before in ("use", "query"):
        return _verb_completions(registry, ctx, partial)

    # after a step-target keyword → step names
    if word_before in ("next", "on_true", "on_false", "on_approve", "on_reject", "on_timeout") or (
        prefix.rstrip().endswith("->")
    ):
        if parsed:
            return [_symbol_item(s) for s in registry.completions("step")]
        return _keyword_completions(partial)

    # after `approver:` → context-entity symbols
    if prefix.rstrip().endswith("approver:") or word_before == "approver":
        if parsed:
            return [_symbol_item(s) for s in registry.completions("context_entity")]
        return _keyword_completions(partial)

    # inside a `with { … }` value position → value sources (output/variable/context)
    if _in_with_value(prefix):
        if parsed:
            return [
                _symbol_item(s)
                for s in registry.completions()
                if s.kind in ("output", "variable", "context_entity")
            ]
        return _keyword_completions(partial)

    # at the top of a step body → the step-keyword set
    if _at_step_head(prefix):
        return _keyword_completions(partial, keywords=_STEP_KEYWORDS)

    # fall back to all symbols (+ keywords when nothing parsed)
    if parsed:
        items = [_symbol_item(s) for s in registry.completions()]
        items.extend(_keyword_completions(partial, keywords=_STEP_KEYWORDS))
        return items
    return _keyword_completions(partial) + _verb_completions(registry, ctx, partial)


def _verb_completions(
    registry: ProtocolRegistry | None, ctx: ValidationContext | None, partial: str
) -> list[dict]:
    if ctx is None:
        return []
    if registry is not None:
        verbs = registry.verbs_for(partial)
    else:
        # parse failed mid-edit — derive the catalog directly from ctx so `use ` still completes
        verbs = sorted(v for v in _ctx_verbs(ctx) if v.startswith(partial))
    return [
        {"label": v, "kind": "verb", "detail": "catalog verb", "insert": v} for v in verbs
    ]


def _ctx_verbs(ctx: ValidationContext) -> set[str]:
    """Every verb the catalog declares (read verbs + each skill's required verbs)."""
    known: set[str] = set(ctx.read_verbs)
    for spec in ctx.skills.values():
        known |= set(spec.required_verbs)
    return known


def _keyword_completions(partial: str, keywords: tuple[str, ...] = _STEP_KEYWORDS) -> list[dict]:
    return [
        {"label": kw, "kind": "keyword", "detail": "keyword", "insert": kw}
        for kw in keywords
        if kw.startswith(partial)
    ]


def _symbol_item(symbol) -> dict:
    return {
        "label": symbol.name,
        "kind": symbol.kind,
        "detail": symbol.detail,
        "insert": symbol.name,
    }


# ── hover ────────────────────────────────────────────────────────────────────────────────────


def hover(text: str, line: int, col: int, ctx: ValidationContext | None = None) -> dict | None:
    """The definition detail of the identifier under the cursor, or None if nothing resolves. For a
    verb the detail already carries the skill + grant state (from the registry)."""
    name = _identifier_at(text, line, col)
    if not name:
        return None
    registry, parsed = _safe_registry(text, ctx)
    if registry is None:
        return None
    symbol = registry.resolve(name)
    if symbol is None:
        # a dotted path (`lead.id`) — resolve the head
        head = name.partition(".")[0]
        symbol = registry.resolve(head)
    if symbol is None:
        return None
    return {"contents": symbol.detail, "kind": symbol.kind}


# ── semantic tokens ──────────────────────────────────────────────────────────────────────────


def semantic_tokens(text: str) -> list[dict]:
    """Deterministic token classification driven off the parser's own tokenizer. Each token is
    `{line, start, length, type}` (1-based line, 0-based start col) with type ∈ {keyword, cycle_id,
    verb, role, entity, variable, step, string, number, comment, operator}.

    Strings, numbers, and punctuation are classified directly. Word tokens are classified by the
    section/keyword grammar with a small amount of look-back: the word after `cycle` is the cycle
    id, the word after `use`/`query` is a verb, the word after `step` is a step name. Never raises —
    a tokenizer failure yields an empty list (the editor falls back to no highlighting)."""
    try:
        tokens = _tokenize(text)
    except NilSyntaxError:
        return []

    out: list[dict] = []
    prev_word: str | None = None
    for tok in tokens:
        if tok.kind == "eof":
            break
        if tok.kind == "string":
            ttype = "string"
            prev_word = None
        elif tok.kind == "punct":
            ttype = "operator"
        else:  # word
            ttype = _classify_word(tok.value, prev_word)
            prev_word = tok.value
        out.append(
            {"line": tok.line, "start": tok.col - 1, "length": _token_length(tok), "type": ttype}
        )
    return out


def _token_length(tok) -> int:
    if tok.kind == "string":
        # the printed string token includes the quotes; the value is unescaped — re-encode length
        return len(json.dumps(tok.value, ensure_ascii=False))
    return len(tok.value)


def _classify_word(value: str, prev_word: str | None) -> str:
    if prev_word == "cycle":
        return "cycle_id"
    if prev_word in ("use", "query"):
        return "verb"
    if prev_word == "step":
        return "step"
    if value in _KEYWORDS:
        return "keyword"
    if _NUMBER.match(value):
        return "number"
    if value in _BOOL_NULL:
        return "keyword"
    return "variable"


# ── shared helpers ───────────────────────────────────────────────────────────────────────────


def _safe_registry(
    text: str, ctx: ValidationContext | None
) -> tuple[ProtocolRegistry | None, bool]:
    """(registry, parsed). On a parse failure return (None, False) so callers fall back to
    keyword/verb completion. Build is total — it never raises out of the LSP layer."""
    try:
        cycle = parse_nil(text)
    except NilSyntaxError:
        return None, False
    try:
        return ProtocolRegistry.from_cycle(cycle, ctx), True
    except Exception:  # noqa: BLE001 — best-effort; an index failure must not break the editor
        return None, False


def _line_prefix(text: str, line: int, col: int) -> str:
    """The source on `line` (1-based) up to `col` (1-based) — what is left of the cursor."""
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return ""
    raw = lines[line - 1]
    return raw[: max(0, col - 1)]


def _partial_token(prefix: str) -> str:
    """The partial identifier the cursor is currently on (empty if the cursor sits after a space)."""
    m = re.search(r"[A-Za-z0-9_.\-+]*$", prefix)
    return m.group(0) if m else ""


def _last_keyword(prefix: str) -> str | None:
    """The last complete word BEFORE the partial token the cursor is on (the steering keyword)."""
    stripped = prefix[: len(prefix) - len(_partial_token(prefix))]
    words = re.findall(r"[A-Za-z_]\w*", stripped)
    return words[-1] if words else None


def _in_with_value(prefix: str) -> bool:
    """True when the cursor is at a VALUE position inside an open `with`/arg map: after a `key:` with
    an unbalanced `{` on the line and a `:` since the last `{`."""
    if "{" not in prefix:
        return False
    opens = prefix.count("{")
    closes = prefix.count("}")
    if opens <= closes:
        return False
    after_brace = prefix.rsplit("{", 1)[1]
    return ":" in after_brace


def _at_step_head(prefix: str) -> bool:
    """True when the cursor is at the head of a step body — i.e. the last open brace was a `step … {`
    and nothing meaningful follows it yet on this prefix."""
    if "{" not in prefix:
        return False
    before, _, after = prefix.rpartition("{")
    if "}" in after:
        return False
    # the token immediately before this `{` chain is a step id, whose own predecessor is `step`
    words = re.findall(r"[A-Za-z_]\w*", before)
    return len(words) >= 2 and words[-2] == "step" and after.strip() == _partial_token(prefix)


def _identifier_at(text: str, line: int, col: int) -> str | None:
    """The identifier path under the (1-based) cursor, or None when the cursor is not on a word."""
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return None
    raw = lines[line - 1]
    idx = col - 1
    for m in _IDENT.finditer(raw):
        if m.start() <= idx <= m.end():
            return m.group(0)
    return None


__all__ = ["diagnostics", "completions", "hover", "semantic_tokens"]
