"""Traffic flow documentation (design section 5.3): one picture per application and one per isolation zone, from Git.

The hub firewall cannot say which application uses a rule - its rules name zones (IP groups), not pods. But the
rules are written per application: the egress agent's files ``firewall-applications/<spoke>/<namespace>.yaml`` carry,
for every destination, the namespace that asked for it, why (the line of its configuration) and the evidence (the
denied flows). This module joins those files with what the application's own configuration says (its Flux
Kustomization in the team's repository), the zone's allow-list (the fleet's ``policy/egress-allow-lists.yaml``) and
the cells it runs in (aks-terraform's ``envs/*.tfvars``), and writes Markdown pages with Mermaid pictures:

- per application (``docs/traffic-flows/<spoke>/<namespace>.md``): who reaches it (gateway, hostnames), what it
  runs (workloads, images) and every destination outside the cluster with its status - allowed and named in the
  configuration, allowed but not named by it, named but without a rule (denied), proposed in an open pull request,
  or open to the whole zone through the zone's allow-list;
- per zone (``docs/traffic-flows/<spoke>/zone-<zone>.md``): every application of the zone and the union of their
  destinations - what the firewall really opens for the zone, and which applications asked for each name.

Everything here is deterministic: no model, no cluster access, Git only. ``generate`` returns the pages and the
findings (rules no configuration explains, destinations no rule allows); the documentation agent writes the pages into
a pull request and explains the findings. The pages carry no timestamp, so a run that changes nothing writes nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

APPLICATION_FILES = "stacks/platform-extensions/firewall-applications"
ALLOW_LISTS = "policy/egress-allow-lists.yaml"
DOCS_DIR = "docs/traffic-flows"

# A destination's status in an application's picture
ALLOWED = "allowed"  # a rule in the application's file, and its configuration names the destination
UNEXPLAINED = "unexplained"  # a rule in the application's file that its configuration does not name
MISSING = "missing"  # the configuration names it and no rule allows it: the firewall denies it
PROPOSED = "proposed"  # in an open pull request of the egress agent
ZONE = "zone"  # the zone's allow-list, open to every application of the zone (on an application page only when named)
PLATFORM = "platform"  # a platform rule of the exposure, open from every cell (likewise)

STATUS_TEXT = {
    ALLOWED: "allowed - the application's rule, named in its configuration",
    UNEXPLAINED: "allowed - the application's rule, but its configuration does not name it",
    MISSING: "no rule - the configuration names it, the firewall denies it",
    PROPOSED: "proposed - in an open pull request, not allowed yet",
    ZONE: "zone allow-list - open to every application of the zone",
    PLATFORM: "platform rule - open from every cell of the exposure",
}
PLATFORM_RULES_FILE = "stacks/platform-extensions/terraform.tfvars"

_URL = re.compile(r"https?://([a-z0-9][a-z0-9.-]*\.[a-z][a-z0-9-]*)(?::\d+)?(?=[/?#\s\"']|$)", re.I)
_FLUX_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::=([^}]*))?\}")
CLUSTER_SCOPED = ("Namespace", "CiliumClusterwideNetworkPolicy", "ClusterRole", "ClusterRoleBinding")


# ----------------------------------------------------------------------------------------------------------------
# What is documented


@dataclass
class Destination:
    fqdn: str
    port: int
    protocol: str
    status: str
    reason: str = ""
    evidence: str = ""
    declared_in: list[str] = field(default_factory=list)  # where the configuration names it
    pull_request: str | None = None
    rule: str | None = None  # the firewall rule the landing zone renders it into

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.fqdn.lower(), self.protocol.lower(), str(self.port))


@dataclass
class Workload:
    kind: str
    name: str
    replicas: int | None
    images: list[str]
    env_urls: list[tuple[str, str]]  # (variable, value) of every URL in the pod's environment and arguments


@dataclass
class Route:
    name: str
    hostnames: list[str]  # with the Flux variables still in them
    gateway: str  # namespace/name
    backends: list[str]


@dataclass
class Cell:
    name: str
    exposure: str
    acns: bool
    enabled: bool = True

    @property
    def model(self) -> str:
        if self.acns:
            return "pod subnet + Cilium FQDN policies: the firewall sees the pod's own address"
        return "pod subnet + hub firewall: the firewall sees the pod's own address"


@dataclass
class Application:
    namespace: str
    zone: str
    team: str = ""
    contact: str = ""
    source_url: str = ""
    source_path: str = ""
    zone_domain: str = ""
    home_cell: str = ""
    cells: list[Cell] = field(default_factory=list)
    workloads: list[Workload] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    destinations: list[Destination] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list, repr=False)  # the built configuration (labdocs.py)

    @property
    def slug(self) -> str:
        return repo_slug(self.source_url)

    def hostnames(self, cell: Cell) -> list[str]:
        return sorted({substitute(h, _values(self, cell)) for r in self.routes for h in r.hostnames})


def _values(app: Application, cell: Cell) -> dict[str, str]:
    """The Flux post-build variables aks-terraform gives the namespace's configuration in a cell."""
    return {
        "cluster_name": cell.name,
        "zone": app.zone,
        "zone_domain": app.zone_domain,
        "home_cell": app.home_cell,
        "cluster_exposure": cell.exposure,
    }


@dataclass
class Finding:
    namespace: str
    zone: str
    fqdn: str
    status: str
    detail: str


@dataclass
class Result:
    files: dict[str, str]  # path under the documentation directory -> content
    findings: list[Finding]
    applications: list[Application]

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": sorted(self.files),
            "findings": [asdict(f) for f in self.findings],
            "applications": [{"namespace": a.namespace, "zone": a.zone} for a in self.applications],
        }


def repo_slug(url: str) -> str:
    return url.lower().removeprefix("https://github.com/").removesuffix(".git").strip("/")


# ----------------------------------------------------------------------------------------------------------------
# Reading Git


def substitute(text: str, values: dict[str, str]) -> str:
    """Flux post-build substitution: ``${name}`` and ``${name:=default}``; an unknown name stays as written."""

    def one(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in values:
            return values[name]
        return default if default is not None else m.group(0)

    return _FLUX_VAR.sub(one, text)


def read_application_files(landing_zone: Path, spoke: str) -> dict[str, dict[str, Any]]:
    """``firewall-applications/<spoke>/<namespace>.yaml`` -> namespace -> {zone, contact, egress}."""
    found: dict[str, dict[str, Any]] = {}
    for f in sorted((landing_zone / APPLICATION_FILES / spoke).glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
        if isinstance(data, dict):
            found[f.stem] = data
    return found


def read_allow_lists(fleet: Path) -> dict[str, dict[str, list[str]]]:
    """The fleet's zone allow-lists: zone -> {names, patterns}."""
    path = fleet / ALLOW_LISTS
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {
        zone: {"names": list(z.get("names") or []), "patterns": list(z.get("patterns") or [])}
        for zone, z in (data.get("zones") or {}).items()
    }


def _strings(block: str, key: str) -> list[str]:
    m = re.search(r'(?<![\w/])"?' + re.escape(key) + r'"?\s*=\s*\[([^\]]*)\]', block)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else []


def read_platform_rules(landing_zone: Path, exposure: str, env: str) -> dict[str, Any]:
    """The landing zone's ``firewall_exposures.<exposure>.environments.<env>``: the names its platform rules open from
    every cell (``observability_fqdns``) and the zones' ``fqdn_allow_list`` (to compare with the fleet's lists)."""
    path = landing_zone / PLATFORM_RULES_FILE
    empty: dict[str, Any] = {"observability": [], "zones": {}}
    if not path.is_file():
        return empty
    text = _strip_hcl_comments(path.read_text())
    m = re.search(r"(?m)^firewall_exposures\s*=\s*\{", text)
    if not m:
        return empty
    exposures = _entries(_block(text, m.end() - 1))
    environments = _entries(exposures.get(exposure, ""))
    envs = _entries(environments.get("environments", ""))
    block = envs.get(env, "")
    if not block:
        return empty
    zones = _entries(_entries(block).get("zones", ""))
    return {
        "observability": _strings(block, "observability_fqdns"),
        "zones": {zone: _strings(z, "fqdn_allow_list") for zone, z in zones.items()},
    }


def _strip_hcl_comments(text: str) -> str:
    out: list[str] = []
    quoted, i = False, 0
    while i < len(text):
        c = text[i]
        if c == '"' and (i == 0 or text[i - 1] != "\\"):
            quoted = not quoted
        if not quoted and (c == "#" or text.startswith("//", i)):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _block(text: str, start: int) -> str:
    """The text between the brace at ``start`` and its match."""
    depth, i, quoted = 0, start, False
    while i < len(text):
        c = text[i]
        if c == '"' and text[i - 1] != "\\":
            quoted = not quoted
        elif not quoted:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1 : i]
        i += 1
    return text[start + 1 :]


def _string(block: str, key: str) -> str:
    m = re.search(r'(?<![\w/])"?' + re.escape(key) + r'"?\s*=\s*"([^"]*)"', block)
    return m.group(1) if m else ""


def _entries(block: str) -> dict[str, str]:
    """Top-level ``name = { ... }`` entries of an HCL map, by name."""
    found: dict[str, str] = {}
    pattern = re.compile(r'(?m)^\s*"?([\w./-]+)"?\s*=\s*\{')
    i = 0
    while True:
        m = pattern.search(block, i)
        if not m:
            return found
        inner = _block(block, m.end() - 1)
        found[m.group(1)] = inner
        i = m.end() + len(inner)


def read_cells(iac: Path, env: str) -> dict[str, Cell]:
    """The environment's cells from ``envs/<env>-*.tfvars`` and ``shared/envs/<env>.tfvars`` (enabled, pod network)."""
    shared_cells: dict[str, str] = {}
    shared = iac / "shared" / "envs" / f"{env}.tfvars"
    if shared.is_file():
        text = _strip_hcl_comments(shared.read_text())
        m = re.search(r"(?m)^cells\s*=\s*\{", text)
        if m:
            shared_cells = _entries(_block(text, m.end() - 1))
    cells: dict[str, Cell] = {}
    for f in sorted((iac / "envs").glob(f"{env}-*.tfvars")):
        text = _strip_hcl_comments(f.read_text())
        name = re.search(r'(?m)^name\s*=\s*"([^"]*)"', text)
        if not name:
            continue
        settings = shared_cells.get(f.stem, "")
        # A cell the environment's shared tfvars does not list (the stateful cluster, defined but never deployed) is
        # not built; one listed with enabled = false is disabled.
        enabled = (f.stem in shared_cells or not shared_cells) and not re.search(r"enabled\s*=\s*false", settings)
        cells[name.group(1)] = Cell(
            name=name.group(1),
            exposure=_string(text, "exposure") or "int",
            # ACNS only where the cell sets it: the variable's default is null (design v2.0, D80)
            acns=bool(re.search(r"(?m)^advanced_networking\s*=\s*\{", text)),
            enabled=enabled,
        )
    return cells


def read_managed_namespaces(iac: Path, env: str) -> dict[str, dict[str, dict[str, Any]]]:
    """``managed_namespaces`` of every cell's tfvars: namespace -> cluster -> {team, contact, zone, url, path,
    zone_domain, home_cell} (a cell may sync another path of the same repository, e.g. the stateful overlay)."""
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for f in sorted((iac / "envs").glob(f"{env}-*.tfvars")):
        text = _strip_hcl_comments(f.read_text())
        cluster = re.search(r'(?m)^name\s*=\s*"([^"]*)"', text)
        m = re.search(r"(?m)^managed_namespaces\s*=\s*\{", text)
        if not cluster or not m:
            continue
        for namespace, block in _entries(_block(text, m.end() - 1)).items():
            found.setdefault(namespace, {})[cluster.group(1)] = {
                "team": _string(block, "team"),
                "contact": _string(block, "contact"),
                "zone": _string(block, "platform/zone") or _string(block, "zone"),
                "url": _string(block, "url"),
                "path": _string(block, "path").removeprefix("./").strip("/") or ".",
                "zone_domain": _string(block, "zone_domain"),
                "home_cell": _string(block, "home_cell"),
            }
    return found


# ----------------------------------------------------------------------------------------------------------------
# The application's configuration: its Kustomization, built


def _docs(path: Path) -> list[dict[str, Any]]:
    try:
        return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def _kustomization(directory: Path) -> Path | None:
    names = ("kustomization.yaml", "kustomization.yml")
    return next((directory / n for n in names if (directory / n).is_file()), None)


def _pointer(obj: Any, path: str, create: bool = False) -> tuple[Any, str]:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.strip("/").split("/")]
    cur = obj
    for p in parts[:-1]:
        if isinstance(cur, list):
            cur = cur[int(p)]
        else:
            if p not in cur and create:
                cur[p] = {}
            cur = cur[p]
    return cur, parts[-1]


def _json_patch(obj: dict[str, Any], ops: list[dict[str, Any]]) -> None:
    for op in ops:
        try:
            parent, last = _pointer(obj, op["path"], create=op.get("op") == "add")
            if op["op"] == "add":
                if isinstance(parent, list):
                    parent.insert(len(parent) if last == "-" else int(last), op.get("value"))
                else:
                    parent[last] = op.get("value")
            elif op["op"] == "replace":
                parent[int(last) if isinstance(parent, list) else last] = op.get("value")
            elif op["op"] == "remove":
                del parent[int(last) if isinstance(parent, list) else last]
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # a patch the real build would refuse too; the picture shows the rest


def _named_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) and "name" in v for v in value)


def _merge(base: Any, patch: Any) -> Any:
    """A strategic merge: maps key by key, lists of named items (containers, env, ports) item by item by name."""
    if isinstance(base, dict) and isinstance(patch, dict):
        for k, v in patch.items():
            base[k] = _merge(base.get(k), v) if k in base else v
        return base
    if _named_list(base) and _named_list(patch):
        by_name = {item["name"]: item for item in base}
        for item in patch:
            if item["name"] in by_name:
                _merge(by_name[item["name"]], item)
            else:
                base.append(item)
        return base
    return patch


def _matches(obj: dict[str, Any], target: dict[str, Any] | None) -> bool:
    if not target:
        return True
    meta = obj.get("metadata") or {}
    return all(
        [
            not target.get("kind") or obj.get("kind") == target["kind"],
            not target.get("name") or re.fullmatch(str(target["name"]), meta.get("name", "")),
            not target.get("namespace") or meta.get("namespace") == target["namespace"],
        ]
    )


def _apply_patch(objects: list[dict[str, Any]], patch: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    text = patch.get("patch")
    if not text and patch.get("path") and (directory / patch["path"]).is_file():
        text = (directory / patch["path"]).read_text()
    try:
        body = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return objects
    target = patch.get("target")
    if isinstance(body, list):
        for o in objects:
            if _matches(o, target):
                _json_patch(o, body)
        return objects
    if not isinstance(body, dict):
        return objects
    target = target or {"kind": body.get("kind"), "name": (body.get("metadata") or {}).get("name")}
    if body.get("$patch") == "delete":
        return [o for o in objects if not _matches(o, target)]
    for o in objects:
        if _matches(o, target):
            _merge(o, {k: v for k, v in body.items() if k not in ("apiVersion", "kind")})
    return objects


def _transform(k: dict[str, Any], directory: Path, objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A Kustomization's (or Component's) namespace, images, replicas and patches, applied to ``objects``."""
    if k.get("namespace"):
        for o in objects:
            if o.get("kind") not in CLUSTER_SCOPED:
                o.setdefault("metadata", {})["namespace"] = k["namespace"]  # kustomize's: the outer one wins
    for image in k.get("images") or []:
        for o in objects:
            for c in (((o.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
                ref, _, digest = str(c.get("image", "")).partition("@")
                name, _, tag = ref.partition(":")
                if name == image.get("name"):
                    name = image.get("newName") or name
                    tag = str(image.get("newTag") or tag)
                    digest = str(image.get("digest") or digest)
                    c["image"] = (f"{name}:{tag}" if tag else name) + (f"@{digest}" if digest else "")
    for rep in k.get("replicas") or []:
        for o in objects:
            named = (o.get("metadata") or {}).get("name") == rep.get("name")
            if o.get("kind") in ("Deployment", "StatefulSet") and named:
                o.setdefault("spec", {})["replicas"] = rep.get("count")
    for patch in k.get("patches") or []:
        objects = _apply_patch(objects, patch, directory)
    return objects


def _build(directory: Path, seen: set[Path] | None = None) -> list[dict[str, Any]]:
    """A small kustomize: resources (files and directories), components, namespace, images, replicas and
    patches (JSON 6902, strategic merge, ``$patch: delete``) - enough to read what an application runs and names."""
    seen = seen if seen is not None else set()
    directory = directory.resolve()
    if directory in seen:
        return []
    seen.add(directory)
    kfile = _kustomization(directory)
    if kfile is None:
        return [d for f in sorted(directory.glob("*.y*ml")) for d in _docs(f)]
    k = yaml.safe_load(kfile.read_text()) or {}
    objects: list[dict[str, Any]] = []
    for item in k.get("resources") or []:
        target = (directory / str(item)).resolve()
        if target.is_dir():
            objects += _build(target, seen)
        elif target.is_file():
            objects += _docs(target)
    # A component adds its own resources to the parent's and transforms the parent's objects
    for item in k.get("components") or []:
        target = (directory / str(item)).resolve()
        cfile = _kustomization(target) if target.is_dir() else None
        if cfile is None or target in seen:
            continue
        seen.add(target)
        component = yaml.safe_load(cfile.read_text()) or {}
        for res in component.get("resources") or []:
            path = (target / str(res)).resolve()
            objects += _build(path, seen) if path.is_dir() else _docs(path) if path.is_file() else []
        objects = _transform(component, target, objects)
    return _transform(k, directory, objects)


def build_manifests(repo: Path, path: str) -> list[dict[str, Any]]:
    """The application's objects: ``kustomize build`` when it is installed, otherwise the small build above."""
    directory = repo / path
    if shutil.which("kustomize"):
        try:
            out = subprocess.run(
                ["kustomize", "build", str(directory)], capture_output=True, text=True, timeout=120, check=True
            ).stdout
            return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]
        except (subprocess.SubprocessError, yaml.YAMLError):
            pass
    return _build(directory)


def _urls(value: Any, out: list[tuple[str, str]], name: str) -> None:
    if isinstance(value, str):
        out.extend((name, m.group(0)) for m in _URL.finditer(value))
    elif isinstance(value, list):
        for v in value:
            _urls(v, out, name)
    elif isinstance(value, dict):
        for k, v in value.items():
            _urls(v, out, f"{name}.{k}" if name else str(k))


def describe(objects: list[dict[str, Any]], namespace: str) -> tuple[list[Workload], list[Route], list[str]]:
    """Workloads (with the URLs their environment names), routes and services of one namespace."""
    workloads, routes, services = [], [], []
    for o in objects:
        meta = o.get("metadata") or {}
        if meta.get("namespace") not in (None, namespace):
            continue
        kind, spec = o.get("kind"), o.get("spec") or {}
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"):
            pod = (spec.get("template") or {}).get("spec") or {}
            if kind == "CronJob":
                job = (spec.get("jobTemplate") or {}).get("spec") or {}
                pod = (job.get("template") or {}).get("spec") or {}
            urls: list[tuple[str, str]] = []
            images = []
            for c in list(pod.get("containers") or []) + list(pod.get("initContainers") or []):
                images.append(str(c.get("image", "")))
                for e in c.get("env") or []:
                    _urls(e.get("value"), urls, str(e.get("name")))
                _urls(c.get("args"), urls, "args")
                _urls(c.get("command"), urls, "command")
            workloads.append(Workload(kind, meta.get("name", ""), spec.get("replicas"), images, urls))
        elif kind in ("HTTPRoute", "GRPCRoute", "TLSRoute"):
            p = (spec.get("parentRefs") or [{}])[0]
            backends = sorted({str(b.get("name")) for r in spec.get("rules") or [] for b in r.get("backendRefs") or []})
            hostnames = [str(h) for h in spec.get("hostnames") or []]
            gateway = f"{p.get('namespace', namespace)}/{p.get('name', '')}"
            routes.append(Route(meta.get("name", ""), hostnames, gateway, backends))
        elif kind == "Service":
            services.append(meta.get("name", ""))
        elif kind == "ConfigMap":
            urls = []
            _urls(o.get("data"), urls, meta.get("name", "configmap"))
            if urls:
                workloads.append(Workload("ConfigMap", meta.get("name", ""), None, [], urls))
    return workloads, routes, services


def _referenced_files(repo: Path, path: str) -> list[Path]:
    """The Kustomization directory's files and, recursively, the directories its resources and components name."""
    out: list[Path] = []
    seen: set[Path] = set()
    root = repo.resolve()

    def visit(directory: Path) -> None:
        directory = directory.resolve()
        if directory in seen or not directory.is_dir() or (root not in directory.parents and directory != root):
            return
        seen.add(directory)
        out.extend(sorted(p for p in directory.glob("*.y*ml") if p.is_file()))
        kfile = _kustomization(directory)
        if kfile is None:
            return
        try:
            k = yaml.safe_load(kfile.read_text()) or {}
        except yaml.YAMLError:
            return
        for item in list(k.get("resources") or []) + list(k.get("components") or []):
            target = directory / str(item)
            if target.is_dir():
                visit(target)

    visit(repo / path)
    return out


def mentioned_names(repo: Path, path: str, names: list[str]) -> dict[str, list[str]]:
    """Where the configuration's files mention each name as text (comments included) - weaker than a URL in the
    environment, but still the team's word for it."""
    found: dict[str, list[str]] = {}
    wanted = [n.lower().removeprefix("*.") for n in names if n]
    if not wanted:
        return found
    for f in _referenced_files(repo, path):
        try:
            text = f.read_text(errors="replace").lower()
        except OSError:
            continue
        rel = str(f.resolve().relative_to(repo.resolve()))
        for name in wanted:
            if name in text:
                found.setdefault(name, []).append(f"mentioned in {rel}")
    return found


# ----------------------------------------------------------------------------------------------------------------
# Joining: what the rules allow against what the configuration names


def _name_matches(fqdn: str, host: str) -> bool:
    fqdn, host = fqdn.lower().rstrip("."), host.lower().rstrip(".")
    if fqdn.startswith("*."):
        return host.endswith(fqdn[1:]) and host.count(".") >= fqdn.count(".")
    return fqdn == host


def _in_cluster(host: str) -> bool:
    return "." not in host or host.endswith((".svc", ".cluster.local", ".local", ".internal")) or host == "localhost"


def _declared(app: Application) -> dict[str, list[str]]:
    declared: dict[str, list[str]] = {}
    for w in app.workloads:
        for var, url in w.env_urls:
            m = _URL.match(url)
            host = m.group(1).lower() if m else ""
            if host and not _in_cluster(host):
                declared.setdefault(host, []).append(f"{w.kind} {w.name}, {var}")
    return declared


def classify(
    app: Application,
    entries: list[dict[str, Any]],
    allow_list: dict[str, list[str]],
    proposed: dict[tuple[str, str, str], str],
    rule_group: str,
    mentions: dict[str, list[str]] | None = None,
    platform_rules: list[str] | None = None,
) -> None:
    """Fills ``app.destinations`` from the application's rule file, its configuration, the zone's allow-list, the
    platform's rules and the open pull requests, each destination with its status. The zone's allow-list and the
    platform's rules belong to the zone's page: an application's page shows one of their names only when the
    application's own configuration names it (else it would just be noise on every page)."""
    declared = _declared(app)
    destinations: list[Destination] = []
    covered: set[str] = set()

    def named_by(name: str) -> list[str]:
        covered.update(host for host in declared if _name_matches(name, host))
        return [where for host, wheres in declared.items() if _name_matches(name, host) for where in wheres]

    for e in entries:
        if not isinstance(e, dict) or not e.get("fqdn"):
            continue
        fqdn = str(e["fqdn"]).lower()
        protocol = str(e.get("protocol", "https")).lower()
        port = int(e.get("port", 443))
        named = named_by(fqdn) + (mentions or {}).get(fqdn.removeprefix("*."), [])
        destinations.append(
            Destination(
                fqdn,
                port,
                protocol,
                ALLOWED if named else UNEXPLAINED,
                reason=str(e.get("reason", "")).strip(),
                evidence=str(e.get("evidence", "")).strip(),
                declared_in=named,
                rule=f"{rule_group}/{app.namespace}-{protocol}-{port}",
            )
        )
    for status, names in (
        (ZONE, list(allow_list.get("names", [])) + list(allow_list.get("patterns", []))),
        (PLATFORM, platform_rules or []),
    ):
        for name in names:
            named = named_by(name)
            if named:
                destinations.append(Destination(name.lower(), 443, "https", status, declared_in=named))
    for host, wheres in sorted(declared.items()):
        if host in covered:
            continue
        url = next((u for w in app.workloads for _, u in w.env_urls if host in u.lower()), "")
        protocol = "http" if url.lower().startswith("http://") else "https"
        port = 80 if protocol == "http" else 443
        pr = proposed.get((host, protocol, str(port)))
        status = PROPOSED if pr else MISSING
        destinations.append(Destination(host, port, protocol, status, declared_in=wheres, pull_request=pr))
    app.destinations = sorted(destinations, key=lambda d: (d.status in (ZONE, PLATFORM), d.fqdn))


def findings_of(app: Application) -> list[Finding]:
    out = []
    for d in app.destinations:
        if d.status == UNEXPLAINED:
            out.append(
                Finding(
                    app.namespace,
                    app.zone,
                    d.fqdn,
                    d.status,
                    f"the rule {d.rule} allows {d.fqdn}:{d.port}, but nothing in {app.source_path} of {app.slug} "
                    f"names it (rule's reason: {d.reason or '-'}). Still needed, or a rule to remove?",
                )
            )
        elif d.status == MISSING:
            out.append(
                Finding(
                    app.namespace,
                    app.zone,
                    d.fqdn,
                    d.status,
                    f"{'; '.join(d.declared_in)} names {d.fqdn}, and no rule of {app.namespace} or zone {app.zone} "
                    "allows it: the firewall denies it until the egress agent's pull request is merged",
                )
            )
    return out


# ----------------------------------------------------------------------------------------------------------------
# The pages


def _id(prefix: str, text: str) -> str:
    return prefix + re.sub(r"[^A-Za-z0-9]", "_", text)


def _label(text: str) -> str:
    """A Mermaid label inside quotes: no double quotes, pipes or square brackets."""
    return text.replace('"', "'").replace("|", "/").replace("[", "(").replace("]", ")")


_CLASSES = """    classDef ns fill:#e3f2fd,stroke:#1565c0,color:#000
    classDef platform fill:#eceff1,stroke:#455a64,color:#000
    classDef fw fill:#ede7f6,stroke:#5e35b1,color:#000
    classDef allowed fill:#e8f5e9,stroke:#2e7d32,color:#000
    classDef unexplained fill:#fff8e1,stroke:#ef6c00,color:#000
    classDef missing fill:#ffebee,stroke:#c62828,color:#000,stroke-dasharray: 5 5
    classDef proposed fill:#f3e5f5,stroke:#7b1fa2,color:#000,stroke-dasharray: 5 5
    classDef zone fill:#e0f2f1,stroke:#00695c,color:#000"""

_EDGE_TEXT = {
    ALLOWED: "allowed",
    UNEXPLAINED: "allowed, not in the configuration",
    MISSING: "denied: no rule",
    PROPOSED: "proposed",
    ZONE: "zone allow-list",
    PLATFORM: "platform rule",
}


def _edge(status: str, text: str) -> str:
    text = _label(text)
    if status in (MISSING, PROPOSED):
        return f'-.->|"{text}"|'
    return f'-->|"{text}"|'


def application_picture(app: Application) -> str:
    cell = next((c for c in app.cells if c.enabled), app.cells[0] if app.cells else None)
    gateway = f"{app.zone}-gateway/{app.zone}"
    if app.routes and cell:
        gateway = substitute(app.routes[0].gateway, _values(app, cell))
    internal = cell is not None and cell.exposure == "int"
    client = "Corporate network and VPN clients" if internal else "Internet via Traffic Manager"
    lines = ["flowchart LR", f'    client["{_label(client)}"]']
    cell_text = f"Cell {cell.name} - {cell.model}" if cell else "Cell"
    lines.append(f'    subgraph CELL["{_label(cell_text)}"]')
    lines.append("        direction LR")
    hosts = app.hostnames(cell) if cell else sorted({h for r in app.routes for h in r.hostnames})
    gw_label = f"Gateway {gateway}" + "".join(f"<br/>{h}" for h in hosts)
    lines.append(f'        gw["{_label(gw_label)}"]')
    ns_text = f"namespace {app.namespace} - team {app.team or '?'}"
    lines.append(f'        subgraph NS["{_label(ns_text)}"]')
    lines.append("            direction TB")
    for s in app.services:
        lines.append(f'            {_id("svc_", s)}["Service {_label(s)}"]')
    pods = [w for w in app.workloads if w.kind != "ConfigMap"]
    for w in pods:
        replicas = f" x{w.replicas}" if w.replicas is not None else ""
        label = f"{w.kind} {w.name}{replicas}" + "".join(f"<br/>{i}" for i in w.images)
        lines.append(f'            {_id("wl_", w.name)}["{_label(label)}"]')
    lines.append("        end")
    lines.append('        dns["CoreDNS"]')
    lines.append(f'        eso["ESO {_label(app.zone)} - Key Vault share {_label(app.namespace)}--*"]')
    lines.append("    end")
    groups = sorted({d.rule.split("/")[0] for d in app.destinations if d.rule})
    fw_label = "Hub firewall" + (f"<br/>rule collection group {groups[0]}" if groups else "")
    lines.append(f'    fw["{_label(fw_label)}"]')
    lines.append('    subgraph OUT["Outside the landing zone"]')
    lines.append("        direction TB")
    for d in app.destinations:
        lines.append(f'        {_id("d_", d.fqdn)}["{_label(d.fqdn)}:{d.port}"]')
    lines.append("    end")
    # Edges
    lines.append("    client --> gw")
    for b in sorted({b for r in app.routes for b in r.backends if b in app.services}):
        lines.append(f"    gw --> {_id('svc_', b)}")
    for s in app.services:
        for w in pods:
            lines.append(f"    {_id('svc_', s)} --> {_id('wl_', w.name)}")
    if pods:
        wid = _id("wl_", pods[0].name)
        lines.append(f"    {wid} --> dns")
        lines.append(f'    eso -.->|"Secrets"| {wid}')
        if app.destinations:
            lines.append(f'    {wid} -->|"every flow leaving the cluster"| fw')
    for d in app.destinations:
        lines.append(f"    fw {_edge(d.status, _EDGE_TEXT[d.status])} {_id('d_', d.fqdn)}")
    lines.append(_CLASSES)
    lines.append("    class gw,dns,eso platform")
    lines.append("    class fw fw")
    ns_ids = [_id("svc_", s) for s in app.services] + [_id("wl_", w.name) for w in pods]
    if ns_ids:
        lines.append(f"    class {','.join(ns_ids)} ns")
    for status in (ALLOWED, UNEXPLAINED, MISSING, PROPOSED, ZONE, PLATFORM):
        ids = [_id("d_", d.fqdn) for d in app.destinations if d.status == status]
        if ids:
            lines.append(f"    class {','.join(ids)} {status}")
    return "\n".join(lines) + "\n"


def _cell_text(c: Cell) -> str:
    return f"`{c.name}`" + ("" if c.enabled else " (not running)")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br/>").strip()


def application_page(app: Application, spoke: str) -> str:
    out = [
        f"# Traffic flows of `{app.namespace}`",
        "",
        "Generated by the documentation agent of onek8s/aks-ai-agents (`aks-agent docs traffic`) from Git alone - the",
        f"application's configuration, its firewall rules in `{APPLICATION_FILES}/{spoke}/{app.namespace}.yaml`,",
        "the zone's allow-list and the cells' settings. Do not edit by hand: the next run overwrites it.",
        "",
        "| | |",
        "|---|---|",
        f"| Isolation zone | `{app.zone}` |",
        f"| Team, contact | {app.team or '-'}, {app.contact or '-'} |",
        f"| Configuration | [{app.slug or app.source_url} / {app.source_path}]"
        f"({app.source_url}/tree/main/{app.source_path}) |",
        f"| Cells | {', '.join(_cell_text(c) for c in app.cells) or '-'} |",
        f"| Egress model | {'; '.join(sorted({c.model for c in app.cells if c.enabled})) or '-'} |",
        "",
        "```mermaid",
        application_picture(app).rstrip("\n"),
        "```",
        "",
        "## Egress: what leaves the cluster, and who asked for it",
        "",
        "The hub firewall's rules name the zone, not the application; the application's rule file does. Each row is",
        "one destination with its status, the reason the rule file gives and where the application's configuration",
        "names it.",
        "",
        "| Destination | Port | Status | Rule | Why (from the rule) | Named in the configuration | Evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in app.destinations:
        named = "<br/>".join(d.declared_in) or "-"
        rule = f"`{d.rule}`" if d.rule else (f"[pull request]({d.pull_request})" if d.pull_request else "-")
        out.append(
            f"| `{d.fqdn}` | {d.port}/{d.protocol} | {STATUS_TEXT[d.status]} | {rule} | {_cell(d.reason) or '-'} | "
            f"{named} | {_cell(d.evidence) or '-'} |"
        )
    if not app.destinations:
        out.append("| - | | nothing leaves the cluster: no rule, and the configuration names no destination | | | | |")
    out += [
        "",
        "The zone's allow-list and the platform's rules (open to every application of the zone, resp. from every cell)",
        f"are on the [zone's page](zone-{app.zone}.md); a name of theirs appears above only when this application's",
        "configuration names it.",
    ]
    out += ["", "## Ingress: how it is reached", ""]
    if app.routes:
        out += ["| Route | Hostnames | Gateway | Backends |", "|---|---|---|---|"]
        cells = [c for c in app.cells if c.enabled] or app.cells
        for r in app.routes:
            hosts = sorted({substitute(h, _values(app, c)) for c in cells for h in r.hostnames} or set(r.hostnames))
            gateway = substitute(r.gateway, _values(app, cells[0]) if cells else {})
            out.append(
                f"| `{r.name}` | {'<br/>'.join(f'`{h}`' for h in hosts)} | `{gateway}` | "
                f"{', '.join(f'`{b}`' for b in r.backends) or '-'} |"
            )
    else:
        out.append("No route: nothing outside the namespace reaches it through a gateway.")
    out += ["", "## Workloads", ""]
    if app.workloads:
        out += ["| Workload | Replicas | Images | URLs in its environment |", "|---|---|---|---|"]
        for w in app.workloads:
            urls = "<br/>".join(f"`{var}`: `{url}`" for var, url in w.env_urls) or "-"
            out.append(
                f"| {w.kind} `{w.name}` | {w.replicas if w.replicas is not None else '-'} | "
                f"{'<br/>'.join(f'`{i}`' for i in w.images) or '-'} | {urls} |"
            )
    else:
        out.append("No workload found in the configuration.")
    if app.notes:
        out += ["", "## Notes", ""] + [f"- {n}" for n in app.notes]
    out += ["", _legend(), ""]
    return "\n".join(out)


def _legend() -> str:
    return "\n".join(
        [
            "## Legend",
            "",
            "- **allowed** (green): a rule in the application's file, and the application's configuration names the",
            "  destination - the rule has a source.",
            "- **allowed, not in the configuration** (amber): a rule in the application's file that nothing in its",
            "  configuration names - a dependency the configuration hides (a library, a sidecar), or a rule to remove.",
            "- **denied: no rule** (red, dashed): the configuration names it and no rule allows it - the firewall",
            "  denies it until the egress agent's pull request is merged.",
            "- **proposed** (purple, dashed): in an open pull request of the egress agent.",
            "- **zone allow-list** (teal): on the zone's allow-list, open to every application of the zone; on an",
            "  application's page only when its configuration names it.",
            "- **platform rule** (grey): a rule of the platform for every cell of the exposure (the registry, Grafana",
            "  Cloud, ...); on an application's page only when its configuration names it.",
            "",
            "A rule opens its destination to the whole zone (the firewall tells zones apart, not applications within",
            "one - design section 5.2): the zone's page shows the union.",
        ]
    )


def _zone_destinations(apps: list[Application]) -> dict[tuple[str, int], dict[str, Any]]:
    by_dest: dict[tuple[str, int], dict[str, Any]] = {}
    for a in apps:
        for d in a.destinations:
            if d.status in (ALLOWED, UNEXPLAINED):
                entry = by_dest.setdefault((d.fqdn, d.port), {"apps": [], "status": set()})
                entry["apps"].append(a.namespace)
                entry["status"].add(d.status)
    return by_dest


def zone_picture(
    zone: str, apps: list[Application], allow_list: dict[str, list[str]], platform_rules: list[str] | None = None
) -> str:
    lines = ["flowchart LR", f'    subgraph Z["Isolation zone {_label(zone)}"]', "        direction TB"]
    for a in apps:
        lines.append(f'        {_id("a_", a.namespace)}["{_label(a.namespace)}<br/>team {_label(a.team or "?")}"]')
    lines.append("    end")
    lines.append(f'    fw["Hub firewall<br/>source: IP group of zone {_label(zone)}"]')
    lines.append('    subgraph OUT["What the firewall opens for the zone"]')
    lines.append("        direction TB")
    by_dest = _zone_destinations(apps)
    for (fqdn, port), entry in sorted(by_dest.items()):
        asked = _label(", ".join(entry["apps"]))
        lines.append(f'        {_id("d_", fqdn)}["{_label(fqdn)}:{port}<br/>asked by {asked}"]')
    zone_names = list(allow_list.get("names", [])) + list(allow_list.get("patterns", []))
    for name in zone_names:
        lines.append(f'        {_id("z_", name)}["{_label(name)}:443<br/>zone allow-list"]')
    for name in platform_rules or []:
        lines.append(f'        {_id("p_", name)}["{_label(name)}:443<br/>platform rule, every cell"]')
    lines.append("    end")
    for a in apps:
        lines.append(f"    {_id('a_', a.namespace)} --> fw")
    statuses = {fqdn: (UNEXPLAINED if e["status"] == {UNEXPLAINED} else ALLOWED) for (fqdn, _p), e in by_dest.items()}
    for fqdn, status in sorted(statuses.items()):
        lines.append(f"    fw {_edge(status, _EDGE_TEXT[status])} {_id('d_', fqdn)}")
    for name in zone_names:
        lines.append(f"    fw {_edge(ZONE, _EDGE_TEXT[ZONE])} {_id('z_', name)}")
    for name in platform_rules or []:
        lines.append(f"    fw {_edge(PLATFORM, _EDGE_TEXT[PLATFORM])} {_id('p_', name)}")
    missing = sorted({d.fqdn for a in apps for d in a.destinations if d.status in (MISSING, PROPOSED)})
    if missing:
        lines.append(f'    none["Named by applications, no rule yet:<br/>{_label("<br/>".join(missing))}"]')
        lines.append('    fw -.-x|"denied"| none')
    lines.append(_CLASSES)
    lines.append("    class fw fw")
    if apps:
        lines.append(f"    class {','.join(_id('a_', a.namespace) for a in apps)} ns")
    for status in (ALLOWED, UNEXPLAINED):
        ids = [_id("d_", fqdn) for fqdn, s in sorted(statuses.items()) if s == status]
        if ids:
            lines.append(f"    class {','.join(ids)} {status}")
    if zone_names:
        lines.append(f"    class {','.join(_id('z_', n) for n in zone_names)} zone")
    if platform_rules:
        lines.append(f"    class {','.join(_id('p_', n) for n in platform_rules)} platform")
    if missing:
        lines.append("    class none missing")
    return "\n".join(lines) + "\n"


def zone_page(
    zone: str,
    spoke: str,
    apps: list[Application],
    allow_list: dict[str, list[str]],
    platform_rules: list[str] | None = None,
    notes: list[str] | None = None,
) -> str:
    cells = sorted({c.name for a in apps for c in a.cells if c.enabled})
    out = [
        f"# Traffic flows of isolation zone `{zone}`",
        "",
        "Generated by the documentation agent of onek8s/aks-ai-agents (`aks-agent docs traffic`). Do not edit by hand.",
        "",
        "The hub firewall opens every destination below to **every application of the zone** - its rules take the",
        "zone's IP group as their source (design section 5.2). The application pages say who asked for each one. "
        f"Cells: {', '.join(f'`{c}`' for c in cells) or '-'}.",
        "",
        "```mermaid",
        zone_picture(zone, apps, allow_list, platform_rules).rstrip("\n"),
        "```",
        "",
        "## Applications",
        "",
        "| Namespace | Team | Rules | Of them not named in the configuration | Named, no rule |",
        "|---|---|---|---|---|",
    ]
    for a in apps:
        rules = [d for d in a.destinations if d.status in (ALLOWED, UNEXPLAINED)]
        unexplained = sum(d.status == UNEXPLAINED for d in rules)
        missing = sum(d.status in (MISSING, PROPOSED) for d in a.destinations)
        out.append(
            f"| [`{a.namespace}`]({a.namespace}.md) | {a.team or '-'} | {len(rules)} | {unexplained} | {missing} |"
        )
    out += [
        "",
        "## Destinations the zone may reach",
        "",
        "| Destination | Port | Asked for by | Named in their configuration | Why |",
        "|---|---|---|---|---|",
    ]
    rows: dict[tuple[str, int], list[tuple[Application, Destination]]] = {}
    for a in apps:
        for d in a.destinations:
            if d.status in (ALLOWED, UNEXPLAINED):
                rows.setdefault((d.fqdn, d.port), []).append((a, d))
    for (fqdn, port), pairs in sorted(rows.items()):
        asked = ", ".join(f"[`{a.namespace}`]({a.namespace}.md)" for a, _ in pairs)
        named = ", ".join("yes" if d.status == ALLOWED else "**no**" for _, d in pairs)
        why = "<br/>".join(f"{a.namespace}: {_cell(d.reason) or '-'}" for a, d in pairs)
        out.append(f"| `{fqdn}` | {port} | {asked} | {named} | {why} |")
    for name in list(allow_list.get("names", [])) + list(allow_list.get("patterns", [])):
        out.append(f"| `{name}` | 443 | the zone's allow-list (platform) | - | open to every application of the zone |")
    for name in platform_rules or []:
        out.append(
            f"| `{name}` | 443 | a platform rule of the exposure | - | open from every cell: the platform's own flows |"
        )
    if not rows and not allow_list and not platform_rules:
        out.append("| - | | nothing: the zone's applications reach nothing outside the cluster | | |")
    if notes:
        out += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    out += ["", _legend(), ""]
    return "\n".join(out)


def index_page(spoke: str, zones: dict[str, list[Application]]) -> str:
    out = [
        f"# Traffic flows: `{spoke}`",
        "",
        "What every application of this exposure reaches, and who asked for each firewall rule - one page per",
        "isolation zone and one per application, generated from Git by the documentation agent of onek8s/aks-ai-agents",
        "(design section 5.3). Do not edit by hand.",
        "",
        "| Zone | Applications |",
        "|---|---|",
    ]
    for zone, apps in sorted(zones.items()):
        links = ", ".join(f"[`{a.namespace}`]({spoke}/{a.namespace}.md)" for a in apps)
        out.append(f"| [`{zone}`]({spoke}/zone-{zone}.md) | {links or '-'} |")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------------------------------------------
# All of it


@dataclass
class Sources:
    landing_zone: Path
    fleet: Path
    iac: Path
    spoke: str  # <env>-<exposure>, e.g. dev-int
    app_repos: dict[str, Path] = field(default_factory=dict)  # owner/repo (lower case) -> local clone
    proposed: dict[str, dict[tuple[str, str, str], str]] = field(default_factory=dict)  # namespace -> entry -> PR url

    @property
    def env(self) -> str:
        return self.spoke.split("-", 1)[0]

    @property
    def exposure(self) -> str:
        return self.spoke.split("-", 1)[1] if "-" in self.spoke else ""


def _chosen(per_cell: dict[str, dict[str, Any]], cells: dict[str, Cell]) -> dict[str, Any]:
    """The configuration documented: the home cell's, else the first enabled cell's (the stateful overlay of a cell
    that is not built would otherwise win by name), else the first."""
    home = next((v for c, v in per_cell.items() if v.get("home_cell") == c), None)
    enabled = next((v for c, v in per_cell.items() if cells[c].enabled), None)
    return home or enabled or next(iter(per_cell.values()), {})


def collect(sources: Sources) -> list[Application]:
    """Every application namespace of the spoke: from the cells' ``managed_namespaces`` and the rule files."""
    files = read_application_files(sources.landing_zone, sources.spoke)
    cells = read_cells(sources.iac, sources.env)
    managed = read_managed_namespaces(sources.iac, sources.env)
    allow_lists = read_allow_lists(sources.fleet)
    platform_rules = read_platform_rules(sources.landing_zone, sources.exposure, sources.env)["observability"]
    rule_group = f"{sources.spoke}-applications"
    apps: list[Application] = []
    for namespace in sorted(set(files) | set(managed)):
        per_cell = {
            c: v for c, v in managed.get(namespace, {}).items() if c in cells and cells[c].exposure == sources.exposure
        }
        if managed.get(namespace) and not per_cell:
            continue  # a namespace of another exposure's cells
        data = files.get(namespace, {})
        m = _chosen(per_cell, cells)
        zone = m.get("zone") or str(data.get("zone", ""))
        app = Application(
            namespace=namespace,
            zone=zone,
            team=m.get("team", ""),
            contact=m.get("contact") or str(data.get("contact", "")),
            source_url=m.get("url", ""),
            source_path=m.get("path", ""),
            zone_domain=m.get("zone_domain", ""),
            home_cell=m.get("home_cell", ""),
            cells=[cells[c] for c in per_cell],
        )
        other_paths = sorted({f"{c}: {v['path']}" for c, v in per_cell.items() if v.get("path") != m.get("path")})
        if other_paths:
            app.notes.append("Other cells sync another path of the same repository: " + ", ".join(other_paths) + ".")
        repo = sources.app_repos.get(app.slug)
        mentions: dict[str, list[str]] = {}
        entries = [e for e in data.get("egress") or [] if isinstance(e, dict)]
        if repo is not None and app.source_path:
            objects = build_manifests(repo, app.source_path)
            app.objects = objects
            app.workloads, app.routes, app.services = describe(objects, namespace)
            mentions = mentioned_names(repo, app.source_path, [str(e.get("fqdn", "")) for e in entries])
            if not objects:
                app.notes.append(f"No manifests found at {app.source_path} of {app.slug}.")
        elif app.source_url:
            app.notes.append(
                f"The configuration ({app.slug} / {app.source_path}) was not available to this run: the picture shows "
                "the rules only."
            )
        elif not m:
            app.notes.append(
                "No cell's managed_namespaces names this namespace: the rule file exists, the application may be gone."
            )
        proposed = sources.proposed.get(namespace, {})
        classify(app, entries, allow_lists.get(zone, {}), proposed, rule_group, mentions, platform_rules)
        apps.append(app)
    return apps


def generate(sources: Sources) -> Result:
    apps = collect(sources)
    allow_lists = read_allow_lists(sources.fleet)
    platform = read_platform_rules(sources.landing_zone, sources.exposure, sources.env)
    zones: dict[str, list[Application]] = {}
    for a in apps:
        zones.setdefault(a.zone or "unknown", []).append(a)
    files: dict[str, str] = {"README.md": index_page(sources.spoke, zones)}
    findings: list[Finding] = []
    for a in apps:
        files[f"{sources.spoke}/{a.namespace}.md"] = application_page(a, sources.spoke)
        findings += findings_of(a)
    for zone, zapps in zones.items():
        allow_list = allow_lists.get(zone, {})
        notes = []
        in_fleet = sorted(set(allow_list.get("names", [])) | set(allow_list.get("patterns", [])))
        in_tfvars = sorted(set(platform["zones"].get(zone, [])))
        if zone in platform["zones"] and in_fleet != in_tfvars:
            notes.append(
                f"The fleet's allow-list ({', '.join(in_fleet) or 'empty'}) and the landing zone's `fqdn_allow_list` "
                f"of the zone ({', '.join(in_tfvars) or 'empty'}) differ: the firewall enforces the latter, the pull "
                "request check reads the former - bring them in step."
            )
        files[f"{sources.spoke}/zone-{zone}.md"] = zone_page(
            zone, sources.spoke, zapps, allow_list, platform["observability"], notes
        )
    return Result(files, findings, apps)


def write(result: Result, out_dir: Path, spoke: str) -> tuple[list[str], list[str]]:
    """Writes the pages; removes stale pages of the spoke. Returns (changed, removed)."""
    changed, removed = [], []
    for rel, text in result.files.items():
        path = out_dir / rel
        if path.is_file() and path.read_text() == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        changed.append(rel)
    spoke_dir = out_dir / spoke
    if spoke_dir.is_dir():
        for stale in sorted(spoke_dir.glob("*.md")):
            rel = f"{spoke}/{stale.name}"
            if rel not in result.files:
                stale.unlink()
                removed.append(rel)
    return changed, removed


def diff(result: Result, out_dir: Path, spoke: str) -> list[str]:
    """The pages that differ from what ``out_dir`` holds (for a check that fails when the pages are stale)."""
    stale = [
        rel
        for rel, text in result.files.items()
        if not (out_dir / rel).is_file() or (out_dir / rel).read_text() != text
    ]
    spoke_dir = out_dir / spoke
    if spoke_dir.is_dir():
        stale += [
            f"{spoke}/{p.name}" for p in sorted(spoke_dir.glob("*.md")) if f"{spoke}/{p.name}" not in result.files
        ]
    return stale


def findings_text(findings: list[Finding]) -> str:
    if not findings:
        return "No findings: every rule is named in its application's configuration, every named destination has one."
    return "\n".join(f"- `{f.namespace}` ({f.zone}) `{f.fqdn}` - **{f.status}**: {f.detail}" for f in findings)


def summary_json(result: Result) -> str:
    return json.dumps(result.to_dict(), indent=1)
