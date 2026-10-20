"""The documentation agent's tool (design section 5.3): the traffic flow pages, generated from Git into the run's
workspace of the repository that holds them (``documentation.repository`` in platform.yaml, the landing zone by
default), one page per application and per isolation zone. Deterministic - no model, no cluster - so the model's
only work is to explain the findings in the pull request."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import trafficdocs
from .base import ToolContext, ToolError, tool
from .repo import _check_repo, _check_write_path, workspace

_SPOKE = re.compile(r"^[a-z0-9]+-[a-z0-9]+$")


def docs_repository(ctx: ToolContext) -> str:
    return ctx.platform.get("documentation.repository", "firewall")


def docs_directory(ctx: ToolContext) -> str:
    return str(ctx.platform.get("documentation.path", trafficdocs.DOCS_DIR)).strip("/")


def _app_repos(ctx: ToolContext, iac: Path, env: str) -> dict[str, Path]:
    """The application repositories the cells' managed namespaces name, cloned where the grant allows it."""
    keys = {
        trafficdocs.repo_slug(str(v)): k
        for k, v in (ctx.platform.get("git.repositories") or {}).items()
        if isinstance(v, str)
    }
    repos: dict[str, Path] = {}
    for per_cell in trafficdocs.read_managed_namespaces(iac, env).values():
        for settings in per_cell.values():
            slug = trafficdocs.repo_slug(settings.get("url", ""))
            key = keys.get(slug)
            if not slug or slug in repos or not key:
                continue
            try:
                _check_repo(ctx, "repo_read", key)
            except ToolError:
                continue  # not granted: the page shows the rules only and says so
            repos[slug] = workspace(ctx, key)
    return repos


def _proposed(ctx: ToolContext, spoke: str) -> dict[str, dict[tuple[str, str, str], str]]:
    """Destinations in the egress agent's open pull requests (GitHub only; nothing on failure)."""
    if ctx.platform.get("git.platform", "github") != "github":
        return {}
    try:
        from ..egress_cli import GitHub

        found: dict[str, dict[tuple[str, str, str], str]] = {}
        for namespace, entries in GitHub(ctx.platform, spoke).open_pr_files().items():
            found[namespace] = {entry: pr["url"] for entry, pr in entries.items()}
        return found
    except Exception:  # noqa: BLE001 - the pages are still right without it; the status is just "missing"
        return {}


@tool(
    "traffic_flow_docs",
    "Generate the traffic flow documentation of one spoke (<env>-<exposure>, e.g. dev-int) into your workspace of "
    "the documentation repository: one Markdown page with a Mermaid picture per application namespace and per "
    "isolation zone, from the applications' firewall rule files, their Flux configuration, the zone allow-lists and "
    "the cells' settings. Returns the pages that changed and the findings (rules no configuration names, named "
    "destinations without a rule). Nothing changed: no pull request is needed.",
    {"spoke": {"type": "string", "description": "<env>-<exposure>, e.g. dev-int"}},
    required=["spoke"],
)
def traffic_flow_docs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    spoke = args["spoke"]
    if not _SPOKE.match(spoke):
        raise ToolError(f"not a spoke name: {spoke!r} (<env>-<exposure>)")
    repo_key, directory = docs_repository(ctx), docs_directory(ctx)
    for key in ("firewall", "fleet", "iac", repo_key):
        _check_repo(ctx, "repo_read", key)
    _check_repo(ctx, "repo_write", repo_key)
    _check_write_path(ctx, repo_key, f"{directory}/README.md")
    _check_write_path(ctx, repo_key, f"{directory}/{spoke}/zone-x.md")
    sources = trafficdocs.Sources(
        landing_zone=workspace(ctx, "firewall"),
        fleet=workspace(ctx, "fleet"),
        iac=workspace(ctx, "iac"),
        spoke=spoke,
    )
    sources.app_repos = _app_repos(ctx, sources.iac, sources.env)
    sources.proposed = _proposed(ctx, spoke)
    result = trafficdocs.generate(sources)
    out_dir = workspace(ctx, repo_key) / directory
    changed, removed = trafficdocs.write(result, out_dir, spoke)
    return {
        "repository": repo_key,
        "directory": directory,
        "changed": changed,
        "removed": removed,
        "unchanged": len(result.files) - len(changed),
        "applications": [{"namespace": a.namespace, "zone": a.zone, "team": a.team} for a in result.applications],
        "findings": [f.__dict__ for f in result.findings],
        "next": "Nothing changed: finish without a pull request."
        if not changed and not removed
        else "Open one pull request with create_pull_request; explain every finding in its body.",
    }
