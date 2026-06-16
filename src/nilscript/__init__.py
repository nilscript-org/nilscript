"""nilscript — Network Intent Layer (NIL) + the nilscript DSL.

The neutral standard for connecting systems to agents. This top-level package is a
thin, dependency-free accessor over the bundled standard (JSON schemas + docs). It
contains no standard logic — it only locates and loads files.

The optional Python SDK lives in ``nilscript.sdk`` and is imported only when the
package is installed with the extra::

    pip install nilscript          # standard only (this module) — zero deps
    pip install nilscript[sdk]     # + SDK (httpx, pydantic)

``import nilscript`` never imports ``nilscript.sdk``; the SDK's heavy dependencies
are pulled in only on ``from nilscript.sdk import ...``.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

__version__ = "0.3.0"

__all__ = ["spec_path", "dsl_schema_path", "load_profile", "__version__"]

# Anchor every lookup to this package's installed location (works from a wheel).
_PKG = files(__name__)

# The NIL schemas keep their historical /0.1/ namespace segment (the release is
# tracked in nil/versions/ + verb semver, not in the path). See VERSIONING.md.
_NIL_SCHEMAS = "nil/schemas/0.1"


def _path(*parts: str) -> Path:
    """Resolve a path inside the installed package to a real filesystem Path."""
    return Path(str(_PKG.joinpath(*parts)))


def spec_path() -> Path:
    """Filesystem path to the bundled NIL JSON Schema root (``nil/schemas/0.1``)."""
    return _path(*_NIL_SCHEMAS.split("/"))


def dsl_schema_path() -> Path:
    """Filesystem path to the bundled nilscript DSL JSON Schema."""
    return _path("dsl", "schema", "nilscript-dsl.v0.1.schema.json")


def load_profile(verb: str) -> dict[str, Any]:
    """Load a profile action's JSON Schema by qualified verb.

    ``verb`` is ``"<domain>.<action>"`` (e.g. ``"commerce.process_refund"``),
    resolving to ``nil/schemas/0.1/profiles/<domain>-v1/<action>.json``.

    Raises ``ValueError`` for a malformed verb and ``FileNotFoundError`` when no
    such profile action exists.
    """
    domain, sep, action = verb.partition(".")
    if not sep or not domain or not action:
        raise ValueError(
            f"verb must be '<domain>.<action>' (e.g. 'commerce.process_refund'), got {verb!r}"
        )
    target = _PKG.joinpath(_NIL_SCHEMAS, "profiles", f"{domain}-v1", f"{action}.json")
    if not target.is_file():
        raise FileNotFoundError(f"no profile schema for verb {verb!r} at {target}")
    return json.loads(target.read_text(encoding="utf-8"))
