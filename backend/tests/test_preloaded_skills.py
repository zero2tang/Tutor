"""显式技能只进入一次模型上下文，其他技能仍可按需读取。"""

from copy import deepcopy
import json
from types import SimpleNamespace

from pydantic import BaseModel
import pytest

from app.application.agent.prompts import build_instruction
from app.application.agent.runner import AgentRunner
from app.agent.runtime.skills import SkillLibrary
from app.agent.tools.contracts import ToolContext
from test_agent_tools import response, runtime
from native_runtime_helpers import native_tools


class SkillResult(BaseModel):
    text: str


class SkillTools:
    def __init__(self):
        self.reads = []
        self.library = SkillLibrary()

    def definitions(self, role):
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_skill",
                    "description": "读取本角色技能",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            }
        ]

    def validate_call(self, role, name, arguments):
        if name != "read_skill" or set(arguments) != {"name"}:
            raise ValueError("测试仅允许技能读取。")

    async def execute(self, role, name, arguments, context):
        self.validate_call(role, name, arguments)
        self.reads.append(arguments["name"])
        return self.library.read(role, arguments["name"]), {
            "name": name,
            "status": "completed",
        }


class SkillModel:
    settings = SimpleNamespace(llm_model="local-test")

    def __init__(self, names):
        self.names = names
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        if len(self.requests) == 1:
            return response(
                {
                    "tool_calls": [
                        {
                            "id": f"skill-{index}",
                            "type": "function",
                            "function": {
                                "name": "read_skill",
                                "arguments": json.dumps({"name": name}),
                            },
                        }
                        for index, name in enumerate(self.names)
                    ]
                }
            )
        return response(
            {"content": json.dumps({"text": "已按技能继续教学。"}, ensure_ascii=False)}
        )


@pytest.mark.asyncio
async def test_explicit_skill_is_in_system_once_and_same_name_read_uses_cached_receipt(
    runtime, monkeypatch
):
    # 恢复真实指令构建，不能仅把传入的技能名称当成已加载正文。
    monkeypatch.setattr("app.application.agent.runner.build_instruction", build_instruction)
    tools = SkillTools()
    model = SkillModel(["guided_explanation", "understanding"])
    result = await AgentRunner(model, native_tools(tools)).run(
        "exercise",
        "guided_explanation",
        {"student_message": "我为什么要两边同时减6？"},
        SkillResult,
        ToolContext(),
        "turn",
    )
    full = tools.library.read("exercise", "guided_explanation")["instructions"]
    assert model.requests[0][0]["role"] == "system"
    assert model.requests[0][0]["content"].count(full) == 1
    receipts = [
        json.loads(message["content"])
        for message in model.requests[1]
        if message["role"] == "tool"
    ]
    explicit = next(
        value for value in receipts if value["name"] == "guided_explanation"
    )
    other = next(value for value in receipts if value["name"] == "understanding")
    assert explicit["already_loaded"] is True and "instructions" not in explicit
    assert (
        other["instructions"]
        == tools.library.read("exercise", "understanding")["instructions"]
    )
    assert tools.reads == ["understanding"]
    assert result["output"]["text"] == "已按技能继续教学。"
    stored = runtime[result["run_id"]]
    assert stored.tool_calls[0]["cached"] is True
    assert stored.tool_calls[1].get("cached") is not True


@pytest.mark.asyncio
async def test_auto_catalog_does_not_mark_skill_body_as_already_loaded(
    runtime, monkeypatch
):
    monkeypatch.setattr("app.application.agent.runner.build_instruction", build_instruction)
    tools = SkillTools()
    model = SkillModel(["guided_explanation"])
    await AgentRunner(model, native_tools(tools)).run(
        "exercise", "auto", {}, SkillResult, ToolContext(), "turn"
    )
    full = tools.library.read("exercise", "guided_explanation")["instructions"]
    assert full not in model.requests[0][0]["content"]
    receipt = next(
        json.loads(message["content"])
        for message in model.requests[1]
        if message["role"] == "tool"
    )
    assert receipt["instructions"] == full and not receipt.get("already_loaded")
    assert tools.reads == ["guided_explanation"]


@pytest.mark.asyncio
async def test_invalid_explicit_skill_is_rejected_before_model_or_cache(
    runtime, monkeypatch
):
    monkeypatch.setattr("app.application.agent.runner.build_instruction", build_instruction)
    tools = SkillTools()
    model = SkillModel(["nonexistent"])
    with pytest.raises(ValueError, match="技能不存在"):
        await AgentRunner(model, native_tools(tools)).run(
            "exercise", "nonexistent", {}, SkillResult, ToolContext(), "turn"
        )
    assert model.requests == [] and tools.reads == []
