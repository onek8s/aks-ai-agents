"""The lab examples of the design site (``examples/lab/`` of onek8s.github.io): what Git deploys to the lab, per zone.

The test applications that run in the lab are documented next to the design, but outside it: one page per isolation
zone, with one section per application (namespace and workload), and an index. Everything comes from Git, read by
the traffic flow documentation's collection (trafficdocs.py: the cells, the application namespaces, their built
Flux configuration, the firewall rule files, the zone allow-lists, the egress statuses and findings, the pictures),
plus what only these pages show:

- the cell's settings (aks-terraform ``envs/*.tfvars``: location, availability zones, the Gateway implementation,
  the isolation zones and their classic node pools, with the defaults of ``variables.tf``);
- the zone's Gateway and domain (the fleet's ``clusters/<cell>/zones.yaml`` and ``infrastructure/zones``);
- per application its image, replicas, placement labels, Services, routes and environment, and - from the
  application's source repository, matched by the image's name - its description, version and endpoints.

Deterministic: no model, no cluster, no Azure, no timestamp, so a run that changes nothing writes nothing. The
pages are Markdown for GitHub Pages (kramdown), no front matter, the first line the H1 that titles the page; they
link only to each other and to GitHub.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import trafficdocs as t

DOCS_DIR = "examples/lab"
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job")
PLACEMENT_LABELS = ("platform/arch", "platform/capacity", "platform/accelerator")

COUNTRIES = {
    "at": "Austria",
    "be": "Belgium",
    "ch": "Switzerland",
    "de": "Germany",
    "dk": "Denmark",
    "ee": "Estonia",
    "es": "Spain",
    "fi": "Finland",
    "fr": "France",
    "ie": "Ireland",
    "it": "Italy",
    "lt": "Lithuania",
    "lv": "Latvia",
    "nl": "Netherlands",
    "no": "Norway",
    "pl": "Poland",
    "se": "Sweden",
    "uk": "United Kingdom",
    "us": "United States",
}


def country(zone: str) -> str:
    """``int-fi`` -> Finland; a zone without a known country suffix keeps its name."""
    return COUNTRIES.get(zone.rsplit("-", 1)[-1].lower(), zone) if "-" in zone else zone


def zone_title(zone: str) -> str:
    name = country(zone)
    return f"{zone} - {name}" if name != zone else zone


def anchor(heading: str) -> str:
    """kramdown's automatic id of a plain heading (the site's check computes the same)."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).strip().replace(" ", "-")


# ----------------------------------------------------------------------------------------------------------------
# Reading Git: the cells' settings, the fleet's zones, the applications' sources


@dataclass
class CellSettings:
    name: str
    stem: str  # the tfvars file's name, e.g. dev-int-stateless-az3 (the fleet's clusters/<stem>/)
    exposure: str
    enabled: bool
    location: str = ""
    availability_zones: list[str] = field(default_factory=list)
    gateway_implementation: str = ""
    zones: list[str] = field(default_factory=list)
    pools: dict[str, str] | None = None  # zone_node_pools with the variable's defaults; None: no zone pools
    zone_domains: dict[str, str] = field(default_factory=dict)  # from the fleet's zones.yaml


def _hcl_value(texts: list[str], key: str) -> str | None:
    """The last top-level ``key = <value>`` of the texts (a cell's own file wins over the files it shares)."""
    found = None
    for text in texts:
        m = re.search(r"(?m)^" + re.escape(key) + r"\s*=\s*(\"[^\"]*\"|null|true|false|[\d.]+)", text)
        if m:
            found = m.group(1)
    return found


def _hcl_block(texts: list[str], key: str) -> str | None:
    found = None
    for text in texts:
        m = re.search(r"(?m)^" + re.escape(key) + r"\s*=\s*(\{|null)", text)
        if m:
            found = None if m.group(1) == "null" else t._block(text, m.end() - 1)
    return found


def _attributes(block: str) -> dict[str, str]:
    """Simple ``name = value`` attributes of an HCL object (strings, numbers, booleans)."""
    out = {}
    for m in re.finditer(r'(?m)^\s*"?([\w-]+)"?\s*=\s*("([^"]*)"|[\w.]+)\s*,?\s*$', block):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def _variable(iac: Path, name: str) -> str:
    path = iac / "variables.tf"
    if not path.is_file():
        return ""
    text = t._strip_hcl_comments(path.read_text())
    m = re.search(r'(?m)^variable\s+"' + re.escape(name) + r'"\s*\{', text)
    return t._block(text, m.end() - 1) if m else ""


def _optional_defaults(variable: str) -> dict[str, str]:
    """``name = optional(type, default)`` of a variable's object type."""
    return {
        m.group(1): m.group(2).strip().strip('"')
        for m in re.finditer(r"(?m)^\s*(\w+)\s*=\s*optional\(\s*\w+\s*,\s*([^)\n]+)\)", variable)
    }


def _default(variable: str) -> str:
    m = re.search(r'(?m)^\s*default\s*=\s*("([^"]*)"|null)', variable)
    return (m.group(2) or "") if m else ""


def read_cell_settings(iac: Path, env: str, cells: dict[str, t.Cell]) -> dict[str, CellSettings]:
    """Per cell, its settings from its own tfvars and the files it shares (``envs/dev-int-stateless.tfvars`` for
    ``envs/dev-int-stateless-az3.tfvars``: a file whose name the cell's starts with)."""
    files = {f.stem: t._strip_hcl_comments(f.read_text()) for f in sorted((iac / "envs").glob(f"{env}-*.tfvars"))}
    pool_defaults = _optional_defaults(_variable(iac, "zone_node_pools"))
    gateway_default = _default(_variable(iac, "gateway_implementation"))
    out: dict[str, CellSettings] = {}
    for stem, text in files.items():
        name = re.search(r'(?m)^name\s*=\s*"([^"]*)"', text)
        if not name or name.group(1) not in cells:
            continue
        shared = sorted((s for s in files if s != stem and stem.startswith(s + "-")), key=len)
        texts = [files[s] for s in shared] + [text]
        cell = cells[name.group(1)]
        settings = CellSettings(name=cell.name, stem=stem, exposure=cell.exposure, enabled=cell.enabled)
        settings.location = (_hcl_value(texts, "location") or "").strip('"')
        pool = _hcl_block(texts, "default_node_pool") or ""
        settings.availability_zones = t._strings(pool, "availability_zones")
        gateway = _hcl_value(texts, "gateway_implementation")
        settings.gateway_implementation = gateway_default if gateway is None else gateway.strip('"').replace("null", "")
        zones = _hcl_block(texts, "isolation_zones")
        settings.zones = sorted(t._entries(zones)) if zones else []
        pools = _hcl_block(texts, "zone_node_pools")
        if pools is not None:
            settings.pools = {**pool_defaults, **_attributes(pools)}
        out[cell.name] = settings
    return out


def read_fleet_zones(fleet: Path, stem: str) -> dict[str, str]:
    """The fleet's zones of a cell (``clusters/<stem>/zones.yaml``): zone -> its domain."""
    path = fleet / "clusters" / stem / "zones.yaml"
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for doc in t._docs(path):
        spec = doc.get("spec") or {}
        if str(spec.get("path", "")).rstrip("/") != "./infrastructure/zones":
            continue
        values = (spec.get("postBuild") or {}).get("substitute") or {}
        if values.get("zone"):
            found[str(values["zone"])] = str(values.get("zone_domain", ""))
    return found


def read_gateway_listeners(fleet: Path) -> list[dict[str, Any]]:
    """The listeners of the zone Gateway the fleet applies in every zone (``infrastructure/zones``)."""
    for f in sorted((fleet / "infrastructure" / "zones").glob("*.y*ml")):
        for doc in t._docs(f):
            if doc.get("kind") == "Gateway":
                return [dict(listener) for listener in (doc.get("spec") or {}).get("listeners") or []]
    return []


@dataclass
class AppSource:
    slug: str  # owner/repo
    description: str = ""
    version: str = ""
    endpoints: list[tuple[str, str]] = field(default_factory=list)  # (path, what), from the README's table


def _readme(repo: Path) -> str:
    for name in ("README.md", "readme.md", "README"):
        if (repo / name).is_file():
            return (repo / name).read_text(errors="replace")
    return ""


def _description(readme: str) -> str:
    """The README's first paragraph after its first heading."""
    lines = readme.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith("# ")), 0)
    paragraph: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if paragraph:
                break
            continue
        if line.startswith(("#", "```", "|", "<", "![", "[![")):
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return " ".join(paragraph)


def _endpoints(readme: str) -> list[tuple[str, str]]:
    """The rows of the table under a heading named Endpoints (first column a path)."""
    m = re.search(r"(?mi)^#+\s+(API\s+)?endpoints\s*$", readme)
    if not m:
        return []
    out = []
    for line in readme[m.end() :].splitlines()[1:]:
        if line.startswith("#"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(cells) >= 2 and cells[0].strip("`").startswith("/"):
            out.append((cells[0].strip("`"), ", ".join(cells[1:])))
    return out


def _version(repo: Path) -> str:
    for f in sorted(repo.rglob("*.csproj")):
        m = re.search(r"<Version>\s*([^<\s]+)\s*</Version>", f.read_text(errors="replace"))
        if m:
            return m.group(1)
    package = repo / "package.json"
    if package.is_file():
        try:
            return str(json.loads(package.read_text()).get("version", ""))
        except ValueError:
            pass
    for name in ("pyproject.toml", "Cargo.toml"):
        if (repo / name).is_file():
            m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (repo / name).read_text())
            if m:
                return m.group(1)
    if (repo / "VERSION").is_file():
        return (repo / "VERSION").read_text().strip()
    return ""


def read_app_source(slug: str, repo: Path) -> AppSource:
    readme = _readme(repo)
    return AppSource(t.repo_slug(slug), _description(readme), _version(repo), _endpoints(readme))


def image_parts(image: str) -> tuple[str, str, str]:
    """``registry/path/name:tag@digest`` -> (repository, tag, digest)."""
    ref, _, digest = image.partition("@")
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :], digest
    return ref, "", digest


def match_source(image: str, sources: dict[str, AppSource]) -> AppSource | None:
    """The application source whose repository name is the image repository's last path segment."""
    name = image_parts(image)[0].rsplit("/", 1)[-1].lower()
    return next((s for slug, s in sorted(sources.items()) if slug.rsplit("/", 1)[-1] == name), None)


# ----------------------------------------------------------------------------------------------------------------
# What a section documents


@dataclass
class Port:
    name: str
    port: Any
    target: Any
    app_protocol: str


@dataclass
class Service:
    name: str
    type: str
    ports: list[Port]


@dataclass
class RouteInfo:
    name: str
    hostnames: list[str]
    paths: list[str]
    backends: list[str]
    gateway: str
    home_cell: str


@dataclass
class Section:
    app: t.Application
    kind: str
    name: str
    replicas: Any
    images: list[str]
    labels: dict[str, str]  # placement labels of the pod
    env: list[tuple[str, str]]
    services: list[Service]
    routes: list[RouteInfo]
    source: AppSource | None = None

    @property
    def heading(self) -> str:
        return f"{self.name} in {self.app.namespace}"


def _pod_spec(obj: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(pod metadata, pod spec) of a workload."""
    spec = obj.get("spec") or {}
    if obj.get("kind") == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    template = spec.get("template") or {}
    return template.get("metadata") or {}, template.get("spec") or {}


def _in(obj: dict[str, Any], namespace: str) -> bool:
    return (obj.get("metadata") or {}).get("namespace") in (None, namespace)


def sections_of(app: t.Application, sources: dict[str, AppSource]) -> list[Section]:
    cell = _cell(app)
    values = t._values(app, cell) if cell else {}
    objects = [o for o in app.objects if _in(o, app.namespace)]
    services = []
    for o in objects:
        if o.get("kind") != "Service":
            continue
        spec = o.get("spec") or {}
        ports = [
            Port(
                str(p.get("name", "")), p.get("port"), p.get("targetPort", p.get("port")), str(p.get("appProtocol", ""))
            )
            for p in spec.get("ports") or []
        ]
        services.append(Service((o.get("metadata") or {}).get("name", ""), str(spec.get("type", "ClusterIP")), ports))
    routes = []
    for o in objects:
        if o.get("kind") not in ("HTTPRoute", "GRPCRoute", "TLSRoute"):
            continue
        meta, spec = o.get("metadata") or {}, o.get("spec") or {}
        parent = (spec.get("parentRefs") or [{}])[0]
        rules = spec.get("rules") or []
        paths = sorted(
            {
                str((m.get("path") or {}).get("value", "/"))
                for r in rules
                for m in r.get("matches") or [{"path": {"value": "/"}}]
            }
            or {"/"}
        )
        backends = sorted({f"{b.get('name')}:{b.get('port')}" for r in rules for b in r.get("backendRefs") or []})
        routes.append(
            RouteInfo(
                name=meta.get("name", ""),
                hostnames=sorted(t.substitute(str(h), values) for h in spec.get("hostnames") or []),
                paths=paths,
                backends=backends,
                gateway=t.substitute(f"{parent.get('namespace', app.namespace)}/{parent.get('name', '')}", values),
                home_cell=t.substitute(str((meta.get("annotations") or {}).get("platform/home-cell", "")), values),
            )
        )
    services.sort(key=lambda s: s.name)
    routes.sort(key=lambda r: r.name)
    out = []
    for o in sorted(
        (o for o in objects if o.get("kind") in WORKLOAD_KINDS),
        key=lambda o: (o.get("kind"), (o.get("metadata") or {}).get("name", "")),
    ):
        meta = o.get("metadata") or {}
        pod_meta, pod = _pod_spec(o)
        labels = {k: str(v) for k, v in (pod_meta.get("labels") or {}).items() if k in PLACEMENT_LABELS}
        containers = list(pod.get("containers") or []) + list(pod.get("initContainers") or [])
        images = [str(c.get("image", "")) for c in containers]
        env = []
        for c in containers:
            for e in c.get("env") or []:
                if "value" in e:
                    env.append((str(e.get("name")), t.substitute(str(e.get("value")), values)))
                elif e.get("valueFrom"):
                    source = e["valueFrom"]
                    ref = (source.get("fieldRef") or {}).get("fieldPath") or next(iter(source), "")
                    env.append((str(e.get("name")), f"(from {ref})"))
        source = next((s for s in (match_source(i, sources) for i in images) if s), None)
        out.append(
            Section(
                app=app,
                kind=str(o.get("kind")),
                name=meta.get("name", ""),
                replicas=(o.get("spec") or {}).get("replicas"),
                images=images,
                labels=labels,
                env=sorted(env),  # by name: kustomize and the small build merge patched lists in other orders
                services=services,
                routes=routes,
                source=source,
            )
        )
    return out


def _cell(app: t.Application) -> t.Cell | None:
    home = next((c for c in app.cells if c.name == app.home_cell and c.enabled), None)
    return home or next((c for c in app.cells if c.enabled), app.cells[0] if app.cells else None)


# ----------------------------------------------------------------------------------------------------------------
# The pages


def _code(text: Any) -> str:
    return f"`{str(text).replace('`', '')}`"


def _cellt(text: str) -> str:
    return t._cell(text)


def _intro(what: str) -> list[str]:
    return [
        f"Generated by the documentation agent of [onek8s/aks-ai-agents]({_GITHUB}/onek8s/aks-ai-agents) "
        f"(`aks-agent docs lab`) from Git alone: {what}. Do not edit by hand - aks-terraform's Lab examples "
        "workflow regenerates it."
    ]


def _status(status: str) -> str:
    """``**allowed**: the application's rule, ...`` - the traffic flow documentation's text without its first word."""
    return f"**{status}** - {t.STATUS_TEXT[status].split(' - ', 1)[-1]}"


_GITHUB = "https://github.com"


def _blob(repo: str, path: str, tree: bool = False) -> str:
    return f"{_GITHUB}/{repo}/{'tree' if tree else 'blob'}/main/{path}"


def pools_of(settings: CellSettings, zone: str) -> list[dict[str, str]]:
    """The classic node pools aks-terraform gives a zone in a cell (``zone_node_pools``)."""
    if settings.pools is None or zone not in settings.zones:
        return []
    p, base = settings.pools, zone.replace("-", "")
    out = []
    for suffix, arch, spot in (("", "amd64", False), ("spot", "amd64", True), ("arm", "arm64", False)):
        out.append(_pool(base + suffix, arch, spot, p))
    out.append(_pool(base + "armspot", "arm64", True, p))
    return out


def _pool(name: str, arch: str, spot: bool, p: dict[str, str]) -> dict[str, str]:
    size = p.get("amd64_vm_size" if arch == "amd64" else "arm64_vm_size", "")
    return {
        "name": name,
        "arch": arch,
        "capacity": "spot" if spot else "on-demand",
        "vm_size": size,
        "nodes": f"0-{p.get('spot_max_count' if spot else 'max_count', '?')}",
        "os_disk": p.get("os_disk_type", ""),
        "max_pods": p.get("max_pods", ""),
    }


def _matching_pools(section: Section, pools: list[dict[str, str]]) -> list[str]:
    arch = section.labels.get("platform/arch")
    spot = section.labels.get("platform/capacity") == "spot"
    chosen = [p for p in pools if not arch or p["arch"] == arch]
    if spot:
        chosen.sort(key=lambda p: p["capacity"] != "spot")
    else:
        chosen = [p for p in chosen if p["capacity"] != "spot"]
    return [p["name"] for p in chosen]


@dataclass
class Repos:
    """The GitHub repositories of the local clones, for links."""

    landing_zone: str = "onek8s/azure-landing-zone"
    fleet: str = "onek8s/aks-fluxcd-platform"
    iac: str = "onek8s/aks-terraform"


@dataclass
class Lab:
    spoke: str
    apps: list[t.Application]
    zones: dict[str, list[t.Application]]
    cells: dict[str, CellSettings]
    sections: dict[str, list[Section]]  # namespace -> its sections
    allow_lists: dict[str, dict[str, list[str]]]
    platform_rules: list[str]
    listeners: list[dict[str, Any]]
    findings: list[t.Finding]
    repos: Repos

    def zone_cells(self, zone: str) -> list[CellSettings]:
        names = {c.name for a in self.zones.get(zone, []) for c in a.cells}
        found = [c for n, c in self.cells.items() if zone in c.zones or n in names]
        return sorted(found, key=lambda c: (not c.enabled, c.name))

    def domain(self, zone: str) -> str:
        for c in self.zone_cells(zone):
            if c.zone_domains.get(zone):
                return c.zone_domains[zone]
        return next((a.zone_domain for a in self.zones.get(zone, []) if a.zone_domain), "")


def _cell_line(c: CellSettings) -> str:
    where = c.location or "-"
    if c.availability_zones:
        where += f", availability zone {', '.join(c.availability_zones)}"
    return f"{_code(c.name)} ({where})" + ("" if c.enabled else " - defined, not built")


def index_page(lab: Lab) -> str:
    out = [
        "# Lab examples",
        "",
        "The test applications the lab runs, one page per isolation zone and one section per application. They",
        "describe what Git deploys to the lab, not what a cluster runs at a given moment.",
        "",
        *_intro(
            "aks-terraform's cells, the landing zone's firewall rule files, the fleet's zones and allow-lists, the "
            "applications' Flux configuration and their source repositories"
        ),
        "",
        "These pages are not part of the requirements or the design, and they are in neither PDF.",
        "",
        "## Cells",
        "",
        f"The spoke `{lab.spoke}`: the cells of exposure `{lab.spoke.split('-', 1)[-1]}`, from aks-terraform's",
        f"[`envs/`]({_blob(lab.repos.iac, 'envs', tree=True)}).",
        "",
        "| Cell | Location | Availability zones | Gateway implementation | Isolation zones | State |",
        "|---|---|---|---|---|---|",
    ]
    for c in sorted(lab.cells.values(), key=lambda c: (not c.enabled, c.name)):
        out.append(
            f"| [{_code(c.name)}]({_blob(lab.repos.iac, f'envs/{c.stem}.tfvars')}) | {c.location or '-'} | "
            f"{', '.join(c.availability_zones) or '-'} | {c.gateway_implementation or 'none'} | "
            f"{', '.join(_code(z) for z in c.zones) or '-'} | {'built' if c.enabled else 'defined, not built'} |"
        )
    out += ["", "## Zones", "", "| Zone | Country | Cells | Applications |", "|---|---|---|---|"]
    for zone in sorted(lab.zones):
        cells = ", ".join(_code(c.name) for c in lab.zone_cells(zone) if c.enabled) or "-"
        apps = ", ".join(
            f"[{s.heading}]({zone}.md#{anchor(s.heading)})"
            for a in lab.zones[zone]
            for s in lab.sections.get(a.namespace, [])
        )
        out.append(f"| [{zone_title(zone)}]({zone}.md) | {country(zone)} | {cells} | {apps or '-'} |")
    out += ["", "## Findings", ""]
    if lab.findings:
        out.append("What the traffic flow documentation found in the rule files and the configuration:")
        out.append("")
        for f in lab.findings:
            out.append(f"- [{f.namespace}]({f.zone or 'unknown'}.md) `{f.fqdn}` - **{f.status}**: {_cellt(f.detail)}")
    else:
        out.append("None: every rule is named in its application's configuration, every named destination has one.")
    return "\n".join(out) + "\n"


def zone_page(lab: Lab, zone: str) -> str:
    apps = lab.zones.get(zone, [])
    cells = lab.zone_cells(zone)
    domain = lab.domain(zone)
    allow_list = lab.allow_lists.get(zone, {})
    names = list(allow_list.get("names", [])) + list(allow_list.get("patterns", []))
    sections = [s for a in apps for s in lab.sections.get(a.namespace, [])]
    published = sorted({h for s in sections for r in s.routes for h in r.hostnames})
    gateway = next((r.gateway for s in sections for r in s.routes), f"{zone}-gateway/{zone}")
    implementations = sorted({c.gateway_implementation for c in cells if c.enabled and c.gateway_implementation})
    listeners = [
        f"{listener.get('protocol', '')} {listener.get('port', '')} "
        f"{_code(t.substitute(str(listener.get('hostname', '*')), {'zone_domain': domain, 'zone': zone}))}"
        for listener in lab.listeners
    ]
    out = [
        f"# {zone_title(zone)}",
        "",
        *_intro(f"what Git deploys into the isolation zone `{zone}` of the spoke `{lab.spoke}`"),
        "",
        "Back to the [lab examples](index.md).",
        "",
        "| | |",
        "|---|---|",
        f"| Isolation zone | {_code(zone)} ({country(zone)}) |",
        f"| Cells | {'<br/>'.join(_cell_line(c) for c in cells) or '-'} |",
        f"| Domain | {_code(domain) if domain else '-'} |",
        f"| Gateway | {_code(gateway)}"
        + (f" ({', '.join(implementations)})" if implementations else "")
        + (f", listener {', '.join(listeners)}" if listeners else "")
        + " |",
        f"| Published names | {'<br/>'.join(_code(h) for h in published) or '-'} |",
        f"| Zone allow-list | {', '.join(_code(n) for n in names) or '-'} "
        f"([fleet]({_blob(lab.repos.fleet, t.ALLOW_LISTS)})) |",
        f"| Applications | {', '.join(f'[{s.heading}](#{anchor(s.heading)})' for s in sections) or '-'} |",
        "",
        "## Placement",
        "",
    ]
    pool_rows = [(c, p) for c in cells if c.enabled for p in pools_of(c, zone)]
    if pool_rows:
        out += [
            "The zone's nodes are classic node pools of aks-terraform (`zone_node_pools`), each scaled by the cluster",
            "autoscaler from zero, labelled and tainted `platform/zone`; an application states its architecture and",
            "capacity with pod labels only.",
            "",
            "| Cell | Pool | Architecture | Capacity | VM size | Nodes | OS disk | Pods per node |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c, p in pool_rows:
            out.append(
                f"| {_code(c.name)} | {_code(p['name'])} | {p['arch']} | {p['capacity']} | {p['vm_size'] or '-'} | "
                f"{p['nodes']} | {p['os_disk'] or '-'} | {p['max_pods'] or '-'} |"
            )
    else:
        out.append("No cell that is built gives this zone node pools.")
    out += [
        "",
        "## Traffic of the zone",
        "",
        "What the hub firewall opens for the zone: every destination below is open to every application of the zone.",
        "",
        "```mermaid",
        t.zone_picture(zone, apps, allow_list, lab.platform_rules).rstrip("\n"),
        "```",
    ]
    quiet = [a for a in apps if not lab.sections.get(a.namespace)]
    if quiet:
        out += ["", "Namespaces of the zone without a workload in Git:", ""]
        for a in quiet:
            note = "; ".join(a.notes) or "no workload in its configuration"
            out.append(f"- `{a.namespace}`: {_cellt(note)}")
    for s in sections:
        out += ["", *application_section(lab, zone, s)]
    out += ["", _legend()]
    return "\n".join(out) + "\n"


def _legend() -> str:
    return "\n".join(
        [
            "## Legend",
            "",
            "The status of a destination, from the traffic flow documentation:",
            "",
            *(f"- {_status(status)}" for status in t.STATUS_TEXT),
        ]
    )


def application_section(lab: Lab, zone: str, s: Section) -> list[str]:
    app = s.app
    cell = _cell(app)
    pools = pools_of(lab.cells[cell.name], zone) if cell and cell.name in lab.cells else []
    rule_file = f"{t.APPLICATION_FILES}/{lab.spoke}/{app.namespace}.yaml"
    traffic_page = f"{t.DOCS_DIR}/{lab.spoke}/{app.namespace}.md"
    images = []
    for image in s.images:
        repo, tag, digest = image_parts(image)
        text = (
            _code(repo) + (f", version {_code(tag)}" if tag else "") + (f", digest {_code(digest)}" if digest else "")
        )
        images.append(text)
    labels = ", ".join(f"{_code(f'{k}: {v}')}" for k, v in sorted(s.labels.items())) or "none"
    matching = _matching_pools(s, pools)
    placement = labels + (f" - the zone's pools {', '.join(_code(p) for p in matching)}" if matching else "")
    rows = [
        f"| Configuration | [{app.slug} / {app.source_path}]({app.source_url}/tree/main/{app.source_path}) |"
        if app.source_url
        else "| Configuration | - |",
        f"| Team, contact | {app.team or '-'}, {app.contact or '-'} |",
        f"| {s.kind} | {_code(s.name)}, replicas {s.replicas if s.replicas is not None else '-'} |",
        f"| Image | {'<br/>'.join(images) or '-'} |",
    ]
    if s.source:
        version = f", version {_code(s.source.version)}" if s.source.version else ""
        rows.append(f"| Source | [{s.source.slug}]({_GITHUB}/{s.source.slug}){version} |")
    rows += [
        f"| Placement labels | {placement} |",
        f"| Firewall rule file | [{app.namespace}.yaml]({_blob(lab.repos.landing_zone, rule_file)}) |",
        f"| Traffic flows | [{app.namespace}]({_blob(lab.repos.landing_zone, traffic_page)}) |",
    ]
    out = [f"## {s.heading}", ""]
    if s.source and s.source.description:
        out += [f"From [{s.source.slug}]({_GITHUB}/{s.source.slug})'s README: {s.source.description}", ""]
    out += ["| | |", "|---|---|", *rows, ""]
    out += ["### Services and routes", ""]
    if s.services:
        out += ["| Service | Port | Target port | Protocol |", "|---|---|---|---|"]
        for svc in s.services:
            for p in svc.ports or [Port("", "-", "-", "")]:
                protocol = _code(p.app_protocol) if p.app_protocol else "-"
                out.append(f"| {_code(svc.name)} | {p.port} | {_code(p.target)} | {protocol} |")
        out.append("")
    if s.routes:
        out += ["| Route | Hostnames | Paths | Backends | Gateway | platform/home-cell |", "|---|---|---|---|---|---|"]
        for r in s.routes:
            out.append(
                f"| {_code(r.name)} | {'<br/>'.join(_code(h) for h in r.hostnames) or '-'} | "
                f"{', '.join(_code(p) for p in r.paths)} | {', '.join(_code(b) for b in r.backends) or '-'} | "
                f"{_code(r.gateway)} | {_code(r.home_cell) if r.home_cell else '-'} |"
            )
        out.append("")
    if not s.services and not s.routes:
        out += ["No Service and no route: nothing reaches it from outside its pods.", ""]
    if s.env:
        out += ["### Environment", "", "| Variable | Value |", "|---|---|"]
        out += [f"| {_code(name)} | {_env_value(value)} |" for name, value in s.env]
        out.append("")
    if s.source and s.source.endpoints:
        out += [f"### Endpoints of {s.name}", "", "| Path | What |", "|---|---|"]
        out += [f"| {_code(path)} | {_cellt(what)} |" for path, what in s.source.endpoints]
        out.append("")
    out += ["### Egress", ""]
    if app.destinations:
        out += [
            "| Destination | Port | Status | Named in the configuration | Reason of the rule |",
            "|---|---|---|---|---|",
        ]
        for d in app.destinations:
            out.append(
                f"| {_code(d.fqdn)} | {d.port}/{d.protocol} | {_status(d.status)} | "
                f"{'<br/>'.join(_cellt(w) for w in d.declared_in) or '-'} | {_cellt(d.reason) or '-'} |"
            )
    else:
        out.append("Nothing leaves the cluster: no rule, and the configuration names no destination.")
    findings = t.findings_of(app)
    if findings:
        out += ["", "Findings:", ""]
        out += [f"- `{f.fqdn}` - **{f.status}**: {_cellt(f.detail)}" for f in findings]
    if app.notes:
        out += ["", "Notes:", ""] + [f"- {_cellt(n)}" for n in app.notes]
    out += ["", "```mermaid", t.application_picture(app).rstrip("\n"), "```"]
    return out


def _env_value(value: str) -> str:
    """A value; ``name=url;name=url`` (aks-hello's PEERS) one pair a line."""
    parts = [p for p in value.split(";") if p]
    if len(parts) > 1 and all("=" in p for p in parts):
        return "<br/>".join(_code(p) for p in parts)
    return _code(value) if value else "-"


# ----------------------------------------------------------------------------------------------------------------
# All of it


@dataclass
class Result:
    files: dict[str, str]  # file name under examples/lab -> content
    findings: list[t.Finding]
    lab: Lab


def build(sources: t.Sources, app_sources: dict[str, Path] | None = None, repos: Repos | None = None) -> Lab:
    apps = t.collect(sources)
    cells = t.read_cells(sources.iac, sources.env)
    settings = {
        n: c for n, c in read_cell_settings(sources.iac, sources.env, cells).items() if c.exposure == sources.exposure
    }
    for c in settings.values():
        c.zone_domains = read_fleet_zones(sources.fleet, c.stem)
    srcs = {t.repo_slug(slug): read_app_source(slug, path) for slug, path in (app_sources or {}).items()}
    zones: dict[str, list[t.Application]] = {}
    for c in settings.values():
        if c.enabled:
            for z in c.zones:
                zones.setdefault(z, [])
    for a in apps:
        zones.setdefault(a.zone or "unknown", []).append(a)
    findings = [f for a in apps for f in t.findings_of(a)]
    return Lab(
        spoke=sources.spoke,
        apps=apps,
        zones=zones,
        cells=settings,
        sections={a.namespace: sections_of(a, srcs) for a in apps},
        allow_lists=t.read_allow_lists(sources.fleet),
        platform_rules=t.read_platform_rules(sources.landing_zone, sources.exposure, sources.env)["observability"],
        listeners=read_gateway_listeners(sources.fleet),
        findings=findings,
        repos=repos or Repos(),
    )


def generate(sources: t.Sources, app_sources: dict[str, Path] | None = None, repos: Repos | None = None) -> Result:
    lab = build(sources, app_sources, repos)
    files = {"index.md": index_page(lab)}
    for zone in sorted(lab.zones):
        files[f"{zone}.md"] = zone_page(lab, zone)
    return Result(files, lab.findings, lab)


def write(result: Result, out_dir: Path) -> tuple[list[str], list[str]]:
    """Writes the pages; removes every other ``*.md`` of the directory (it owns them). Returns (changed, removed)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for rel, text in sorted(result.files.items()):
        path = out_dir / rel
        if path.is_file() and path.read_text() == text:
            continue
        path.write_text(text)
        changed.append(rel)
    removed = []
    for stale in sorted(out_dir.glob("*.md")):
        if stale.name not in result.files:
            stale.unlink()
            removed.append(stale.name)
    return changed, removed


def diff(result: Result, out_dir: Path) -> list[str]:
    """The pages that differ from what ``out_dir`` holds, and the pages there that would be removed."""
    stale = [rel for rel, text in sorted(result.files.items()) if _read(out_dir / rel) != text]
    if out_dir.is_dir():
        stale += [p.name for p in sorted(out_dir.glob("*.md")) if p.name not in result.files]
    return stale


def _read(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None
