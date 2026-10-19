"""The egress agent's deterministic pipeline (design D61): only new denials, each proposed once, the model last.

The model is the expensive part, so everything that needs no judgement runs here, in code, and the model sees only
what is left:

1. ``new``: the connections Log Analytics *ingested* since the last processed ingestion time (the state's
   checkpoint), but never further back than ``max_lookback`` (default 3 hours): a run after a long pause, or the first
   one, starts there and older events are ignored. Only denied connections by default (the query asks for
   ``Action == "Deny"``); ``include_allowed`` adds the allowed ones too, for an environment whose firewall still allows
   by default - every application's real egress then becomes an explicit rule before the default turns to deny (the
   firewall's log must keep allowed lines then: azure-landing-zone ``labfw.log_analytics.denied_only = false``). Bare
   IP destinations (no FQDN the firewall saw) are dropped: no application rule can name them. Sources already known to
   be nodes or platform pods, or to belong to a namespace with a pending (or recently rejected) proposal for that
   destination, are dropped too. Nothing left: the run ends here - no VPN, no cluster access, no model call.
2. ``propose``: the remaining source addresses are mapped to pods (read-only kubectl); node addresses and the
   platform's own namespaces are dropped; so is every (namespace, destination) already in the application's firewall
   file on the default branch, in an open pull request of this agent, or pending / rejected in the state. Only then
   is the model started, with the remaining flows in its task.

The state is a JSON file the workflow keeps between runs (GitHub Actions cache). Each run first refreshes it from
GitHub: a merged pull request's entries stay for an hour (the Platform workflow applies the rule), a closed one's are
kept as rejected for a week (a human said no; the same proposal is not made again until then).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STATE_VERSION = 1
MERGED_GRACE = dt.timedelta(hours=1)
REJECTED_SUPPRESS = dt.timedelta(days=7)
IP_MEMORY = dt.timedelta(hours=6)
DECLINED_SUPPRESS = dt.timedelta(days=1)
MAX_LOOKBACK = dt.timedelta(hours=3)
MAX_ROWS = 2000

KQL_NEW = """
{table}
| where ingestion_time() > datetime({start})
| where Action in ({actions}) and isnotempty(Fqdn)
| summarize Count = count(), FirstSeen = min(TimeGenerated), LastSeen = max(TimeGenerated),
            Ingested = max(ingestion_time()) by SourceIp, Fqdn, DestinationPort, Protocol, Action
| order by Ingested asc
| take {limit}
"""


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def key(namespace: str, fqdn: str, protocol: str, port: Any) -> str:
    return f"{namespace}|{fqdn.lower().rstrip('.')}|{str(protocol).lower()}|{port}"


@dataclass
class EgressState:
    checkpoint: str = "1970-01-01T00:00:00Z"
    proposals: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> {status, pr, ...}
    addresses: dict[str, dict[str, Any]] = field(default_factory=dict)  # source IP -> {namespace, zone, seen}

    @classmethod
    def load(cls, path: str | Path) -> EgressState:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            return cls()
        if data.get("version") != STATE_VERSION:
            return cls()
        return cls(data.get("checkpoint", cls.checkpoint), data.get("proposals", {}), data.get("addresses", {}))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "version": STATE_VERSION,
                    "checkpoint": self.checkpoint,
                    "proposals": self.proposals,
                    "addresses": self.addresses,
                },
                indent=1,
                sort_keys=True,
            )
        )

    def suppressed(self, k: str) -> bool:
        return k in self.proposals and self.proposals[k]["status"] in ("pending", "merged", "rejected", "declined")

    def forget_old(self, at: dt.datetime) -> None:
        for ip, entry in list(self.addresses.items()):
            if (_parse(entry.get("seen")) or at) < at - IP_MEMORY:
                del self.addresses[ip]
        for k, entry in list(self.proposals.items()):
            updated = _parse(entry.get("updated")) or at
            if (
                (entry["status"] == "merged" and updated < at - MERGED_GRACE)
                or (entry["status"] == "rejected" and updated < at - REJECTED_SUPPRESS)
                or (entry["status"] == "declined" and updated < at - DECLINED_SUPPRESS)
            ):
                del self.proposals[k]

    def forget_declined(self) -> int:
        declined = [k for k, e in self.proposals.items() if e["status"] == "declined"]
        for k in declined:
            del self.proposals[k]
        return len(declined)

    def refresh(self, pr_state, at: dt.datetime) -> None:
        """pr_state(repo, number) -> 'open' | 'merged' | 'closed' (GitHub, no model)."""
        for entry in self.proposals.values():
            if entry["status"] != "pending" or not entry.get("pr"):
                continue
            state = pr_state(entry["pr"]["repo"], entry["pr"]["number"])
            if state == "merged":
                entry.update(status="merged", updated=_iso(at))
            elif state == "closed":
                entry.update(status="rejected", updated=_iso(at))
        self.forget_old(at)


def window_start(state: EgressState, at: dt.datetime, max_lookback: dt.timedelta = MAX_LOOKBACK) -> str:
    """Where this run starts reading: the checkpoint, or ``max_lookback`` ago if the checkpoint is older."""
    checkpoint = _parse(state.checkpoint) or dt.datetime.min.replace(tzinfo=dt.UTC)
    return _iso(max(checkpoint, at - max_lookback))


def new_flows(
    query,
    state: EgressState,
    table: str,
    at: dt.datetime,
    max_lookback: dt.timedelta = MAX_LOOKBACK,
    include_allowed: bool = False,
) -> list[dict[str, Any]]:
    """Flows ingested since the checkpoint (at most ``max_lookback`` back); advances the checkpoint to the newest one.

    Denied flows only, unless ``include_allowed``. With nothing new the checkpoint stays: the next run continues from
    it (until it is older than ``max_lookback``)."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table {table!r}")
    actions = '"Deny", "Allow"' if include_allowed else '"Deny"'
    start = window_start(state, at, max_lookback)
    rows = query(KQL_NEW.format(table=table, start=start, actions=actions, limit=MAX_ROWS))
    newest = max(
        (_parse(r.get("Ingested")) for r in rows),
        default=None,
        key=lambda t: t or dt.datetime.min.replace(tzinfo=dt.UTC),
    )
    if newest:
        state.checkpoint = _iso(newest)
    return rows


def drop_known(rows: list[dict[str, Any]], state: EgressState, networks: list[Any]) -> list[dict[str, Any]]:
    """Flows from the cells whose source is not already known to belong to a namespace with this proposal."""
    kept = []
    for r in rows:
        try:
            ip = ipaddress.ip_address(r.get("SourceIp", ""))
        except ValueError:
            continue
        if networks and not any(ip in n for n in networks):
            continue
        known = state.addresses.get(str(ip))
        if known and known.get("ignore"):
            continue  # a node or a platform pod: no application to propose for
        ns = known.get("namespace") if known else None
        if ns and state.suppressed(key(ns, r["Fqdn"], r.get("Protocol", "https"), r.get("DestinationPort", 443))):
            continue
        kept.append(r)
    return kept


def attribute(
    rows: list[dict[str, Any]], mapping: dict[str, dict[str, Any]], state: EgressState, at: dt.datetime
) -> list[dict[str, Any]]:
    """Joins flows with their pods (pods_by_ip results by IP); remembers application pods' namespaces.

    The pods keep their own addresses up to the firewall (Azure CNI Pod Subnet). Nodes and the platform's pods
    are remembered as nothing to propose for."""
    out = []
    for r in rows:
        m = mapping.get(r["SourceIp"]) or {}
        if m.get("kind") != "pod" or not m.get("zone") or m.get("team") in (None, "platform"):
            if m.get("kind") in ("node", "pod"):
                # Remembered, so the next runs drop it before any cluster access
                state.addresses[r["SourceIp"]] = {"ignore": True, "kind": m["kind"], "seen": _iso(at)}
            continue
        state.addresses[r["SourceIp"]] = {"namespace": m["namespace"], "zone": m["zone"], "seen": _iso(at)}
        out.append({**r, "pod": m})
    return out


def existing_fqdns(file_text: str | None) -> set[tuple[str, str, str]]:
    if not file_text:
        return set()
    data = yaml.safe_load(file_text) or {}
    return {
        (str(e.get("fqdn", "")).lower(), str(e.get("protocol", "https")).lower(), str(e.get("port", 443)))
        for e in data.get("egress") or []
    }


def to_propose(
    flows: list[dict[str, Any]], state: EgressState, file_for, open_pr_files, at: dt.datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Per namespace, the flows not yet in its file on the default branch, an open pull request or the state.

    A flow an open pull request already proposes is recorded as pending with that pull request, so the next runs drop
    its source before any cluster access."""
    by_namespace: dict[str, list[dict[str, Any]]] = {}
    files: dict[str, set] = {}
    in_prs = open_pr_files()  # namespace -> {(fqdn, protocol, port): pull request {repo, number, url}}
    for f in flows:
        ns = f["pod"]["namespace"]
        if ns not in files:
            files[ns] = existing_fqdns(file_for(ns))
        proto, port = str(f.get("Protocol", "https")).lower(), str(f.get("DestinationPort", 443))
        entry = (f["Fqdn"].lower(), "https" if proto in ("https", "tls") else proto, port)
        if entry in files[ns]:
            continue
        pr = in_prs.get(ns, {}).get(entry)
        if pr:
            stamp = _iso(at or now())
            state.proposals.setdefault(
                key(ns, *entry), {"status": "pending", "pr": pr, "created": stamp, "updated": stamp}
            )
            continue
        if state.suppressed(key(ns, *entry)):
            continue
        by_namespace.setdefault(ns, []).append(f)
    return by_namespace


def record(
    state: EgressState,
    proposed: dict[str, list[dict[str, Any]]],
    outputs: list[dict[str, Any]],
    at: dt.datetime,
    decline: bool = True,
) -> None:
    """Marks every proposed (namespace, destination) pending with the pull request that carries it, and the ones
    the model proposed nothing for as declined for a day - so the same flows do not start the model again on every
    run."""

    def flow_key(ns: str, f: dict[str, Any]) -> str:
        proto = str(f.get("Protocol", "https")).lower()
        return key(ns, f["Fqdn"], "https" if proto in ("https", "tls") else proto, f.get("DestinationPort", 443))

    covered: set[str] = set()
    for out in outputs:
        if out.get("type") != "pull_request" or not out.get("number"):
            continue
        for path in out.get("files", []):
            ns = Path(path).stem
            for f in proposed.get(ns, []):
                k = flow_key(ns, f)
                covered.add(k)
                state.proposals[k] = {
                    "status": "pending",
                    "pr": {"repo": out["repo"], "number": out["number"], "url": out.get("url")},
                    "created": _iso(at),
                    "updated": _iso(at),
                }
    for ns, flows in proposed.items() if decline else ():
        for f in flows:
            k = flow_key(ns, f)
            if k not in covered and not state.suppressed(k):
                state.proposals[k] = {"status": "declined", "created": _iso(at), "updated": _iso(at)}


def task_text(proposed: dict[str, list[dict[str, Any]]], spoke: str) -> str:
    lines = [
        "These flows are new and not proposed yet - the platform already filtered them (only new, only application",
        "pods, not in the firewall files or open pull requests). `action` Deny: the firewall denied it; Allow: a broad",
        "rule or the default still allowed it - the application needs its own rule all the same.",
        "Do not call firewall_denied_flows or",
        f"pods_by_ip again. The firewall files are under stacks/platform-extensions/firewall-applications/{spoke}/.",
        "For each namespace: check the destination in its configuration (application_source with the flow's `cluster`",
        "and `namespace`, then repo_read), add its destinations with firewall_egress_add (it keeps the file's",
        "existing entries - never replace one) and open one pull request, as your instructions say. Then finish.",
        "",
        "```json",
        json.dumps(
            {
                ns: [
                    {
                        "sourceIp": f["SourceIp"],
                        "fqdn": f["Fqdn"],
                        "port": f.get("DestinationPort"),
                        "protocol": f.get("Protocol"),
                        "action": f.get("Action", "Deny"),
                        "count": f.get("Count"),
                        "firstSeen": f.get("FirstSeen"),
                        "lastSeen": f.get("LastSeen"),
                        "pod": {
                            k: f["pod"].get(k)
                            for k in (
                                "cluster",
                                "namespace",
                                "pod",
                                "workload",
                                "app",
                                "zone",
                                "team",
                                "contact",
                                "node",
                            )
                        },
                    }
                    for f in flows
                ]
                for ns, flows in proposed.items()
            },
            indent=1,
            default=str,
        ),
        "```",
    ]
    return "\n".join(lines)
