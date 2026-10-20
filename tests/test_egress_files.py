"""The applications' egress files: an agent adds destinations and never rewrites or removes an existing one."""

import subprocess

import pytest
import yaml
from test_repo_tools import FakePlatform

from aks_agents.audit import RunRecorder
from aks_agents.definition import AgentDefinition, ToolGrant
from aks_agents.settings import PlatformConfig, Settings
from aks_agents.tools.base import REGISTRY, ToolContext, ToolError, tools_for
from aks_agents.tools.firewall import add_egress_entries, keep_egress_entries

PATH = "stacks/platform-extensions/firewall-applications/dev-int/int-se-example.yaml"
EXISTING = """zone: int-se
contact: https://github.com/onek8s/aks-fluxcd-example/issues
egress:
  - fqdn: api.open-meteo.com
    port: 443
    protocol: https
    reason: "aks-hello fetches a weather forecast"
    evidence: |
      91 denied connections
      from pods aks-hello
"""
# What the model wrote in azure-landing-zone#72: the existing entry replaced by the new one.
REPLACED = """zone: int-se
contact: https://github.com/onek8s/aks-fluxcd-example/issues
egress:
  - fqdn: stats.grafana.org
    port: 443
    protocol: https
    reason: "metrics"
    evidence: "5 denied connections"
"""
NEW = {"fqdn": "stats.grafana.org", "port": 443, "protocol": "https", "reason": "metrics", "evidence": "5 denied"}
INT_FI_PATH = "stacks/platform-extensions/firewall-applications/dev-int/int-fi-example.yaml"
# int-fi-example.yaml on main when azure-landing-zone#75 was opened ...
INT_FI = """zone: int-fi
contact: https://github.com/onek8s/aks-fluxcd-example/issues
egress:
  - fqdn: api.open-meteo.com
    port: 443
    protocol: https
    reason: aks-hello 0.4.1 fetches a Helsinki weather forecast from the internet (api.open-meteo.com) as described in apps/firewall-egress-int-fi/kustomization.yaml
    evidence: |
      8 denied flows from pod aks-hello-895fcf8f8-jzfg6 in namespace int-fi-example on cluster aks-dev-int-stateless-az2, first seen 2026-10-16T04:43:17Z, last seen 2026-10-16T04:49:17Z
      The destination is named in apps/firewall-egress-int-fi/kustomization.yaml, line 6-10
      The flow was denied by the hub firewall, which saw the node address (viaNode: true).
      The application configuration names this destination.
  - fqdn: stats.grafana.org
    port: 443
    protocol: https
    reason: The application aks-hello in int-fi-example needs to send metrics to the public Grafana cloud service for monitoring and visualization.
    evidence: |
      5 denied flows from pod aks-hello-895fcf8f8-jzfg6 in namespace int-fi-example on cluster aks-dev-int-stateless-az2, first seen 2026-10-16T08:39:12Z, last seen 2026-10-16T08:39:39Z
      The flow was denied by the hub firewall, which saw the node address (viaNode: true).
      The application configuration does not explicitly name this destination, but it is a plausible public API for metrics.
"""
# ... and as #75 (titled "Egress: int-se-example -> stats.grafana.org") changed it: stats.grafana.org became api.ipify.org.
PR75_INT_FI = (
    INT_FI.split("  - fqdn: stats.grafana.org")[0]
    + """  - fqdn: api.ipify.org
    port: 443
    protocol: https
    reason: The application aks-hello in int-fi-example needs to query its public IP address at startup.
    evidence: |
      6 denied flows from pod aks-hello-77db47cfcd-bbfv9 in namespace int-fi-example on cluster aks-dev-int-stateless-az2, first seen 2026-10-16T09:12:57Z, last seen 2026-10-16T09:13:11Z
      The flow was denied by the hub firewall, which saw the node address (viaNode: true).
      The application configuration names this destination.
"""
)


def test_adding_keeps_existing_entries_verbatim_and_sorted():
    text, added, present = add_egress_entries(PATH, EXISTING, "int-se", "c", [NEW])
    assert added == ["stats.grafana.org"] and present == []
    assert text.startswith(EXISTING)  # every existing line untouched
    data = yaml.safe_load(text)
    assert [e["fqdn"] for e in data["egress"]] == ["api.open-meteo.com", "stats.grafana.org"]
    assert data["egress"][1] == NEW

    first = {**NEW, "fqdn": "a.example.org"}
    text, added, _ = add_egress_entries(PATH, text, "int-se", "c", [first])
    assert [e["fqdn"] for e in yaml.safe_load(text)["egress"]] == [
        "a.example.org",
        "api.open-meteo.com",
        "stats.grafana.org",
    ]
    assert EXISTING.split("egress:\n")[1] in text


def test_a_destination_already_there_is_not_added_again():
    text, added, present = add_egress_entries(PATH, EXISTING, "int-se", "c", [{**NEW, "fqdn": "api.open-meteo.com"}])
    assert text == EXISTING and added == [] and present == ["api.open-meteo.com"]


def test_a_new_file_and_an_empty_list():
    text, added, _ = add_egress_entries(PATH, None, "int-se", "https://x/issues", [NEW])
    assert yaml.safe_load(text) == {"zone": "int-se", "contact": "https://x/issues", "egress": [NEW]}
    text, _, _ = add_egress_entries(PATH, "zone: int-se\ncontact: c\negress: []\n", "int-se", "c", [NEW])
    assert yaml.safe_load(text)["egress"] == [NEW]


def test_wrong_zone_is_refused():
    with pytest.raises(ToolError, match="zone"):
        add_egress_entries(PATH, EXISTING, "int-fi", "c", [NEW])


def test_replacing_changing_or_deleting_an_entry_is_refused():
    with pytest.raises(ToolError, match="removes the existing entry api.open-meteo.com"):
        keep_egress_entries(PATH, EXISTING, REPLACED)
    with pytest.raises(ToolError, match="rewrites the existing entry"):
        keep_egress_entries(PATH, EXISTING, EXISTING.replace("aks-hello fetches", "fetches"))
    with pytest.raises(ToolError, match="delete"):
        keep_egress_entries(PATH, EXISTING, None)
    with pytest.raises(ToolError, match="zone may not change"):
        keep_egress_entries(PATH, EXISTING, EXISTING.replace("zone: int-se", "zone: int-fi"))
    keep_egress_entries(PATH, None, REPLACED)  # a new file
    keep_egress_entries(PATH, EXISTING, add_egress_entries(PATH, EXISTING, "int-se", "c", [NEW])[0])


def test_the_change_of_pull_request_75_is_refused():
    with pytest.raises(ToolError, match="removes the existing entry stats.grafana.org"):
        keep_egress_entries(INT_FI_PATH, INT_FI, PR75_INT_FI)
    text, added, _ = add_egress_entries(INT_FI_PATH, INT_FI, "int-fi", "c", [{**NEW, "fqdn": "api.ipify.org"}])
    assert added == ["api.ipify.org"] and INT_FI.split("egress:\n")[1] in text  # both entries verbatim
    assert [e["fqdn"] for e in yaml.safe_load(text)["egress"]] == [
        "api.ipify.org",
        "api.open-meteo.com",
        "stats.grafana.org",
    ]
    keep_egress_entries(INT_FI_PATH, INT_FI, text)


@pytest.fixture
def ctx(tmp_path):
    origin = tmp_path / "origin" / "lz"
    (origin / PATH).parent.mkdir(parents=True)
    (origin / PATH).write_text(EXISTING)
    (origin / INT_FI_PATH).write_text(INT_FI)

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=origin, check=True)

    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    definition = AgentDefinition(
        name="egress-agent",
        description="",
        instructions="",
        tools=[
            ToolGrant(
                "repo_write", {"paths": {"firewall": ["stacks/platform-extensions/firewall-applications/*/*.yaml"]}}
            ),
            ToolGrant("firewall_egress_add"),
            ToolGrant("create_pull_request", {"repositories": ["firewall"]}),
        ],
    )
    tools_for(definition.tools)
    c = ToolContext(
        agent=definition,
        settings=Settings(environment="dev", root=tmp_path, workspace=tmp_path / "ws", runs_dir=tmp_path / "runs"),
        platform=PlatformConfig(
            {"github": {"repositories": {"firewall": "o/lz"}, "cloneUrl": str(tmp_path / "origin") + "/{name}"}}
        ),
        recorder=RunRecorder(tmp_path / "runs", "egress-agent-x-abc123"),
        run_id="egress-agent-x-abc123",
    )
    c.state["platform"] = FakePlatform(tmp_path / "origin")
    return c


PR = dict(
    repo="firewall",
    title="Egress: int-se-example -> stats.grafana.org",
    intent="denied flows",
    evidence="5 denied",
    design_section="D61, section 5.1 egress",
    rollback="Revert the pull request.",
)


def test_the_tool_adds_and_the_pull_request_carries_both_entries(ctx):
    args = {"repo": "firewall", "spoke": "dev-int", "namespace": "int-se-example", "zone": "int-se", "contact": "c"}
    out = REGISTRY["firewall_egress_add"].handler(ctx, {**args, "entries": [{**NEW, "fqdn": "Stats.Grafana.org."}]})
    assert out == {"path": PATH, "added": ["stats.grafana.org"], "alreadyThere": []}
    REGISTRY["create_pull_request"].handler(ctx, PR)
    commit = next(c for c in ctx.state["platform"].calls if c[0] == "commit")
    fqdns = [e["fqdn"] for e in yaml.safe_load(commit[4][PATH])["egress"]]
    assert fqdns == ["api.open-meteo.com", "stats.grafana.org"]

    with pytest.raises(ToolError, match="not a destination name"):
        REGISTRY["firewall_egress_add"].run(ctx, {**args, "entries": [{**NEW, "fqdn": "1.2.3.4"}]})
    with pytest.raises(ToolError, match="application namespace"):
        REGISTRY["firewall_egress_add"].run(ctx, {**args, "namespace": "platform-x", "entries": [NEW]})
    with pytest.raises(ToolError, match="not a valid name"):
        REGISTRY["firewall_egress_add"].run(ctx, {**args, "spoke": "..", "entries": [NEW]})


def test_a_pull_request_that_replaces_an_entry_is_refused(ctx):
    REGISTRY["repo_write"].handler(ctx, {"repo": "firewall", "path": PATH, "content": REPLACED})
    with pytest.raises(ToolError, match="removes the existing entry api.open-meteo.com"):
        REGISTRY["create_pull_request"].handler(ctx, PR)
    assert ctx.state["platform"].calls == []


def test_one_pull_request_per_application_namespace(ctx):
    """azure-landing-zone#75: the model added entries for two namespaces and opened one pull request, titled for one."""
    add = REGISTRY["firewall_egress_add"].handler
    add(
        ctx,
        {
            "repo": "firewall",
            "spoke": "dev-int",
            "namespace": "int-se-example",
            "zone": "int-se",
            "contact": "c",
            "entries": [NEW],
        },
    )
    add(
        ctx,
        {
            "repo": "firewall",
            "spoke": "dev-int",
            "namespace": "int-fi-example",
            "zone": "int-fi",
            "contact": "c",
            "entries": [{**NEW, "fqdn": "api.ipify.org"}],
        },
    )
    create = REGISTRY["create_pull_request"].handler
    with pytest.raises(
        ToolError, match=f"one pull request per application namespace: this change has {INT_FI_PATH}, {PATH}"
    ):
        create(ctx, PR)
    with pytest.raises(ToolError, match="not changed in the workspace: other.yaml"):
        create(ctx, {**PR, "paths": ["other.yaml"]})
    assert ctx.state["platform"].calls == []

    out = create(ctx, {**PR, "paths": [PATH]})
    assert out["files"] == [PATH]
    commits = [c for c in ctx.state["platform"].calls if c[0] == "commit"]
    assert list(commits[0][4]) == [PATH]
    # int-fi-example's change stayed in the workspace for its own pull request.
    out = create(ctx, {**PR, "title": "Egress: int-fi-example -> api.ipify.org"})
    assert out["files"] == [INT_FI_PATH] and out["branch"] != commits[0][2]
    commits = [c for c in ctx.state["platform"].calls if c[0] == "commit"]
    assert list(commits[1][4]) == [INT_FI_PATH]
    assert [e["fqdn"] for e in yaml.safe_load(commits[1][4][INT_FI_PATH])["egress"]] == [
        "api.ipify.org",
        "api.open-meteo.com",
        "stats.grafana.org",
    ]
    with pytest.raises(ToolError, match="no changes"):
        create(ctx, PR)
