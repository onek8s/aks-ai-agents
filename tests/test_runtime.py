import json

import pytest
from fakes import FakeModel, response, text, use

from aks_agents.definition import AgentDefinition
from aks_agents.runtime import AgentRunner
from aks_agents.settings import PlatformConfig, Settings


@pytest.fixture
def setup(tmp_path):
    design = tmp_path / "design"
    (design / "design").mkdir(parents=True)
    (design / "design" / "15-ai.md").write_text("# 15. AI-driven day 2 operations\nGit is the only write path.\n")
    (design / "requirements").mkdir()
    (design / "requirements" / "0.2-requirements.md").write_text("# Requirements\n")
    (design / "docs").mkdir()
    (design / "docs" / "old.md").write_text("# Old\n")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "test.md").write_text("Test agent instructions.")
    (tmp_path / "agents" / "test-agent.yaml").write_text(
        "name: test-agent\ndescription: t\ninstructions: test.md\ntools: [design_list, {design_search: {}}]\n"
        "model: {effort: low, maxTurns: 5}\n"
    )
    settings = Settings(
        environment="dev", root=tmp_path, design_dir=design, runs_dir=tmp_path / "runs", guardrails_dir=tmp_path
    )
    platform = PlatformConfig({"model": {"provider": "anthropic", "name": "claude-opus-5-5"}})
    return AgentDefinition.load(tmp_path / "agents" / "test-agent.yaml"), settings, platform


def test_run_calls_tools_and_records_everything(setup):
    definition, settings, platform = setup
    model = FakeModel(
        response(text("Looking at the design."), use("design_list", {})),
        response(
            use("kubectl", {"args": "delete ns x"}, id="t2"), use("design_search", {"pattern": "write path"}, id="t3")
        ),
        response(use("finish", {"status": "no_action_needed", "summary": "All good."})),
    )
    result = AgentRunner(definition, settings, platform, model=model, run_id="run-1").run("Daily check")
    assert (result.status, result.summary, result.turns) == ("no_action_needed", "All good.", 3)

    second = model.requests[1]["messages"][-1]["content"][0]
    assert "design/15-ai.md" in second["content"] and "requirements/0.2-requirements.md" in second["content"]
    assert "docs/old.md" not in second["content"]
    third = model.requests[2]["messages"][-1]["content"]
    assert third[0]["is_error"] and "not granted" in third[0]["content"]
    assert "Git is the only write path" in third[1]["content"]
    assert model.requests[0]["effort"] == "low"

    events = [json.loads(line) for line in (settings.runs_dir / "run-1" / "events.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert kinds.count("tool_call") == 3
    assert (settings.runs_dir / "run-1" / "transcript.json").exists()


def test_system_prompt_is_stable_across_runs(setup):
    definition, settings, platform = setup
    a = AgentRunner(definition, settings, platform, model=FakeModel(), run_id="a").system_prompt()
    b = AgentRunner(definition, settings, platform, model=FakeModel(), run_id="b").system_prompt()
    assert a[0] == b[0] and "cache_control" in a[0]
    assert a[1] != b[1] and "Agent-Run: a" in a[1]["text"]


def test_invalid_input_is_reported(setup):
    definition, settings, platform = setup
    model = FakeModel(
        response(use("design_search", {"wrong": 1})),
        response(use("finish", {"status": "failed", "summary": "x"})),
    )
    AgentRunner(definition, settings, platform, model=model, run_id="r").run("t")
    result = model.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "invalid input" in result["content"]


def test_nudge_without_finish_and_turn_limit(setup):
    definition, settings, platform = setup
    model = FakeModel(*[response(text("thinking aloud")) for _ in range(5)])
    result = AgentRunner(definition, settings, platform, model=model, run_id="r").run("t")
    assert result.status == "turn_limit"
    assert "Call finish" in model.requests[1]["messages"][-1]["content"]


def test_refusal_ends_run(setup):
    definition, settings, platform = setup
    model = FakeModel(response(text(""), stop_reason="refusal"))
    assert AgentRunner(definition, settings, platform, model=model, run_id="r").run("t").status == "refused"
