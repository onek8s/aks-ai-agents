from pathlib import Path

from aks_agents.cli import main, validate
from aks_agents.definition import AgentDefinition, ToolGrant, load_all
from aks_agents.settings import PlatformConfig
from aks_agents.tools.base import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
DEFS = load_all(ROOT / "agents")
PLATFORM = PlatformConfig.load(ROOT / "config" / "platform.yaml")


def writes(name):
    return {g.name for g in DEFS[name].tools if REGISTRY[g.name].writes}


def test_all_eleven_agents_valid():
    assert set(DEFS) == {
        "documentation-agent",
        "egress-agent",
        "operations-agent",
        "platform-agent",
        "upgrade-agent",
        "release-agent",
        "capacity-agent",
        "security-agent",
        "onboarding-agent",
        "rightsizing-agent",
        "reviewer-agent",
    }
    assert validate(DEFS, PLATFORM) == []


def test_roles_follow_the_design():
    # Operations agent: issues and runbooks, never pull requests.
    assert "create_pull_request" not in writes("operations-agent")
    assert set(DEFS["operations-agent"].grant("dispatch_runbook").options["runbooks"]) == {
        "halt-release",
        "drain-cell",
        "rollback-cell",
        "suspend-kustomization",
    }
    # Reviewer: reviews only.
    assert writes("reviewer-agent") == {"publish_review"}
    # Right-sizing agent writes only the VPA and resources files of the stateless overlays – never pods.
    assert DEFS["rightsizing-agent"].grant("repo_write").options == {
        "paths": {"fleet": ["apps/*/stateless/*/vpa.yaml", "apps/*/stateless/*/resources.yaml"]}
    }
    assert DEFS["rightsizing-agent"].grant("create_pull_request").options == {"repositories": ["fleet"]}
    # Upgrade agent writes only the version files.
    assert DEFS["upgrade-agent"].grant("repo_write").options == {
        "paths": {"iac": ["environments/*/versions.auto.tfvars"]}
    }
    # Nobody writes the guardrails or runbooks repositories, and nobody but the reviewer reviews.
    for d in DEFS.values():
        for g in d.tools:
            assert not {"guardrails", "runbooks"} & set(g.options.get("paths", {}) or {})


def test_validation_catches_violations():
    bad = AgentDefinition(
        "x",
        "",
        "instr",
        [
            ToolGrant("repo_write", {"paths": {"guardrails": ["*"]}}),
            ToolGrant("publish_review", {"repositories": ["iac"]}),
            ToolGrant("create_pull_request", {"repositories": ["iac"]}),
        ],
    )
    problems = validate({"x": bad}, PLATFORM)
    assert any("humans only" in p for p in problems)
    assert any("reserved" in p for p in problems)


def test_cli_validate(monkeypatch, capsys):
    monkeypatch.setenv("AKS_AGENT_ROOT", str(ROOT))
    assert main(["validate"]) == 0
    assert main(["list"]) == 0
    assert "operations-agent" in capsys.readouterr().out
