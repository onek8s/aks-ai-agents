"""Tools every agent may be granted: the design documents (the specification) and the guardrail limits."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolError, tool

# The design repository's folders: the requirements (human-owned) and the design that meets them.
DESIGN_FOLDERS = ("requirements", "design")


def _design_root(ctx: ToolContext) -> Path:
    root = ctx.settings.design_dir
    if not root or not root.is_dir():
        raise ToolError("the design repository is not available in this run")
    return root


def safe_join(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ToolError(f"{relative} is outside the repository")
    return path


@tool(
    "design_list",
    "List the requirements and the sections of the platform design documents (the specification every change must "
    "follow).",
    {},
)
def design_list(ctx: ToolContext, _: dict[str, Any]) -> str:
    root = _design_root(ctx)
    lines = []
    for folder in DESIGN_FOLDERS:
        for path in sorted((root / folder).glob("*.md")):
            first = path.read_text().splitlines()[0].lstrip("# ").strip() if path.stat().st_size else ""
            lines.append(f"{folder}/{path.name}: {first}")
    return "\n".join(lines)


@tool(
    "design_read",
    "Read one design document, e.g. design/6.1-ai-driven-day-2-operations.md or requirements/0.2-requirements.md. "
    "Cite the section a change follows.",
    {"path": {"type": "string", "description": "Path relative to the design repository root"}},
)
def design_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = safe_join(_design_root(ctx), args["path"])
    if not path.is_file():
        raise ToolError(f"{args['path']} not found; use design_list")
    return path.read_text()


@tool(
    "design_search",
    "Search the design documents with a regular expression (case-insensitive); returns matching lines.",
    {"pattern": {"type": "string"}},
)
def design_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = _design_root(ctx)
    try:
        regex = re.compile(args["pattern"], re.IGNORECASE)
    except re.error as err:
        raise ToolError(f"invalid regular expression: {err}") from err
    hits = []
    for path in sorted(root.rglob("*.md")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if regex.search(line):
                hits.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return "\n".join(hits[:200]) or "no matches"


@tool(
    "guardrails_read",
    "Read a guardrails file: limits.yaml (limits, windows, allowed versions) or tiers.yaml (risk tier rules). "
    "Use it to keep a change inside the limits; the merge gate computes the tier, not you.",
    {"file": {"type": "string", "enum": ["limits.yaml", "tiers.yaml"]}},
)
def guardrails_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = ctx.settings.guardrails_dir
    if not root:
        raise ToolError("guardrails are not available")
    return (root / args["file"]).read_text()
