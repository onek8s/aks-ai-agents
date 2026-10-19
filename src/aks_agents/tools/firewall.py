"""Egress tools (design D61): the flows the hub firewall denied, and the pods behind their source addresses.

Every pod keeps an address of its zone's pod subnet up to the hub firewall (Azure CNI Pod Subnet), so a denied flow's
source address names a pod. The firewall's log is read where the environment keeps it (``firewall.log`` of the
environment in ``platform.yaml``):

- ``log_analytics``: Azure Firewall's resource-specific tables (``AZFWApplicationRule``, ``AZFWNetworkRule``) in the
  firewall's workspace - or labfw's ``LabfwFirewallLog_CL`` with the same columns (``firewall.table``), which the
  Azure Monitor Agent on its VM fills - read with Reader on the workspace, nothing else;
- ``file``: JSON lines with the same field names (labfw's ``azfw.jsonl``), exported by the job before the agent
  starts (``AKS_AGENT_FIREWALL_LOG``) - the lab's labfw keeps its log on its VM, which only its operator reads.

Both are read-only. The pods are found with read verbs only (pods, nodes and namespaces - never Secrets).
``firewall_egress_add`` adds destinations to an application's egress file in the run's workspace without touching its
existing entries, and create_pull_request refuses any change of such a file that removes or rewrites one."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import re
from collections.abc import Iterable
from typing import Any

import yaml

from .base import ToolContext, ToolError, tool
from .core import safe_join
from .kubernetes import _cluster
from .repo import FILE_CHECKS, _check_repo, _check_write_path, workspace
from .shell import execute

MAX_FLOWS = 200


def _parse_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _networks(prefixes: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    try:
        return [ipaddress.ip_network(p, strict=False) for p in prefixes]
    except ValueError as err:
        raise ToolError(f"invalid prefix: {err}") from err


def _in(address: str, networks: list[Any]) -> bool:
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in n for n in networks)


def summarize_denials(
    records: Iterable[dict[str, Any]],
    since: dt.datetime,
    networks: list[Any],
    limit: int = MAX_FLOWS,
) -> dict[str, Any]:
    """Denied flows grouped by source address and destination, newest first, with counts and first/last seen."""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    total = 0
    for rec in records:
        if str(rec.get("Action", "")).lower() != "deny":
            continue
        when = _parse_time(str(rec.get("TimeGenerated", "")))
        if when is None or when < since:
            continue
        source = str(rec.get("SourceIp") or "")
        if not _in(source, networks):
            continue
        total += 1
        destination = str(rec.get("Fqdn") or rec.get("DestinationIp") or "")
        port = str(rec.get("DestinationPort") or "")
        protocol = str(rec.get("Protocol") or "")
        key = (source, destination, port, protocol)
        group = groups.setdefault(
            key,
            {
                "sourceIp": source,
                "destination": destination,
                "destinationIsFqdn": bool(rec.get("Fqdn")),
                "destinationPort": port,
                "protocol": protocol,
                "category": rec.get("Category"),
                "rule": rec.get("Policy")
                and "/".join(
                    str(rec.get(k) or "") for k in ("Policy", "RuleCollectionGroup", "RuleCollection", "Rule")
                ),
                "actionReason": rec.get("ActionReason"),
                "count": 0,
                "firstSeen": when,
                "lastSeen": when,
            },
        )
        group["count"] += 1
        group["firstSeen"] = min(group["firstSeen"], when)
        group["lastSeen"] = max(group["lastSeen"], when)
    flows = sorted(groups.values(), key=lambda g: g["lastSeen"], reverse=True)
    for g in flows:
        g["firstSeen"], g["lastSeen"] = g["firstSeen"].isoformat(), g["lastSeen"].isoformat()
    return {
        "since": since.isoformat(),
        "deniedConnections": total,
        "flows": flows[:limit],
        "truncated": max(0, len(flows) - limit),
    }


def _read_file(path: str) -> Iterable[dict[str, Any]]:
    try:
        handle = open(path, encoding="utf-8")  # noqa: SIM115 - closed below
    except OSError as err:
        raise ToolError(f"the firewall log export {path} cannot be read: {err}") from err
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


KQL = """
{source}
| where TimeGenerated > ago({hours}h) and Action == "Deny"
| project TimeGenerated, Category = {category}, Action, SourceIp, Fqdn, DestinationIp, DestinationPort, Protocol,
          Policy, RuleCollectionGroup, RuleCollection, Rule, ActionReason
| take 5000
"""
AZFW_SOURCE = "union isfuzzy=true AZFWApplicationRule, AZFWNetworkRule"


def kql(hours: int, table: str | None) -> str:
    if table:
        if not table.replace("_", "").isalnum():
            raise ToolError(f"invalid firewall.table {table!r}")
        return KQL.format(source=table, category="Category", hours=hours)
    return KQL.format(source=AZFW_SOURCE, category="Type", hours=hours)


def _read_log_analytics(ctx: ToolContext, hours: int) -> list[dict[str, Any]]:
    env = ctx.platform.environment(ctx.settings.environment)
    firewall = env.get("firewall") or {}
    workspace = (
        os.environ.get("AKS_AGENT_FIREWALL_WORKSPACE")
        or firewall.get("workspaceId")
        or env.get("logAnalyticsWorkspaceId")
    )
    if not workspace:
        raise ToolError("no firewall workspace: set firewall.workspaceId or AKS_AGENT_FIREWALL_WORKSPACE")
    out = execute(
        ctx,
        [
            "az",
            "monitor",
            "log-analytics",
            "query",
            "-w",
            workspace,
            "--analytics-query",
            kql(hours, firewall.get("table")),
            "-o",
            "json",
        ],
        timeout=180,
    )
    return json.loads(out or "[]")


@tool(
    "firewall_denied_flows",
    "The connections the hub firewall denied from the environment's cells in the last hours, grouped by source "
    "address and destination (FQDN for application rules, IP otherwise) with counts, first and last seen, and the "
    "rule or default action that denied them. Optionally only sources in the given prefixes.",
    {
        "since_hours": {"type": "integer", "minimum": 1, "maximum": 168},
        "source_prefixes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    required=["since_hours"],
)
def firewall_denied_flows(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    env = ctx.platform.environment(ctx.settings.environment)
    log = (env.get("firewall") or {}).get("log", "log_analytics")
    cells = (env.get("firewall") or {}).get("sourcePrefixes") or []
    asked = args.get("source_prefixes") or []
    networks = _networks(asked or cells)
    if asked and cells:
        allowed = _networks(cells)
        if not all(any(n.subnet_of(a) for a in allowed if n.version == a.version) for n in networks):
            raise ToolError(f"source_prefixes must be within the environment's cells {cells}")
    hours = int(args["since_hours"])
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    if log == "file":
        path = os.environ.get("AKS_AGENT_FIREWALL_LOG") or (env.get("firewall") or {}).get("file")
        if not path:
            raise ToolError("no firewall log export: set AKS_AGENT_FIREWALL_LOG")
        records: Iterable[dict[str, Any]] = _read_file(path)
    elif log == "log_analytics":
        records = _read_log_analytics(ctx, hours)
    else:
        raise ToolError(f"unknown firewall.log {log!r}")
    result = summarize_denials(records, since, networks)
    cells = {c: _networks(prefixes) for c, prefixes in ((env.get("firewall") or {}).get("cells") or {}).items()}
    for flow in result["flows"]:
        flow["cluster"] = next((c for c, nets in cells.items() if _in(flow["sourceIp"], nets)), None)
    result["log"] = log
    result["spoke"] = (env.get("firewall") or {}).get("spoke")
    return result


def _owner(pod: dict[str, Any]) -> dict[str, str] | None:
    refs = pod.get("metadata", {}).get("ownerReferences") or []
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    if not ref:
        return None
    kind, name = ref.get("kind", ""), ref.get("name", "")
    # A Deployment's pods belong to a ReplicaSet <deployment>-<pod-template-hash>
    template_hash = pod.get("metadata", {}).get("labels", {}).get("pod-template-hash")
    if kind == "ReplicaSet" and template_hash and name.endswith("-" + template_hash):
        return {"kind": "Deployment", "name": name[: -len(template_hash) - 1]}
    return {"kind": kind, "name": name}


def map_addresses(
    ips: list[str], pods: list[dict[str, Any]], nodes: list[dict[str, Any]], namespaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_ns = {ns["metadata"]["name"]: ns["metadata"] for ns in namespaces}
    found: list[dict[str, Any]] = []
    for ip in ips:
        match: dict[str, Any] = {"ip": ip, "kind": "unknown"}
        for pod in pods:
            status = pod.get("status") or {}
            if pod.get("spec", {}).get("hostNetwork"):
                continue
            addresses = {a.get("ip") for a in status.get("podIPs") or []} | {status.get("podIP")}
            if ip in addresses:
                meta = pod["metadata"]
                ns = by_ns.get(meta.get("namespace"), {})
                labels = meta.get("labels") or {}
                match = {
                    "ip": ip,
                    "kind": "pod",
                    "namespace": meta.get("namespace"),
                    "pod": meta.get("name"),
                    "phase": status.get("phase"),
                    "workload": _owner(pod),
                    "app": labels.get("app.kubernetes.io/name") or labels.get("app"),
                    "node": pod.get("spec", {}).get("nodeName"),
                    "zone": (ns.get("labels") or {}).get("platform/zone"),
                    "team": (ns.get("labels") or {}).get("platform/team"),
                    "contact": (ns.get("annotations") or {}).get("platform/contact"),
                    "images": [c.get("image") for c in pod.get("spec", {}).get("containers") or []],
                }
                break
        else:
            for node in nodes:
                if ip in {a.get("address") for a in (node.get("status") or {}).get("addresses") or []}:
                    labels = node["metadata"].get("labels") or {}
                    match = {
                        "ip": ip,
                        "kind": "node",
                        "node": node["metadata"]["name"],
                        "zone": labels.get("platform/zone"),
                        "note": "the node's own address: a host-network pod, or a pod address translated on the "
                        "node - the firewall cannot tell the application",
                    }
                    break
        found.append(match)
    return found


def _items(ctx: ToolContext, cluster: str, kind: str) -> list[dict[str, Any]]:
    args = ["kubectl", "--context", cluster, "--request-timeout=60s", "get", kind, "-o", "json"]
    if kind == "pods":
        args.append("-A")
    return json.loads(execute(ctx, args, timeout=120)).get("items", [])


@tool(
    "pods_by_ip",
    "Which pod (namespace, workload, app, zone, owning team and contact) or node has each address in a cluster "
    "now - for the source addresses of denied flows. A pod that ended since the flow is not found.",
    {
        "cluster": {"type": "string"},
        "ips": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
    },
)
def pods_by_ip(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    cluster = _cluster(ctx, args["cluster"])
    for ip in args["ips"]:
        try:
            ipaddress.ip_address(ip)
        except ValueError as err:
            raise ToolError(f"not an IP address: {ip}") from err
    return map_addresses(
        list(args["ips"]),
        _items(ctx, cluster, "pods"),
        _items(ctx, cluster, "nodes"),
        _items(ctx, cluster, "namespaces"),
    )


@tool(
    "application_source",
    "Where a namespace's applications come from: its Flux Kustomizations (path, the Git repository URL and branch, "
    "applied revision) and which granted repository key that is, so you can repo_list / repo_read the right files "
    "(start with <path>/kustomization.yaml).",
    {"cluster": {"type": "string"}, "namespace": {"type": "string"}},
)
def application_source(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    cluster = _cluster(ctx, args["cluster"])
    namespace = args["namespace"]
    if not namespace.replace("-", "").isalnum():
        raise ToolError(f"not a namespace name: {namespace}")

    def get(kind: str) -> list[dict[str, Any]]:
        out = execute(
            ctx, ["kubectl", "--context", cluster, "--request-timeout=60s", "get", kind, "-n", namespace, "-o", "json"]
        )
        return json.loads(out).get("items", [])

    repos = {r["metadata"]["name"]: r.get("spec", {}) for r in get("gitrepositories.source.toolkit.fluxcd.io")}
    keys = {str(v).lower(): k for k, v in ((ctx.platform.get("git.repositories") or {}).items()) if isinstance(v, str)}
    sources = []
    for k in get("kustomizations.kustomize.toolkit.fluxcd.io"):
        spec = k.get("spec", {})
        repo = repos.get((spec.get("sourceRef") or {}).get("name"), {})
        url = repo.get("url", "")
        slug = url.lower().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        path = (spec.get("path") or ".").removeprefix("./").strip("/") or "."
        sources.append(
            {
                "kustomization": k["metadata"]["name"],
                "path": path,
                "repositoryUrl": url,
                "branch": (repo.get("ref") or {}).get("branch"),
                "appliedRevision": (k.get("status") or {}).get("lastAppliedRevision"),
                "repositoryKey": keys.get(slug),
                "start": f"repo_read repo={keys.get(slug)} path={path}/kustomization.yaml" if keys.get(slug) else None,
            }
        )
    return sources


# The applications' egress files of the landing zone (one per application namespace). Their entries are added, never
# rewritten: a pull request of an agent may add destinations, but an existing entry stays exactly as a human merged it.
APPLICATION_FILES = "stacks/platform-extensions/firewall-applications"
_FQDN = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_ITEM = re.compile(r"^(\s*)- ")


def _entry_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("fqdn", "")).lower(),
        str(entry.get("protocol", "https")).lower(),
        str(entry.get("port", 443)),
    )


def _load_egress_file(path: str, text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise ToolError(f"{path} is not valid YAML: {err}") from err
    if not isinstance(data, dict) or not isinstance(data.get("egress") or [], list):
        raise ToolError(f"{path}: expected a mapping with zone, contact and an egress list")
    return data


def keep_egress_entries(path: str, old: str | None, new: str | None) -> None:
    """Refuses a change of an application's egress file that removes or changes an entry already on the default
    branch, or its zone: an agent only adds destinations."""
    if old is None:
        return
    if new is None:
        raise ToolError(f"{path}: an agent may not delete an application's egress file")
    before, after = _load_egress_file(path, old), _load_egress_file(path, new)
    if before.get("zone") != after.get("zone"):
        raise ToolError(f"{path}: the zone may not change ({before.get('zone')} -> {after.get('zone')})")
    now = {_entry_key(e): e for e in after.get("egress") or [] if isinstance(e, dict)}
    for entry in before.get("egress") or []:
        if not isinstance(entry, dict):
            continue
        key = _entry_key(entry)
        if key not in now:
            raise ToolError(
                f"{path}: the change removes the existing entry {key[0]} ({key[1]}/{key[2]}); keep every existing "
                "entry and only add new ones (firewall_egress_add does that)"
            )
        if now[key] != entry:
            raise ToolError(
                f"{path}: the change rewrites the existing entry {key[0]} ({key[1]}/{key[2]}); existing entries stay "
                "unchanged - only add new ones (firewall_egress_add does that)"
            )


def _entry_block(indent: str, entry: dict[str, Any]) -> list[str]:
    inner = indent + "  "
    return [
        f"{indent}- fqdn: {entry['fqdn']}\n",
        f"{inner}port: {entry['port']}\n",
        f"{inner}protocol: {entry['protocol']}\n",
        f"{inner}reason: {json.dumps(entry['reason'], ensure_ascii=False)}\n",
        f"{inner}evidence: {json.dumps(entry['evidence'], ensure_ascii=False)}\n",
    ]


def add_egress_entries(
    path: str, text: str | None, zone: str, contact: str, entries: list[dict[str, Any]]
) -> tuple[str, list[str], list[str]]:
    """The file's text with the new entries added (existing lines untouched, each new entry before the first existing
    one whose fqdn sorts after it), the destinations added and the ones already there."""
    if text is None or not text.strip():
        text = f"zone: {zone}\ncontact: {contact}\negress:\n"
    data = _load_egress_file(path, text)
    if data.get("zone") != zone:
        raise ToolError(f"{path} is for zone {data.get('zone')}, not {zone}")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, line in enumerate(lines) if re.match(r"^egress:\s*(\[\])?\s*$", line)), None)
    if start is None:
        raise ToolError(f"{path} has no top-level egress list")
    lines[start] = "egress:\n"
    # The list's items: from the egress line to the next top-level key.
    top_level = [i for i in range(start + 1, len(lines)) if lines[i].strip() and lines[i][0] not in " \t-#"]
    end = top_level[0] if top_level else len(lines)
    blocks: list[list[str]] = []
    indent = "  "
    for line in lines[start + 1 : end]:
        match = _ITEM.match(line)
        if match and (not blocks or len(match.group(1)) == len(indent)):
            indent = match.group(1)
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        elif line.strip():
            raise ToolError(f"{path}: unexpected text before the first egress entry: {line.strip()}")
    existing = [_entry_key(e) for e in data.get("egress") or [] if isinstance(e, dict)]
    if len(existing) != len(blocks):
        raise ToolError(f"{path}: could not tell its egress entries apart; a human has to add this one")
    added, present = [], []
    for entry in entries:
        key = _entry_key(entry)
        if key in existing:
            present.append(entry["fqdn"])
            continue
        at = next((i for i, k in enumerate(existing) if k > key), len(existing))
        existing.insert(at, key)
        blocks.insert(at, _entry_block(indent, entry))
        added.append(entry["fqdn"])
    result = "".join(lines[: start + 1] + [line for block in blocks for line in block] + lines[end:])
    keep_egress_entries(path, text if data.get("egress") else None, result)
    return result, added, present


@tool(
    "firewall_egress_add",
    "Add destinations to an application namespace's egress file in your firewall workspace "
    f"({APPLICATION_FILES}/<spoke>/<namespace>.yaml), creating it if missing. Existing entries stay exactly as they "
    "are; a destination already there is reported, not added again. Then create_pull_request.",
    {
        "repo": {"type": "string", "description": "Repository key of the landing zone, usually firewall"},
        "spoke": {"type": "string", "description": "e.g. dev-int (from the flows' answer)"},
        "namespace": {"type": "string"},
        "zone": {"type": "string", "description": "The namespace's isolation zone, e.g. int-se"},
        "contact": {"type": "string", "description": "The namespace's contact (used only for a new file)"},
        "entries": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "fqdn": {"type": "string"},
                    "port": {"type": "integer", "enum": [80, 443]},
                    "protocol": {"type": "string", "enum": ["http", "https"]},
                    "reason": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string", "minLength": 1},
                },
                "required": ["fqdn", "reason", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    required=["repo", "spoke", "namespace", "zone", "contact", "entries"],
)
def firewall_egress_add(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    spoke, namespace, zone = args["spoke"], args["namespace"], args["zone"]
    for name in (spoke, namespace, zone):
        if not _NAME.match(name):
            raise ToolError(f"not a valid name: {name!r}")
    if not namespace.startswith(f"{zone}-"):
        raise ToolError(f"namespace {namespace} is not an application namespace of zone {zone} (<zone>-<app>)")
    entries = []
    for e in args["entries"]:
        fqdn = e["fqdn"].strip().lower().rstrip(".")
        if not _FQDN.match(fqdn) or fqdn.rsplit(".", 1)[-1].isdigit():
            raise ToolError(f"not a destination name: {e['fqdn']!r} (no IP addresses, no bare wildcards)")
        protocol = e.get("protocol") or ("http" if e.get("port") == 80 else "https")
        port = e.get("port") or (80 if protocol == "http" else 443)
        entries.append({**e, "fqdn": fqdn, "protocol": protocol, "port": port})
    path = f"{APPLICATION_FILES}/{spoke}/{namespace}.yaml"
    _check_repo(ctx, "repo_write", args["repo"])
    _check_write_path(ctx, args["repo"], path)
    file = safe_join(workspace(ctx, args["repo"]), path)
    text, added, present = add_egress_entries(
        path, file.read_text() if file.is_file() else None, zone, args["contact"], entries
    )
    if added:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    return {"path": path, "added": added, "alreadyThere": present}


FILE_CHECKS.append((f"{APPLICATION_FILES}/*/*.yaml", keep_egress_entries))
