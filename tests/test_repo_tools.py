import subprocess

import pytest

from aks_agents.audit import RunRecorder
from aks_agents.definition import AgentDefinition, ToolGrant
from aks_agents.settings import PlatformConfig, Settings
from aks_agents.tools.base import REGISTRY, ToolContext, ToolError, tools_for
from aks_gate.pr import PullRequest, body_sections, check_structure, parse_trailers


class FakePlatform:
    """Records what the tools ask the Git platform to do."""

    name = "fake"
    max_body_length = 65_000
    default_branch = "main"

    def __init__(self, clone_root):
        self.clone_root = clone_root
        self.calls = []

    def clone_url(self, repo):
        return f"{self.clone_root}/{repo.split('/')[-1]}"

    def git_auth_header(self):
        return None

    def commit_files(self, repo, base_sha, branch, message, changes, existing):
        self.calls.append(("commit", repo, branch, message, changes, existing))
        return "c0ffee"

    def create_pull_request(self, repo, branch, title, body, draft):
        self.calls.append(("pull_request", repo, branch, title, body, draft))
        return {"number": 7, "url": "https://example/pull/7"}

    def create_issue(self, repo, title, body, labels):
        self.calls.append(("issue", repo, title, body, labels))
        return {"number": 3, "url": "https://example/issues/3"}

    def read_pull_request(self, repo, number):
        return {"author": "aks-platform-agent[bot]", "head_sha": "head123"}

    def dispatch_runbook(self, repo, runbook, inputs):
        self.calls.append(("runbook", repo, runbook, inputs))
        return {"dispatched": True}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ctx(tmp_path):
    origin = tmp_path / "origin" / "fleet"
    (origin / "apps" / "shop" / "stateless" / "prd").mkdir(parents=True)
    (origin / "apps" / "shop" / "stateless" / "prd" / "kustomization.yaml").write_text(
        "images:\n- name: shop\n  digest: sha256:aaa\n"
    )
    (origin / "infrastructure").mkdir()
    (origin / "infrastructure" / "base.yaml").write_text("kind: x\n")
    git(origin, "init", "-q", "-b", "main")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    platform = PlatformConfig(
        {
            "github": {
                "repositories": {"fleet": "o/fleet", "runbooks": "o/runbooks"},
                "cloneUrl": str(tmp_path / "origin") + "/{name}",
            },
        }
    )
    definition = AgentDefinition(
        name="release-agent",
        description="",
        instructions="",
        tools=[
            ToolGrant("repo_read", {"repositories": ["fleet"]}),
            ToolGrant("repo_write", {"paths": {"fleet": ["apps/*/stateless/*/kustomization.yaml"]}}),
            ToolGrant("create_pull_request", {"repositories": ["fleet"]}),
            ToolGrant("create_issue", {"repositories": ["fleet"]}),
            ToolGrant("dispatch_runbook", {"runbooks": ["drain-cell", "suspend-kustomization"]}),
        ],
    )
    settings = Settings(environment="prd", root=tmp_path, workspace=tmp_path / "ws", runs_dir=tmp_path / "runs")
    c = ToolContext(
        agent=definition,
        settings=settings,
        platform=platform,
        recorder=RunRecorder(tmp_path / "runs", "release-agent-x-abc123"),
        run_id="release-agent-x-abc123",
    )
    c.state["platform"] = FakePlatform(tmp_path / "origin")
    tools_for(definition.tools)
    return c


def run(ctx, name, **args):
    return REGISTRY[name].run(ctx, args)


PR_ARGS = dict(
    repo="fleet",
    title="Promote shop to prd",
    intent="Release #12 passed acc.",
    evidence="acc gates green",
    design_section="design/3.3-keeping-clusters-in-sync.md",
    rollback="Revert the digest.",
)


def test_read_write_and_pull_request(ctx):
    assert "apps/shop/stateless/prd/kustomization.yaml" in run(ctx, "repo_list", repo="fleet", glob="apps/*")
    assert "sha256:aaa" in run(ctx, "repo_read", repo="fleet", path="apps/shop/stateless/prd/kustomization.yaml")
    assert "sha256:aaa" in run(ctx, "repo_search", repo="fleet", pattern="digest")
    run(
        ctx,
        "repo_replace",
        repo="fleet",
        path="apps/shop/stateless/prd/kustomization.yaml",
        old="sha256:aaa",
        new="sha256:bbb",
    )
    assert "+  digest: sha256:bbb" in run(ctx, "repo_diff", repo="fleet")

    out = REGISTRY["create_pull_request"].handler(ctx, PR_ARGS)
    assert out["number"] == 7 and out["branch"].startswith("agent/release-agent/promote-shop-to-prd-")
    calls = ctx.state["platform"].calls
    commit = next(c for c in calls if c[0] == "commit")
    assert commit[4] == {"apps/shop/stateless/prd/kustomization.yaml": b"images:\n- name: shop\n  digest: sha256:bbb\n"}
    assert "apps/shop/stateless/prd/kustomization.yaml" in commit[5]  # existed before: an edit, not an add
    assert parse_trailers(commit[3]) == {"Agent": ["release-agent"], "Agent-Run": ["release-agent-x-abc123"]}

    # The body and the commit satisfy the merge gate's structure check.
    body = next(c for c in calls if c[0] == "pull_request")[4]
    assert {"intent", "evidence", "design section", "rollback"} <= set(body_sections(body))
    pr = PullRequest(repository="o/fleet", number=7, author="b", head_ref=out["branch"], head_sha="x", body=body)
    pr.commits = [type("C", (), {"sha": "c0ffee0000", "message": commit[3]})()]
    assert (
        check_structure(
            pr, "release-agent", ["Intent", "Evidence", "Design section", "Rollback"], ["Agent", "Agent-Run"]
        )
        == []
    )

    # The workspace is clean again for the next change.
    assert run(ctx, "repo_diff", repo="fleet") == "no changes"
    assert ctx.outputs[0]["url"].endswith("/pull/7")


def test_long_body_is_shortened_in_the_evidence_only():
    from aks_agents.tools.repo import pull_request_body

    body = pull_request_body({**PR_ARGS, "evidence": "x" * 10_000}, "release-agent", "run-1", max_length=4000)
    assert len(body) <= 4000 and "shortened" in body
    assert {"intent", "evidence", "design section", "rollback"} <= set(body_sections(body))
    assert "Revert the digest." in body


def test_write_outside_allowed_paths_is_denied(ctx):
    with pytest.raises(ToolError, match="may not write"):
        run(ctx, "repo_write", repo="fleet", path="infrastructure/base.yaml", content="x")
    with pytest.raises(ToolError, match="outside the repository"):
        run(ctx, "repo_read", repo="fleet", path="../../etc/passwd")
    with pytest.raises(ToolError, match="may not use"):
        run(ctx, "repo_read", repo="iac", path="x")


def test_pull_request_needs_changes_and_design_reference(ctx):
    with pytest.raises(ToolError, match="no changes"):
        REGISTRY["create_pull_request"].handler(ctx, PR_ARGS)
    run(ctx, "repo_write", repo="fleet", path="apps/shop/stateless/prd/kustomization.yaml", content="x\n")
    with pytest.raises(ToolError, match="design_section"):
        REGISTRY["create_pull_request"].handler(ctx, {**PR_ARGS, "design_section": "the docs"})
    with pytest.raises(ToolError, match="design_section"):
        REGISTRY["create_pull_request"].handler(
            ctx, {**PR_ARGS, "design_section": "docs/3.3-keeping-clusters-in-sync.md"}
        )


def test_design_citation_names_the_design_or_the_requirements():
    from aks_agents.tools.repo import DESIGN_CITATION

    for cited in (
        "design/3.5-zero-downtime-cluster-upgrades.md#update-policy",
        "requirements/0.2-requirements.md#r13",
        "requirements/0.3-acceptance-criteria.md",
        "section 3.5",
    ):
        assert DESIGN_CITATION.search(cited), cited
    for cited in ("docs/3.5-zero-downtime-cluster-upgrades.md", "the docs", "other/3.5-x.md"):
        assert not DESIGN_CITATION.search(cited), cited


def test_dry_run_records_without_calls(ctx):
    ctx.settings.dry_run = True
    run(ctx, "repo_write", repo="fleet", path="apps/shop/stateless/prd/kustomization.yaml", content="x\n")
    out = REGISTRY["create_pull_request"].handler(ctx, PR_ARGS)
    assert out["dry_run"] and "## Rollback" in out["body"]
    assert ctx.state["platform"].calls == []


def test_runbook_limits(ctx):
    with pytest.raises(ToolError, match="may not dispatch"):
        run(ctx, "dispatch_runbook", runbook="halt-release", inputs={"environment": "prd", "reason": "x"})
    with pytest.raises(ToolError, match="needs inputs"):
        run(ctx, "dispatch_runbook", runbook="drain-cell", inputs={"environment": "prd", "reason": "x"})
    with pytest.raises(ToolError, match="infra-"):
        run(
            ctx,
            "dispatch_runbook",
            runbook="suspend-kustomization",
            inputs={"environment": "prd", "cell": "sl-az1", "namespace": "infra-x", "name": "n", "reason": "r"},
        )
    run(ctx, "dispatch_runbook", runbook="drain-cell", inputs={"environment": "prd", "cell": "sl-az2", "reason": "5xx"})
    call = ctx.state["platform"].calls[-1]
    assert call[:3] == ("runbook", "o/runbooks", "drain-cell")
    assert call[3]["agent_run"] == "release-agent-x-abc123"


def test_issue(ctx):
    out = run(ctx, "create_issue", repo="fleet", title="t", body="b")
    assert '"number": 3' in out
