"""Derive least-privilege permissions from logged agent runs (design section 7.1, end of phase 2).

During prototyping the agents run with full permissions and every action is logged. This report turns the logs into
what each agent actually needed: the tools it used, the Azure and Kubernetes operations it ran (read vs. write), the
repository paths it changed. Writes that happened directly in Azure or a cluster become pull request paths or are
dropped; read operations map to the read-only roles; tools never used are candidates to remove from the grant.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import GUARDED
from .tools.azure import _command_words, check_az
from .tools.base import ToolError
from .tools.kubernetes import READ_VERBS as KUBE_READ_VERBS


@dataclass
class AgentUsage:
    runs: int = 0
    environments: set[str] = field(default_factory=set)
    tools: Counter = field(default_factory=Counter)
    az_read: Counter = field(default_factory=Counter)
    az_write: Counter = field(default_factory=Counter)
    kube_read: Counter = field(default_factory=Counter)
    kube_write: Counter = field(default_factory=Counter)
    paths_written: Counter = field(default_factory=Counter)
    outputs: Counter = field(default_factory=Counter)
    denied: Counter = field(default_factory=Counter)
    granted: set[str] = field(default_factory=set)


def _kube_key(args: list[str]) -> str:
    verb = args[0] if args else "?"
    resource = next((a for a in args[1:] if not a.startswith("-")), "")
    return f"{verb} {resource.split('/')[0]}".strip()


def collect(runs_dir: str | Path) -> dict[str, AgentUsage]:
    usage: dict[str, AgentUsage] = defaultdict(AgentUsage)
    for events_file in sorted(Path(runs_dir).glob("*/events.jsonl")):
        agent = None
        pending: dict[str, dict[str, Any]] = {}
        for line in events_file.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("event")
            if kind == "run_start":
                agent = event["agent"]
                u = usage[agent]
                u.runs += 1
                u.environments.add(event.get("environment", "?"))
                u.granted.update(event.get("tools", []))
            elif kind == "tool_call" and agent:
                pending[event["id"]] = event
            elif kind == "tool_result" and agent:
                call = pending.pop(event["id"], None)
                if call is None:
                    continue
                u = usage[agent]
                tool, args = call["tool"], call.get("input") or {}
                if event.get("is_error"):
                    u.denied[tool] += 1
                    continue
                u.tools[tool] += 1
                if tool == "azure_cli":
                    argv = [a for a in args.get("args", []) if a != "az"]
                    key = " ".join(_command_words(argv))
                    try:
                        check_az(argv, GUARDED)
                        u.az_read[key] += 1
                    except ToolError:
                        u.az_write[key] += 1
                elif tool == "kubectl":
                    argv = [a for a in args.get("args", []) if a != "kubectl"]
                    (u.kube_read if argv and argv[0] in KUBE_READ_VERBS else u.kube_write)[_kube_key(argv)] += 1
                elif tool in ("repo_write", "repo_replace", "repo_delete"):
                    u.paths_written[f"{args.get('repo')}:{args.get('path')}"] += 1
                elif tool in ("create_pull_request", "create_issue", "comment", "publish_review", "dispatch_runbook"):
                    u.outputs[tool if tool != "dispatch_runbook" else f"dispatch_runbook:{args.get('runbook')}"] += 1
    return dict(usage)


def report(usage: dict[str, AgentUsage]) -> str:
    lines = ["# Agent permissions derived from logged runs", ""]
    for agent in sorted(usage):
        u = usage[agent]
        unused = sorted(u.granted - set(u.tools) - {"finish"})
        lines += [f"## {agent}", "", f"- Runs: {u.runs} in {', '.join(sorted(u.environments))}"]
        lines.append(f"- Tools used: {', '.join(f'{t} ({n})' for t, n in u.tools.most_common()) or 'none'}")
        lines.append(f"- Granted but never used (remove?): {', '.join(unused) or 'none'}")
        if u.denied:
            lines.append(f"- Denied or failed calls: {', '.join(f'{t} ({n})' for t, n in u.denied.most_common())}")
        if u.az_read:
            lines.append(
                "- Azure reads (covered by the read-only roles): "
                + ", ".join(f"`az {c}` ({n})" for c, n in u.az_read.most_common())
            )
        if u.kube_read:
            lines.append(
                "- Kubernetes reads (AKS RBAC Reader): "
                + ", ".join(f"`{c}` ({n})" for c, n in u.kube_read.most_common())
            )
        writes = [f"`az {c}` ({n})" for c, n in u.az_write.most_common()]
        writes += [f"`kubectl {c}` ({n})" for c, n in u.kube_write.most_common()]
        if writes:
            lines.append(
                "- **Direct writes during prototyping** – each must be in code (a pull request path in the "
                "agent's `repo_write` grant) or removed: " + ", ".join(writes)
            )
        if u.paths_written:
            lines.append(
                "- Repository paths written (basis of the `repo_write` grant): "
                + ", ".join(f"`{p}`" for p in sorted(u.paths_written))
            )
        if u.outputs:
            lines.append("- Outputs: " + ", ".join(f"{o} ({n})" for o, n in u.outputs.most_common()))
        cluster_reads = bool(u.kube_read) or "flux_status" in u.tools
        lines.append(
            f"- Suggested Azure roles: Reader, Monitoring Reader, Log Analytics platform tables"
            f"{', Azure Kubernetes Service RBAC Reader' if cluster_reads else ''}"
            f"{', Cost Management Reader' if any(c.startswith('costmanagement') for c in u.az_read) else ''}"
            f"{', Security Reader' if any(c.startswith('security') for c in u.az_read) else ''}"
        )
        lines.append("")
    return "\n".join(lines)
