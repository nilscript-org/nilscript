"""Load the bundled `using-nilscript` agent skill so the server can serve it over MCP.

The skill ships as `SKILL.md` (frontmatter + body). MCP clients never read a file from the wheel,
so the server exposes the skill as an MCP **resource** and **prompt** — the discipline travels the
wire alongside the tools ("the recipe with the kitchen").
"""

from __future__ import annotations

from importlib import resources

SKILL_URI = "nil://skill/using-nilscript"


def raw_skill() -> str:
    return resources.files("nilscript.mcp").joinpath("SKILL.md").read_text(encoding="utf-8")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body_start = text.find("\n", end + 1)
    body = text[body_start + 1 :].lstrip("\n") if body_start != -1 else ""
    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta, body


def skill_body() -> str:
    """The skill markdown without frontmatter — the agent-facing discipline."""
    return _split_frontmatter(raw_skill())[1]


def skill_meta() -> dict[str, str]:
    """Frontmatter fields (`name`, `description`)."""
    return _split_frontmatter(raw_skill())[0]
