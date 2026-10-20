"""Traffic flow documentation (design section 5.3): pages and pictures from the rule files, the applications'
configuration, the zone allow-lists and the cells - deterministic, Git only."""

import subprocess
from pathlib import Path

import pytest
from test_repo_tools import FakePlatform

from aks_agents import trafficdocs as t
from aks_agents.audit import RunRecorder
from aks_agents.definition import AgentDefinition
from aks_agents.settings import PlatformConfig, Settings
from aks_agents.tools.base import REGISTRY, ToolContext, ToolError, tools_for

FIXTURES = Path(__file__).parent / "fixtures" / "trafficdocs"
APPS = "onek8s/aks-fluxcd-example"


def sources(**kw) -> t.Sources:
    return t.Sources(
        landing_zone=FIXTURES / "landing-zone",
        fleet=FIXTURES / "fleet",
        iac=FIXTURES / "iac",
        spoke="dev-int",
        app_repos={APPS: FIXTURES / "apps"},
        **kw,
    )


def by_name(app: t.Application) -> dict[str, t.Destination]:
    return {d.fqdn: d for d in app.destinations}


def test_cells_from_the_tfvars():
    cells = t.read_cells(FIXTURES / "iac", "dev")
    assert cells["aks-dev-int-stateless-az3"].acns is True and cells["aks-dev-int-stateless-az3"].enabled
    assert cells["aks-dev-int-stateless-az4"].acns is False
    assert cells["aks-dev-int-stateful"].enabled is False  # not in the shared cells: never built
    assert cells["aks-dev-ext-stateless-az1"].exposure == "ext"
    assert (
        "Cilium FQDN" in cells["aks-dev-int-stateless-az3"].model
        and "Cilium" not in cells["aks-dev-int-stateless-az4"].model
    )


def test_managed_namespaces_per_cell_with_comments_and_quoted_keys():
    found = t.read_managed_namespaces(FIXTURES / "iac", "dev")
    az3 = found["int-se-example"]["aks-dev-int-stateless-az3"]
    assert az3["zone"] == "int-se" and az3["path"] == "firewall-egress" and az3["team"] == "example"
    assert az3["contact"] == "https://github.com/onek8s/aks-fluxcd-example/issues"
    assert az3["zone_domain"] == "int-se.dev.onek8s.lol" and az3["home_cell"] == "aks-dev-int-stateless-az3"
    assert found["int-se-example"]["aks-dev-int-stateful"]["path"] == "apps"
    assert found["ext-fi-example"]["aks-dev-ext-stateless-az1"]["zone"] == "ext-fi"


def test_the_small_build_follows_resources_components_images_and_patches():
    objects = t._build(FIXTURES / "apps" / "firewall-egress")
    kinds = sorted(o["kind"] for o in objects)
    assert kinds == ["Deployment", "HTTPRoute", "Service"]  # the VPA deleted by the $patch: delete
    dep = next(o for o in objects if o["kind"] == "Deployment")
    assert dep["metadata"]["namespace"] == "int-se-example"
    assert dep["spec"]["replicas"] == 3  # the component's replicas
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "acronek8sdev.azurecr.io/aks-hello:0.5.0"
    assert [e["name"] for e in container["env"]] == [
        "CLUSTER_NAME",
        "LICENSE_URL",
        "TOKEN_URL",
        "INTERNAL",
        "WEATHER_URL",
    ]


def test_describe_finds_workloads_routes_services_and_urls():
    workloads, routes, services = t.describe(t._build(FIXTURES / "apps" / "firewall-egress"), "int-se-example")
    assert services == ["aks-hello"]
    assert routes[0].hostnames == ["hello-${cluster_name}.${zone_domain}"]
    assert routes[0].gateway == "${zone}-gateway/${zone}" and routes[0].backends == ["aks-hello"]
    urls = dict(workloads[0].env_urls)
    assert urls["WEATHER_URL"].startswith("https://api.open-meteo.com") and "LICENSE_URL" in urls


def test_statuses_of_the_destinations():
    apps = {a.namespace: a for a in t.collect(sources())}
    app = apps["int-se-example"]
    d = by_name(app)
    assert d["api.open-meteo.com"].status == t.ALLOWED
    assert d["api.open-meteo.com"].declared_in == [
        "Deployment aks-hello, WEATHER_URL",
        "mentioned in firewall-egress/kustomization.yaml",
    ]
    assert d["api.open-meteo.com"].rule == "dev-int-applications/int-se-example-https-443"
    assert d["stats.grafana.org"].status == t.UNEXPLAINED
    assert d["license.example.org"].status == t.MISSING  # named by LICENSE_URL, no rule
    assert d["license.example.org"].declared_in == ["Deployment aks-hello, LICENSE_URL"]
    # The zone's allow-list: on the application's page only when its configuration names it (TOKEN_URL does)
    assert d["login.microsoftonline.com"].status == t.ZONE
    assert d["login.microsoftonline.com"].declared_in == ["Deployment aks-hello, TOKEN_URL"]
    assert "*.blob.example.net" not in d and "stats.grafana.org" in d  # the latter is the application's own rule
    assert "search.int-se-search.svc.cluster.local" not in d  # in-cluster names are not egress
    assert app.destinations[-1].status == t.ZONE  # the application's own destinations first
    # The home cell's configuration is documented, the stateful overlay noted
    assert app.source_path == "firewall-egress" and app.home_cell == "aks-dev-int-stateless-az3"
    assert [c.name for c in app.cells] == ["aks-dev-int-stateful", "aks-dev-int-stateless-az3"]
    assert any("aks-dev-int-stateful: apps" in n for n in app.notes)
    assert app.hostnames(app.cells[1]) == ["hello-aks-dev-int-stateless-az3.int-se.dev.onek8s.lol"]


def test_a_namespace_without_rules_and_a_rule_file_without_a_namespace():
    apps = {a.namespace: a for a in t.collect(sources())}
    quiet = apps["int-fi-quiet"]  # no rule file, no manifests at its path: nothing of its own, nothing named
    assert quiet.destinations == []
    assert any("No manifests found" in n for n in quiet.notes)
    gone = apps["int-se-gone"]  # a rule file whose namespace no cell manages any more
    assert by_name(gone)["old.example.org"].status == t.UNEXPLAINED
    assert any("may be gone" in n for n in gone.notes)
    assert "ext-fi-example" not in apps  # another exposure


def test_platform_rules_from_the_landing_zone_tfvars():
    rules = t.read_platform_rules(FIXTURES / "landing-zone", "int", "dev")
    assert rules == {
        "observability": ["*.grafana.net", "stats.grafana.org"],
        "zones": {"int-fi": ["login.microsoftonline.com"], "int-se": ["login.microsoftonline.com"]},
    }
    assert t.read_platform_rules(FIXTURES / "landing-zone", "ext", "dev") == {"observability": [], "zones": {}}


def test_a_platform_rule_named_by_the_configuration_shows_as_platform():
    app = t.Application(namespace="int-se-x", zone="int-se")
    app.workloads = [t.Workload("Deployment", "x", 1, [], [("STATS", "https://stats.grafana.org/report")])]
    t.classify(app, [], {}, {}, "dev-int-applications", platform_rules=["stats.grafana.org"])
    assert [(d.fqdn, d.status) for d in app.destinations] == [("stats.grafana.org", t.PLATFORM)]


def test_a_proposed_destination_points_at_its_pull_request():
    proposed = {"int-se-example": {("license.example.org", "https", "443"): "https://example/pull/9"}}
    app = next(a for a in t.collect(sources(proposed=proposed)) if a.namespace == "int-se-example")
    d = by_name(app)["license.example.org"]
    assert d.status == t.PROPOSED and d.pull_request == "https://example/pull/9"


def test_findings_name_the_unexplained_and_the_missing():
    result = t.generate(sources())
    found = {(f.namespace, f.fqdn): f.status for f in result.findings}
    assert found == {
        ("int-se-example", "stats.grafana.org"): t.UNEXPLAINED,
        ("int-se-example", "license.example.org"): t.MISSING,
        ("int-se-gone", "old.example.org"): t.UNEXPLAINED,
    }
    text = t.findings_text(result.findings)
    assert "LICENSE_URL names license.example.org" in text and "Still needed, or a rule to remove?" in text


def test_pages_and_pictures():
    result = t.generate(sources())
    assert set(result.files) == {
        "README.md",
        "dev-int/int-fi-quiet.md",
        "dev-int/int-se-example.md",
        "dev-int/int-se-gone.md",
        "dev-int/zone-int-fi.md",
        "dev-int/zone-int-se.md",
    }
    page = result.files["dev-int/int-se-example.md"]
    assert "```mermaid\nflowchart LR" in page
    assert "pod subnet + Cilium FQDN policies: the firewall sees the pod's own address" in page
    assert 'fw -->|"allowed"| d_api_open_meteo_com' in page
    assert 'fw -->|"allowed, not in the configuration"| d_stats_grafana_org' in page
    assert 'fw -.->|"denied: no rule"| d_license_example_org' in page
    assert "class d_license_example_org missing" in page
    assert "hello-aks-dev-int-stateless-az3.int-se.dev.onek8s.lol" in page
    assert "acronek8sdev.azurecr.io/aks-hello:0.5.0" in page and "Deployment aks-hello x3" in page
    assert "| `license.example.org` | 443/https | no rule" in page
    assert "zone's page](zone-int-se.md)" in page
    zone = result.files["dev-int/zone-int-se.md"]
    assert "asked by int-se-example" in zone and "license.example.org" in zone  # named, no rule yet
    assert "stats.grafana.org:443<br/>platform rule, every cell" in zone  # the exposure's observability rule
    assert "*.blob.example.net:443<br/>zone allow-list" in zone
    assert "differ: the firewall enforces the latter" in zone  # fleet list has *.blob.example.net, tfvars not
    assert "differ" not in result.files["dev-int/zone-int-fi.md"]
    assert "[`int-se-example`](int-se-example.md)" in zone and "| **no** |" in zone
    index = result.files["README.md"]
    assert "[`int-se`](dev-int/zone-int-se.md)" in index and "(dev-int/int-se-example.md)" in index
    # No timestamp anywhere: an unchanged world writes nothing
    assert t.generate(sources()).files == result.files


def test_labels_are_safe_for_mermaid():
    app = t.Application(namespace="int-se-x", zone="int-se", team='a "team" [x]|y')
    app.workloads = [t.Workload("Deployment", "x", 1, ['img:"1"'], [])]
    picture = t.application_picture(app)
    for line in picture.splitlines():
        if '["' in line:
            label = line.split('["', 1)[1].rsplit('"]', 1)[0]
            assert '"' not in label and "|" not in label and "[" not in label and "]" not in label


def test_write_diff_and_stale_pages(tmp_path):
    result = t.generate(sources())
    changed, removed = t.write(result, tmp_path, "dev-int")
    assert sorted(changed) == sorted(result.files) and removed == []
    assert t.write(result, tmp_path, "dev-int") == ([], [])  # idempotent
    assert t.diff(result, tmp_path, "dev-int") == []
    (tmp_path / "dev-int" / "int-se-old.md").write_text("stale")
    assert t.diff(result, tmp_path, "dev-int") == ["dev-int/int-se-old.md"]
    assert t.write(result, tmp_path, "dev-int") == ([], ["dev-int/int-se-old.md"])


def test_cli_writes_and_checks(tmp_path, capsys):
    from aks_agents.cli import main

    args = [
        "docs",
        "traffic",
        "--spoke",
        "dev-int",
        "--landing-zone",
        str(FIXTURES / "landing-zone"),
        "--fleet",
        str(FIXTURES / "fleet"),
        "--iac",
        str(FIXTURES / "iac"),
        "--app-repo",
        f"{APPS}={FIXTURES / 'apps'}",
        "--output",
        str(tmp_path),
    ]
    assert main(args + ["--check"]) == 1  # nothing there yet
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "3 applications of dev-int; 6 pages" in out and "changed: 6" in out
    assert main(args + ["--check"]) == 0
    assert (tmp_path / "dev-int" / "zone-int-se.md").is_file()


# ---- the agent's tool -------------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True)


@pytest.fixture
def ctx(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    repos = {
        "lz": FIXTURES / "landing-zone",
        "fleet": FIXTURES / "fleet",
        "iac": FIXTURES / "iac",
        "aks-fluxcd-example": FIXTURES / "apps",
    }
    for name, src in repos.items():
        target = origin / name
        subprocess.run(["cp", "-r", str(src), str(target)], check=True)
        _git(target, "init", "-q", "-b", "main")
        _git(target, "add", ".")
        _git(target, "commit", "-q", "-m", "init")
    definition = AgentDefinition.load(Path(__file__).resolve().parents[1] / "agents" / "documentation-agent.yaml")
    tools_for(definition.tools)
    c = ToolContext(
        agent=definition,
        settings=Settings(environment="dev", root=tmp_path, workspace=tmp_path / "ws", runs_dir=tmp_path / "runs"),
        platform=PlatformConfig(
            {
                "git": {
                    "repositories": {
                        "firewall": "o/lz",
                        "fleet": "o/fleet",
                        "iac": "o/iac",
                        "apps": f"https://github.com/{APPS}",
                    },
                },
                "documentation": {"repository": "firewall", "path": "docs/traffic-flows"},
            }
        ),
        recorder=RunRecorder(tmp_path / "runs", "documentation-agent-x-abc123"),
        run_id="documentation-agent-x-abc123",
    )
    c.state["platform"] = FakePlatform(origin)
    return c


def test_tool_generates_into_the_documentation_workspace(ctx):
    out = REGISTRY["traffic_flow_docs"].handler(ctx, {"spoke": "dev-int"})
    assert out["repository"] == "firewall" and out["directory"] == "docs/traffic-flows"
    assert "dev-int/int-se-example.md" in out["changed"] and out["unchanged"] == 0
    assert {f["fqdn"] for f in out["findings"]} == {"stats.grafana.org", "license.example.org", "old.example.org"}
    assert "pull request" in out["next"]
    page = (ctx.state["workspaces"]["firewall"] / "docs/traffic-flows/dev-int/int-se-example.md").read_text()
    assert "Deployment aks-hello x3" in page  # the application repository was cloned and read
    again = REGISTRY["traffic_flow_docs"].handler(ctx, {"spoke": "dev-int"})
    assert again["changed"] == [] and "finish" in again["next"]
    with pytest.raises(ToolError, match="spoke"):
        REGISTRY["traffic_flow_docs"].handler(ctx, {"spoke": "dev"})


def test_tool_needs_the_write_grant_on_the_pages(ctx):
    ctx.agent.tools = [g for g in ctx.agent.tools if g.name != "repo_write"]
    with pytest.raises(ToolError):
        REGISTRY["traffic_flow_docs"].handler(ctx, {"spoke": "dev-int"})
