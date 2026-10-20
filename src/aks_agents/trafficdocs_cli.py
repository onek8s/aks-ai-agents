"""``aks-agent docs traffic``: the traffic flow pages from local clones, without a model (trafficdocs.py).

    aks-agent docs traffic --spoke dev-int --landing-zone ../azure-landing-zone --fleet ../aks-fluxcd-platform \\
        --iac ../aks-terraform --app-repo onek8s/aks-fluxcd-example=../aks-fluxcd-example [--check] [--github]

Writes into ``<landing-zone>/docs/traffic-flows`` (``--output`` for another directory) and prints the findings;
``--check`` writes nothing and fails when the pages there are stale (a CI check); ``--github`` also marks the
destinations the egress agent's open pull requests propose (needs AKS_AGENT_GITHUB_TOKEN and the platform config).

``aks-agent docs lab``: the lab examples of the design site (labdocs.py), from the same clones and the applications'
source repositories, into ``<site>/examples/lab`` - one page per isolation zone and an index:

    aks-agent docs lab --spoke dev-int --landing-zone ../azure-landing-zone --fleet ../aks-fluxcd-platform \\
        --iac ../aks-terraform --app-repo onek8s/aks-fluxcd-example=../aks-fluxcd-example \\
        --app-source onek8s/aks-hello=../aks-hello --site ../onek8s.github.io [--output DIR] [--check] [--github]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import labdocs, trafficdocs
from .settings import PlatformConfig, Settings


def _output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def _repos(items: list[str] | None, option: str) -> dict[str, Path] | None:
    """``owner/repo=directory`` items -> {owner/repo: directory}; None (after a message) when one is malformed."""
    repos: dict[str, Path] = {}
    for item in items or []:
        slug, _, directory = item.partition("=")
        if not directory:
            print(f"{option} takes owner/repo=directory, not {item!r}", file=sys.stderr)
            return None
        repos[trafficdocs.repo_slug(slug)] = Path(directory)
    return repos


def _sources(args: argparse.Namespace, settings: Settings) -> trafficdocs.Sources | None:
    app_repos = _repos(args.app_repo, "--app-repo")
    if app_repos is None:
        return None
    sources = trafficdocs.Sources(
        landing_zone=Path(args.landing_zone),
        fleet=Path(args.fleet),
        iac=Path(args.iac),
        spoke=args.spoke,
        app_repos=app_repos,
    )
    if args.github:
        from .egress_cli import GitHub

        platform = PlatformConfig.load(settings.platform_file)
        for namespace, entries in GitHub(platform, args.spoke).open_pr_files().items():
            sources.proposed[namespace] = {entry: pr["url"] for entry, pr in entries.items()}
    return sources


def cmd_traffic(args: argparse.Namespace, settings: Settings) -> int:
    sources = _sources(args, settings)
    if sources is None:
        return 2
    result = trafficdocs.generate(sources)
    out_dir = Path(args.output) if args.output else Path(args.landing_zone) / trafficdocs.DOCS_DIR
    print(f"{len(result.applications)} applications of {args.spoke}; {len(result.files)} pages")
    print(trafficdocs.findings_text(result.findings))
    _output("findings", len(result.findings))
    if args.check:
        stale = trafficdocs.diff(result, out_dir, args.spoke)
        for rel in stale:
            print(f"stale: {out_dir / rel}")
        print("up to date" if not stale else f"{len(stale)} pages are stale: run aks-agent docs traffic")
        _output("changed", len(stale))
        return 1 if stale else 0
    changed, removed = trafficdocs.write(result, out_dir, args.spoke)
    for rel in changed:
        print(f"wrote {out_dir / rel}")
    for rel in removed:
        print(f"removed {out_dir / rel}")
    print(f"changed: {len(changed)}, removed: {len(removed)}, unchanged: {len(result.files) - len(changed)}")
    _output("changed", len(changed) + len(removed))
    return 0


def cmd_lab(args: argparse.Namespace, settings: Settings) -> int:
    sources = _sources(args, settings)
    app_sources = _repos(args.app_source, "--app-source")
    if sources is None or app_sources is None:
        return 2
    if not args.output and not args.site:
        print("docs lab needs --site (the design site's clone) or --output", file=sys.stderr)
        return 2
    repos = labdocs.Repos(landing_zone=args.landing_zone_repo, fleet=args.fleet_repo, iac=args.iac_repo)
    result = labdocs.generate(sources, app_sources, repos)
    out_dir = Path(args.output) if args.output else Path(args.site) / labdocs.DOCS_DIR
    sections = sum(len(s) for s in result.lab.sections.values())
    print(f"{len(result.lab.zones)} zones and {sections} applications of {args.spoke}; {len(result.files)} pages")
    print(trafficdocs.findings_text(result.findings))
    _output("findings", len(result.findings))
    if args.check:
        stale = labdocs.diff(result, out_dir)
        for rel in stale:
            print(f"stale: {out_dir / rel}")
        print("up to date" if not stale else f"{len(stale)} pages are stale: run aks-agent docs lab")
        _output("changed", len(stale))
        return 1 if stale else 0
    changed, removed = labdocs.write(result, out_dir)
    for rel in changed:
        print(f"wrote {out_dir / rel}")
    for rel in removed:
        print(f"removed {out_dir / rel}")
    print(f"changed: {len(changed)}, removed: {len(removed)}, unchanged: {len(result.files) - len(changed)}")
    _output("changed", len(changed) + len(removed))
    return 0


def _clone_arguments(p) -> None:
    p.add_argument("--spoke", required=True, help="<env>-<exposure>, e.g. dev-int")
    p.add_argument("--landing-zone", required=True, help="clone of the landing zone (the firewall rule files)")
    p.add_argument("--fleet", required=True, help="clone of the fleet repository (zone allow-lists)")
    p.add_argument("--iac", required=True, help="clone of aks-terraform (cells, managed namespaces)")
    p.add_argument(
        "--app-repo",
        action="append",
        metavar="OWNER/REPO=DIR",
        help="clone of an application repository the managed namespaces name; repeatable",
    )


def add_parser(sub) -> None:
    docs = sub.add_parser(
        "docs", help="generated documentation (traffic flows per application and zone, the lab examples)"
    )
    dsub = docs.add_subparsers(dest="docs_command", required=True)
    tr = dsub.add_parser("traffic", help="traffic flow pages with Mermaid pictures, from local clones, no model")
    _clone_arguments(tr)
    tr.add_argument("--output", help="directory for the pages (default <landing-zone>/docs/traffic-flows)")
    tr.add_argument("--check", action="store_true", help="write nothing; fail when the pages there are stale")
    tr.add_argument("--github", action="store_true", help="mark destinations in the egress agent's open pull requests")
    tr.add_argument("--env", help="environment (default AKS_AGENT_ENV)")
    tr.set_defaults(func=cmd_traffic)
    lab = dsub.add_parser("lab", help="the design site's lab examples (examples/lab/), from local clones, no model")
    _clone_arguments(lab)
    lab.add_argument(
        "--app-source",
        action="append",
        metavar="OWNER/REPO=DIR",
        help="clone of an application's source repository (README, version), matched by the image's name; repeatable",
    )
    lab.add_argument("--site", help="clone of the design site; the pages go to <site>/examples/lab")
    lab.add_argument("--output", help="directory for the pages instead of <site>/examples/lab")
    lab.add_argument("--check", action="store_true", help="write nothing; fail when the pages there are stale")
    lab.add_argument("--github", action="store_true", help="mark destinations in the egress agent's open pull requests")
    lab.add_argument(
        "--landing-zone-repo", default="onek8s/azure-landing-zone", help="its GitHub repository, for links"
    )
    lab.add_argument("--fleet-repo", default="onek8s/aks-fluxcd-platform", help="its GitHub repository, for links")
    lab.add_argument("--iac-repo", default="onek8s/aks-terraform", help="its GitHub repository, for links")
    lab.add_argument("--env", help="environment (default AKS_AGENT_ENV)")
    lab.set_defaults(func=cmd_lab)
