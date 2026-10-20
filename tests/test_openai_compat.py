import json
from types import SimpleNamespace

from aks_agents.openai_compat import AzureOpenAIClient, from_chat_response, to_chat_messages, to_chat_tools


def test_history_and_tools_become_chat_completions():
    system = [{"type": "text", "text": "rules"}, {"type": "text", "text": "run header"}]
    messages = [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": [
                SimpleNamespace(type="text", text="looking"),
                SimpleNamespace(type="tool_use", id="call_1", name="pods_by_ip", input={"ips": ["100.64.32.26"]}),
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "[]"}]},
        {"role": "user", "content": "Call finish."},
    ]
    chat = to_chat_messages(system, messages)
    assert chat[0] == {"role": "system", "content": "rules\n\nrun header"}
    assert chat[1] == {"role": "user", "content": "task"}
    assert chat[2]["tool_calls"][0]["function"] == {"name": "pods_by_ip", "arguments": '{"ips": ["100.64.32.26"]}'}
    assert chat[3] == {"role": "tool", "tool_call_id": "call_1", "content": "[]"}
    assert chat[4] == {"role": "user", "content": "Call finish."}
    tools = to_chat_tools([{"name": "finish", "description": "d", "input_schema": {"type": "object"}}])
    assert tools == [
        {"type": "function", "function": {"name": "finish", "description": "d", "parameters": {"type": "object"}}}
    ]


def test_answer_becomes_content_blocks():
    data = {
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "finish", "arguments": '{"status": "completed"}'}}
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    r = from_chat_response(data, "gpt-4.1-mini")
    assert (
        r.stop_reason == "tool_use" and r.content[0].name == "finish" and r.content[0].input == {"status": "completed"}
    )
    assert r.usage.input_tokens == 10 and r.usage.output_tokens == 3
    assert (
        from_chat_response({"choices": [{"finish_reason": "length", "message": {"content": "x"}}]}, "m").stop_reason
        == "max_tokens"
    )
    assert (
        from_chat_response({"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]}, "m").stop_reason
        == "end_turn"
    )


def test_client_calls_the_deployment_with_an_entra_token():
    seen = {}

    def post(url, headers, body):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    client = AzureOpenAIClient("ais-onek8s-dev-agents", lambda: "tok", post=post)
    r = client.create(
        model="gpt-4.1-mini",
        system=[{"type": "text", "text": "s"}],
        messages=[{"role": "user", "content": "t"}],
        tools=[],
        max_tokens=100,
    )
    assert seen["url"].startswith(
        "https://ais-onek8s-dev-agents.openai.azure.com/openai/deployments/gpt-4.1-mini/chat/completions"
    )
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["body"]["max_tokens"] == 100 and r.content[0].text == "ok"


def test_the_runtime_loop_runs_on_the_openai_client(tmp_path):
    from aks_agents.definition import AgentDefinition, ToolGrant
    from aks_agents.llm import ModelClient
    from aks_agents.runtime import AgentRunner
    from aks_agents.settings import PlatformConfig, Settings

    answers = iter(
        [
            {
                "choices": [
                    {"message": {"tool_calls": [{"id": "c1", "function": {"name": "design_list", "arguments": "{}"}}]}}
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c2",
                                    "function": {
                                        "name": "finish",
                                        "arguments": '{"status": "no_action_needed", "summary": "nothing"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )
    platform = PlatformConfig.load("config/onek8s-dev.yaml")
    model = ModelClient(platform, client=AzureOpenAIClient("r", lambda: "t", post=lambda u, h, b: next(answers)))
    model.provider = "azure-openai"
    design = tmp_path / "design"
    (design / "design").mkdir(parents=True)
    (design / "design" / "01-overview.md").write_text("# Overview\n")
    settings = Settings(environment="dev", runs_dir=tmp_path / "runs", design_dir=design, workspace=tmp_path / "ws")
    definition = AgentDefinition("egress-agent", "", "instructions", [ToolGrant("design_list")])
    result = AgentRunner(definition, settings, platform, model=model).run("task")
    assert result.status == "no_action_needed" and result.turns == 2


def test_a_repeated_identical_call_is_refused(tmp_path):
    from aks_agents.definition import AgentDefinition, ToolGrant
    from aks_agents.llm import ModelClient
    from aks_agents.runtime import AgentRunner
    from aks_agents.settings import PlatformConfig, Settings

    call = {
        "choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "design_list", "arguments": "{}"}}]}}]
    }
    done = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "f",
                            "function": {"name": "finish", "arguments": '{"status": "completed", "summary": "s"}'},
                        }
                    ]
                }
            }
        ]
    }
    answers = iter([call, call, call, done])
    platform = PlatformConfig.load("config/onek8s-dev.yaml")
    model = ModelClient(platform, client=AzureOpenAIClient("r", lambda: "t", post=lambda u, h, b: next(answers)))
    model.provider = "azure-openai"
    design = tmp_path / "design"
    (design / "design").mkdir(parents=True)
    (design / "design" / "01.md").write_text("# x\n")
    settings = Settings(environment="dev", runs_dir=tmp_path / "runs", design_dir=design, workspace=tmp_path / "ws")
    runner = AgentRunner(
        AgentDefinition("egress-agent", "", "i", [ToolGrant("design_list")]), settings, platform, model=model
    )
    runner.run("task")
    events = [line for line in (tmp_path / "runs").rglob("events.jsonl")][0].read_text()
    assert events.count('"tool_call_repeated"') == 1
