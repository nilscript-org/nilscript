#!/usr/bin/env bash
# parity.sh — release-plan §3 audit. Proves the nizam ecosystem is internally consistent:
#   • the kernel is the single source for sdk/ + cli/ and is at 0.3.0
#   • every adapter's GENERIC modules (edge/state/manifest/models) are byte-identical to the
#     template (the scaffold output) modulo backend identity — i.e. nobody hand-diverged them
#   • the CLI/SDK commands are truthful (verbs, profile, a live conformance run all pass)
#   • the reference adapter's conformance suite is green
#   • nil-erpnext-adapter is flagged as BEHIND (predates 0.3.0) — a warning, not a failure
#
# Usage:   PY=/path/to/python ./scripts/parity.sh
# Exit:    0 = parity holds (erpnext warning is non-fatal); non-zero = real drift / test failure.
set -uo pipefail

# ---- paths ------------------------------------------------------------------
KERNEL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # nizam/nilscript
NIZAM="$(cd "$KERNEL/.." && pwd)"
ADAPTERS="$NIZAM/adapters"
TEMPLATE="$ADAPTERS/nil-adapter-template"
PB="$ADAPTERS/pocketbase-nil-adapter"
ERP="$ADAPTERS/nil-erpnext-adapter"
LANDING="$NIZAM/nilscript-landing"

# A python with the dev deps (pydantic, fastapi, httpx, pytest). Override with PY=...
PY="${PY:-}"
if [[ -z "$PY" ]]; then
  for cand in "$NIZAM/.venv/bin/python" "$HOME/Desktop/packet/.venv/bin/python" python3; do
    command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
  done
fi

FAIL=0; WARN=0
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; FAIL=1; }
warn() { echo "  ! $*"; WARN=1; }
hdr()  { echo; echo "=== $* ==="; }

# Normalize a generic adapter module so only real logic remains: collapse the package name,
# drop the SYSTEM= / FastAPI(title=) identity lines, strip comments + blank lines.
norm() {
  sed -e 's/nil_adapter_template/PKG/g; s/pocketbase_nil_adapter/PKG/g; s/nil_erpnext_adapter/PKG/g' \
      -e '/^SYSTEM = /d' -e '/FastAPI(title=/d' "$1" 2>/dev/null \
    | grep -vE '^\s*#' | grep -vE '^\s*$'
}

# ---- A. kernel single-source + version -------------------------------------
hdr "A · kernel (single source for sdk/ + cli/)"
KVER="$(grep -m1 '^version = ' "$KERNEL/pyproject.toml" | tr -d ' "' | cut -d= -f2)"
[[ "$KVER" == "0.3.0" ]] && ok "kernel version = 0.3.0" || bad "kernel version = $KVER (expected 0.3.0)"
# the demo must consume the installed SDK, not a vendored copy
if grep -rqE "^(from|import) nilscript\.sdk" "$KERNEL/demo" 2>/dev/null \
   && ! find "$KERNEL/demo" -name '*.py' -path '*sdk*' | grep -q .; then
  ok "demo imports the kernel SDK (no vendored copy)"
else
  warn "could not confirm demo uses the installed SDK"
fi
echo "  - kernel test suite:"
( cd "$KERNEL" && PYTHONPATH=src "$PY" -m pytest -q 2>&1 | tail -1 | sed 's/^/    /' )
[[ ${PIPESTATUS:-0} -eq 0 ]] || true
( cd "$KERNEL" && PYTHONPATH=src "$PY" -m pytest -q >/dev/null 2>&1 ) && ok "kernel tests pass" || bad "kernel tests FAILED"

# ---- B. adapter generic-module parity --------------------------------------
hdr "B · adapter parity (generic modules == template, modulo backend identity)"
for f in edge.py state.py manifest.py models.py; do
  tpl="$TEMPLATE/src/nil_adapter_template/$f"
  pb="$PB/src/pocketbase_nil_adapter/$f"
  if [[ -f "$tpl" && -f "$pb" ]]; then
    if diff <(norm "$tpl") <(norm "$pb") >/dev/null 2>&1; then ok "pocketbase $f IDENTICAL to template"
    else bad "pocketbase $f DIVERGED from template"; fi
  else
    bad "missing $f (tpl=$([[ -f $tpl ]] && echo y || echo n) pb=$([[ -f $pb ]] && echo y || echo n))"
  fi
done
echo "  - reference adapter conformance:"
( cd "$PB" && PYTHONPATH=src "$PY" -m pytest -q 2>&1 | tail -1 | sed 's/^/    /' )
( cd "$PB" && PYTHONPATH=src "$PY" -m pytest -q >/dev/null 2>&1 ) && ok "pocketbase conformance passes" || bad "pocketbase conformance FAILED"

# ---- C. CLI / SDK truthfulness ---------------------------------------------
hdr "C · CLI / SDK truthfulness"
cli() { PYTHONPATH="$KERNEL/src" "$PY" -c "from nilscript.cli import main; raise SystemExit(main($1))"; }
cli "['verbs']"   >/dev/null 2>&1 && ok "nilscript verbs runs" || bad "nilscript verbs FAILED"
cli "['profile','commerce.create_product']" >/dev/null 2>&1 && ok "nilscript profile <verb> runs" || bad "nilscript profile FAILED"

# live conformance-test against an in-memory reference shim
PORT=8131
PYTHONPATH="$PB/src" "$PY" -c "
import uvicorn
from pocketbase_nil_adapter.edge import create_app, CapturingEmitter
from pocketbase_nil_adapter.system import FakeSystem
uvicorn.run(create_app(FakeSystem(), CapturingEmitter(), bearer='secret123'), host='127.0.0.1', port=$PORT, log_level='error')
" >/dev/null 2>&1 &
SHIM=$!
trap '[[ -n "${SHIM:-}" ]] && kill "$SHIM" 2>/dev/null' EXIT
sleep 3
if cli "['conformance-test','--url','http://127.0.0.1:$PORT','--verb','commerce.create_product','--args','{\"name\":\"X\",\"price\":9}','--query-verb','services.list_clients','--bearer','secret123']" >/dev/null 2>&1; then
  ok "live conformance-test: all checks pass"
else
  bad "live conformance-test FAILED"
fi
kill "$SHIM" 2>/dev/null; SHIM=""

# ---- D. erpnext: flagged behind (non-fatal) --------------------------------
hdr "D · nil-erpnext-adapter (0.3.0 parity status)"
if [[ -d "$ERP" ]]; then
  if grep -rqE "v0\.1/describe|resource\.(create|update|delete|read)" "$ERP/src" 2>/dev/null; then
    ok "erpnext exposes the 0.3.0 surface (describe + resource.*)"
  else
    warn "erpnext is BEHIND 0.3.0 — no describe/resource.* surface; regenerate from the template (release plan §3, deferred)"
  fi
  [[ -d "$ERP/.git" ]] && ok "erpnext is a git repo" || warn "erpnext has no git repo yet (needs git init)"
else
  warn "erpnext adapter not found"
fi

# ---- E. landing badge ------------------------------------------------------
hdr "E · landing version badge"
if grep -rq "v0.3.0" "$LANDING/components/shell/nav-bar.tsx" 2>/dev/null; then ok "landing nav badge = v0.3.0"
else warn "landing nav badge != v0.3.0"; fi

# ---- summary ---------------------------------------------------------------
hdr "summary"
if [[ "$FAIL" -ne 0 ]]; then echo "  PARITY FAILED — real drift or a failing suite above."; exit 1; fi
if [[ "$WARN" -ne 0 ]]; then echo "  PARITY OK (with warnings — see ! lines; erpnext is the known gap)."; exit 0; fi
echo "  PARITY OK — everything identical, every command truthful."; exit 0
