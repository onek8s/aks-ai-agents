import datetime as dt
import json

from conftest import ROOT, resource

from aks_gate.cli import main
from aks_gate.gate import PASS, REJECT, WAIT, decide
from aks_gate.model import Action, PlanSet, Tier
from aks_gate.pr import PullRequest, parse_trailers
from aks_gate.windows import in_freeze, in_window, window_timezone

# Tuesday 10:00 in Helsinki: inside the prd window
TUESDAY = dt.datetime(2026, 10, 13, 7, 0, tzinfo=dt.UTC)
SATURDAY = dt.datetime(2026, 10, 17, 7, 0, tzinfo=dt.UTC)

BODY = """## Intent
Patch upgrade per the update timetable (#12).

## Evidence
Version pipeline run 4711.

## Design section
design/3.5-zero-downtime-cluster-upgrades.md#update-policy

## Rollback
Revert this pull request.
"""
MESSAGE = "Upgrade sl-az1 to 1.36.3\n\nAgent: upgrade-agent\nAgent-Run: run-123\n"


def pr(**overrides):
    data = {
        "repository": "org/iac",
        "number": 1,
        "author": "aks-upgrade-agent[bot]",
        "head_ref": "agent/upgrade-agent/prd-sl-az1-1.36.3",
        "head_sha": "abc",
        "body": BODY,
        "commits": [{"sha": "abc", "message": MESSAGE}],
        "reviews": [],
        "reviewer_agent_conclusion": "success",
    }
    data.update(overrides)
    return PullRequest.from_dict(data)


def patch_plan(env="prd"):
    name = f"aks-{env}-sl-az1"
    return PlanSet(
        repository="org/iac",
        resources=[
            resource(
                "Microsoft.ContainerService/managedClusters",
                name=name,
                env=env,
                before={"name": name, "kubernetes_version": "1.36.1"},
                after={"name": name, "kubernetes_version": "1.36.3"},
            )
        ],
    )


def test_trailers():
    assert parse_trailers(MESSAGE) == {"Agent": ["upgrade-agent"], "Agent-Run": ["run-123"]}
    assert parse_trailers("single line") == {}
    assert parse_trailers("subject\n\nbody text that is: not a trailer block\nmore") == {}


def test_windows_and_freezes():
    assert in_window("Mon-Thu 08:00-15:00 Europe/Helsinki", TUESDAY)
    assert not in_window("Mon-Thu 08:00-15:00 Europe/Helsinki", SATURDAY)
    assert in_window("Fri-Mon 00:00-23:59", SATURDAY)
    tz = window_timezone(["Mon-Thu 08:00-15:00 Europe/Helsinki"])
    assert in_freeze(["2026-12-18/2027-01-06"], dt.datetime(2027, 1, 6, 12, tzinfo=dt.UTC), tz)
    assert not in_freeze(["2026-12-18/2027-01-06"], TUESDAY, tz)


def test_dev_patch_from_agent_auto_merges(config):
    d = decide(pr(head_ref="agent/upgrade-agent/dev"), patch_plan("dev"), config, TUESDAY)
    assert (d.decision, d.tier, d.auto_merge) == (PASS, Tier.LOW, True)


def test_prd_starts_at_medium_and_waits_for_platform(config):
    d = decide(pr(), patch_plan(), config, TUESDAY)
    assert (d.decision, d.tier) == (WAIT, Tier.MEDIUM)
    assert any("platform" in w for w in d.waiting)
    approved = pr(reviews=[{"user": "alice", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]}])
    d = decide(approved, patch_plan(), config, TUESDAY)
    assert (d.decision, d.auto_merge) == (PASS, False)


def test_stale_bot_and_self_approvals_do_not_count(config):
    reviews = [
        {"user": "alice", "state": "APPROVED", "commit_id": "old", "teams": ["platform"]},
        {"user": "aks-reviewer-agent[bot]", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]},
    ]
    assert decide(pr(reviews=reviews), patch_plan(), config, TUESDAY).decision == WAIT


def test_high_needs_two_distinct_humans(config):
    plan = PlanSet(
        repository="org/iac",
        resources=[
            resource(
                "Microsoft.Network/networkSecurityGroups/securityRules",
                Action.CREATE,
                after={"access": "Deny"},
                env="dev",
            )
        ],
    )
    one = [{"user": "alice", "state": "APPROVED", "commit_id": "abc", "teams": ["platform", "security"]}]
    d = decide(pr(head_ref="agent/upgrade-agent/x", reviews=one), plan, config, TUESDAY)
    assert (d.tier, d.decision) == (Tier.HIGH, WAIT)
    two = one + [{"user": "bob", "state": "APPROVED", "commit_id": "abc", "teams": ["security"]}]
    assert decide(pr(head_ref="agent/upgrade-agent/x", reviews=two), plan, config, TUESDAY).decision == PASS


def test_outside_window_and_human_incident_fix(config):
    approved = [{"user": "alice", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]}]
    d = decide(pr(reviews=approved), patch_plan(), config, SATURDAY)
    assert d.decision == WAIT and any("window" in w for w in d.waiting)
    d = decide(pr(reviews=approved, incident_fix_by_human=True), patch_plan(), config, SATURDAY)
    assert d.decision == PASS


def test_rate_limit(config):
    approved = [{"user": "alice", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]}]
    d = decide(pr(reviews=approved, merges_today={"prd": 2}), patch_plan(), config, TUESDAY)
    assert d.decision == WAIT and any("merged 2" in w for w in d.waiting)


def test_missing_structure_and_trailers_reject(config):
    d = decide(pr(body="fix", commits=[{"sha": "abc", "message": "fix"}]), patch_plan("dev"), config, TUESDAY)
    assert d.decision == REJECT
    assert any("Intent" in v for v in d.violations) and any("Agent-Run" in v for v in d.violations)


def test_design_section_cites_the_design_or_the_requirements(config):
    requirements = BODY.replace(
        "design/3.5-zero-downtime-cluster-upgrades.md#update-policy", "requirements/0.2-requirements.md"
    )
    assert decide(pr(body=requirements), patch_plan("dev"), config, TUESDAY).decision != REJECT
    old = BODY.replace("design/3.5-zero-downtime-cluster-upgrades.md#update-policy", "docs/3.5-zero-downtime.md")
    d = decide(pr(body=old), patch_plan("dev"), config, TUESDAY)
    assert d.decision == REJECT and any("Design section" in v for v in d.violations)


def test_wrong_agent_trailer_and_branch(config):
    other = MESSAGE.replace("Agent: upgrade-agent", "Agent: capacity-agent")
    d = decide(pr(commits=[{"sha": "abc", "message": other}]), patch_plan("dev"), config, TUESDAY)
    assert d.decision == REJECT
    d = decide(pr(head_ref="feature/x"), patch_plan("dev"), config, TUESDAY)
    assert d.decision == REJECT and "must push to agent/upgrade-agent/*" in d.violations[0]


def test_kill_switch(config):
    config.limits.raw["agents"]["enabled"] = False
    d = decide(pr(head_ref="agent/upgrade-agent/dev"), patch_plan("dev"), config, TUESDAY)
    assert d.decision == REJECT


def test_human_pull_request_needs_no_structure(config):
    approved = [{"user": "bob", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]}]
    d = decide(pr(author="alice", head_ref="fix", body="", commits=[], reviews=approved), patch_plan(), config, TUESDAY)
    assert (d.agent, d.tier, d.decision) == (None, Tier.LOW, PASS)


def test_reviewer_flag_and_missing_review(config):
    d = decide(pr(head_ref="agent/upgrade-agent/dev", reviewer_agent_flagged=True), patch_plan("dev"), config, TUESDAY)
    assert d.tier == Tier.MEDIUM
    d = decide(
        pr(head_ref="agent/upgrade-agent/dev", reviewer_agent_conclusion=None), patch_plan("dev"), config, TUESDAY
    )
    assert d.decision == WAIT


def test_cli_end_to_end(tmp_path, capsys):
    plan = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "module.dev.azurerm_kubernetes_cluster.sl_az1",
                "type": "azurerm_kubernetes_cluster",
                "name": "sl_az1",
                "change": {
                    "actions": ["update"],
                    "before": {"name": "aks-dev-sl-az1", "kubernetes_version": "1.36.1"},
                    "after": {"name": "aks-dev-sl-az1", "kubernetes_version": "1.36.3"},
                },
            }
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    data = {
        "repository": "org/iac",
        "number": 1,
        "author": "aks-upgrade-agent[bot]",
        "head_ref": "agent/upgrade-agent/dev",
        "head_sha": "abc",
        "body": BODY,
        "commits": [{"sha": "abc", "message": MESSAGE}],
        "reviewer_agent_conclusion": "success",
    }
    (tmp_path / "pr.json").write_text(json.dumps(data))
    code = main(
        [
            "evaluate",
            "--guardrails",
            str(ROOT / "guardrails"),
            "--pr",
            str(tmp_path / "pr.json"),
            "--plan",
            f"dev:{tmp_path / 'plan.json'}",
            "--now",
            TUESDAY.isoformat(),
            "--summary",
            str(tmp_path / "summary.md"),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["tier"] == "low" and out["autoMerge"] is True
    assert "Merge gate: pass" in (tmp_path / "summary.md").read_text()
    assert main(["evaluate", "--guardrails", str(ROOT / "guardrails"), "--pr", str(tmp_path / "pr.json")]) == 3


def test_cli_discovers_artifacts_and_waits_without_them(tmp_path, capsys):
    pr_file = tmp_path / "pr.json"
    pr_file.write_text(
        json.dumps(
            {
                "repository": "org/iac",
                "number": 1,
                "author": "alice",
                "head_ref": "fix",
                "head_sha": "abc",
                "reviewer_agent_conclusion": "success",
                "reviews": [{"user": "bob", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]}],
            }
        )
    )
    common = ["evaluate", "--guardrails", str(ROOT / "guardrails"), "--pr", str(pr_file), "--now", TUESDAY.isoformat()]
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    assert main([*common, "--artifacts", str(artifacts)]) == 2
    (artifacts / "plan-dev").mkdir()
    (artifacts / "plan-dev" / "plan.json").write_text(json.dumps({"format_version": "1.2", "resource_changes": []}))
    capsys.readouterr()
    assert main([*common, "--artifacts", str(artifacts)]) == 0
