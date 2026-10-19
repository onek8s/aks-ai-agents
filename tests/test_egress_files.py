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


@pytest.fixture
def ctx(tmp_path):
    origin = tmp_path / "origin" / "lz"
    (origin / PATH).parent.mkdir(parents=True)
    (origin / PATH).write_text(EXISTING)

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
