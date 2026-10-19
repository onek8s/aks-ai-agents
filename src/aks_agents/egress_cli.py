"""``aks-agent egress new|propose``: the egress agent's deterministic steps around the model (egress.py)."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import egress
from .definition import load_all
from .platforms.base import PlatformError
from .platforms.github_api import GitHubClient
from .settings import PlatformConfig, Settings
from .tools.firewall import map_addresses
from .tools.shell import execute

APPS = "stacks/platform-extensions/firewall-applications"


def _env(platform: PlatformConfig, settings: Settings) -> dict[str, Any]:
    return platform.environment(settings.environment)


def _az_query(workspace: str):
    ctx = SimpleNamespace(state={})

    def query(kql: str) -> list[dict[str, Any]]:
        out = execute(
            ctx,
            ["az", "monitor", "log-analytics", "query", "-w", workspace, "--analytics-query", kql, "-o", "json"],
            timeout=180,
        )
        return json.loads(out or "[]")

    return query


class GitHub:
    def __init__(self, platform: PlatformConfig, spoke: str, client: GitHubClient | None = None):
        self.client = client or GitHubClient()
        self.repo = platform.repository("firewall")
        self.branch = platform.get("git.defaultBranch", "main")
        self.spoke = spoke

    def pr_state(self, repo: str, number: int) -> str:
        pr = self.client.request("GET", f"/repos/{repo}/pulls/{number}")
        return "merged" if pr.get("merged") else pr.get("state", "open")

    def _raw(self, path: str, ref: str) -> str | None:
        try:
            return self.client.request(
                "GET", f"/repos/{self.repo}/contents/{path}", params={"ref": ref}, accept="application/vnd.github.raw"
            )
        except PlatformError as err:
            if getattr(err, "status", None) == 404 or "404" in str(err):
                return None
            raise

    def file_for(self, namespace: str) -> str | None:
        return self._raw(f"{APPS}/{self.spoke}/{namespace}.yaml", self.branch)

    def open_pr_files(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for pr in (
            self.client.request("GET", f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100}) or []
        ):
            if not pr["head"]["ref"].startswith("agent/egress-agent/"):
                continue
            for f in self.client.request("GET", f"/repos/{self.repo}/pulls/{pr['number']}/files") or []:
                if f["filename"].startswith(f"{APPS}/{self.spoke}/") and f["filename"].endswith(".yaml"):
                    ns = Path(f["filename"]).stem
                    ref = {"repo": self.repo, "number": pr["number"], "url": pr["html_url"]}
                    for entry in egress.existing_fqdns(self._raw(f["filename"], pr["head"]["sha"])):
                        found.setdefault(ns, {})[entry] = ref
        return found


def _networks(prefixes: list[str]) -> list[Any]:
    return [ipaddress.ip_network(p, strict=False) for p in prefixes]


def cmd_new(args: argparse.Namespace, settings: Settings) -> int:
    platform = PlatformConfig.load(settings.platform_file)
    env, at = _env(platform, settings), egress.now()
    fw = env.get("firewall") or {}
    state = egress.EgressState.load(args.state)
    if args.forget_declined:
        dropped = state.forget_declined()
        print(f"forgot {dropped} declined proposals: their flows are looked at again")
    gh = GitHub(platform, fw.get("spoke", ""))
    state.refresh(gh.pr_state, at)
    workspace = os.environ.get("AKS_AGENT_FIREWALL_WORKSPACE") or fw.get("workspaceId")
    include_allowed = args.include_allowed or bool(fw.get("includeAllowed", False))
    start = egress.window_start(state, at, dt.timedelta(minutes=args.max_lookback_minutes))
    rows = egress.new_flows(
        _az_query(workspace),
        state,
        fw.get("table", "AZFWApplicationRule"),
        at,
        max_lookback=dt.timedelta(minutes=args.max_lookback_minutes),
        include_allowed=include_allowed,
    )
    kept = egress.drop_known(rows, state, _networks(fw.get("sourcePrefixes") or []))
    state.save(args.state)
    Path(args.out).write_text(json.dumps(kept, default=str))
    kind = "denied and allowed" if include_allowed else "denied"
    print(
        f"{kind} flows ingested since {start}: {len(rows)}; new to look at: {len(kept)}; checkpoint {state.checkpoint}"
    )
    if len(rows) >= egress.MAX_ROWS:
        print(f"at most {egress.MAX_ROWS} flows a run: the next run continues from the checkpoint")
    _output("new", len(kept))
    return 0


def cmd_propose(args: argparse.Namespace, settings: Settings) -> int:
    from .runtime import AgentRunner

    platform = PlatformConfig.load(settings.platform_file)
    env, at = _env(platform, settings), egress.now()
    fw = env.get("firewall") or {}
    state = egress.EgressState.load(args.state)
    rows = json.loads(Path(args.flows).read_text())
    ctx = SimpleNamespace(state={})
    mapping: dict[str, dict[str, Any]] = {}
    for cluster, prefixes in (fw.get("cells") or {}).items():
        nets = _networks(prefixes)
        ips = sorted({r["SourceIp"] for r in rows if any(ipaddress.ip_address(r["SourceIp"]) in n for n in nets)})
        if not ips:
            continue
        items = {}
        for kind in ("pods", "nodes", "namespaces"):
            argv = ["kubectl", "--context", cluster, "--request-timeout=60s", "get", kind, "-o", "json"]
            if kind == "pods":
                argv.append("-A")
            items[kind] = json.loads(execute(ctx, argv, timeout=120)).get("items", [])
        for m in map_addresses(ips, items["pods"], items["nodes"], items["namespaces"]):
            m["cluster"] = cluster
            mapping[m["ip"]] = m
    flows = egress.attribute(rows, mapping, state, at)
    gh = GitHub(platform, fw.get("spoke", ""))
    proposed = egress.to_propose(flows, state, gh.file_for, gh.open_pr_files, at)
    count = sum(len(v) for v in proposed.values())
    print(f"flows from application pods: {len(flows)}; to propose: {count} in {len(proposed)} namespaces")
    if not proposed:
        state.save(args.state)
        _output("proposed", 0)
        return 0
    definition = load_all(settings.definitions_dir)["egress-agent"]
    result = AgentRunner(definition, settings, platform).run(egress.task_text(proposed, fw.get("spoke", "")))
    if not settings.dry_run:
        # Declined (proposed nothing) only when the model finished; a blocked or failed run is tried again
        egress.record(state, proposed, result.outputs, at, decline=result.status in ("completed", "no_action_needed"))
    state.save(args.state)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    _output("proposed", len(result.outputs))
    return 0 if result.status in ("completed", "no_action_needed") else 1


def _output(name: str, value: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def add_parser(sub) -> None:
    eg = sub.add_parser("egress", help="the egress agent's deterministic steps (new denials, attribution, dedup)")
    esub = eg.add_subparsers(dest="egress_command", required=True)
    new = esub.add_parser("new", help="flows ingested since the last run (no cluster access, no model)")
    new.add_argument("--state", required=True)
    new.add_argument("--out", required=True)
    new.add_argument(
        "--max-lookback-minutes",
        type=int,
        default=int(egress.MAX_LOOKBACK.total_seconds() // 60),
        help="never read further back than this, whatever the checkpoint (default 180)",
    )
    new.add_argument(
        "--forget-declined",
        action="store_true",
        help="look again at flows the model proposed nothing for (they are otherwise skipped for a day)",
    )
    new.add_argument(
        "--include-allowed",
        action="store_true",
        help="allowed flows too (a firewall that still allows by default); also firewall.includeAllowed in the config",
    )
    new.add_argument("--env", help="environment (default AKS_AGENT_ENV)")
    new.set_defaults(func=cmd_new)
    prop = esub.add_parser("propose", help="map to pods, drop what is proposed already, run the model on the rest")
    prop.add_argument("--state", required=True)
    prop.add_argument("--flows", required=True)
    prop.add_argument("--env")
    prop.add_argument("--dry-run", action="store_true")
    prop.set_defaults(func=cmd_propose)


if __name__ == "__main__":
    sys.exit(1)
