"""检查模型输入精简、可信资料注入及每轮请求审计，不连接外部服务。"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

from app.application.agent.tools import compact_tool_result
from native_runtime_helpers import LoopState, run_agent_loop
from app.application.agent.runner import AgentRunner
from app.domains.learning.schemas import Plan
from app.agent.tools.contracts import ToolContext
from test_agent_tools import BaseTools, Result, response, runtime


def test_history_projection_preserves_original_current_task():
    from app.agent.context.builder import ContextBuilder
    original={"user_message":"当前真实原答"*2000,"question":{"stem":"完整题面"},
        "recent_messages":[{"role":"user","content":str(i)*1800} for i in range(10)]}
    saved=deepcopy(original)
    fixed,history=ContextBuilder().project(original,"supervisor")
    assert original==saved and fixed["user_message"]==original["user_message"]
    assert fixed["recent_messages"][-2:]==original["recent_messages"][-2:] and history


def test_retrieval_excerpt_keeps_source_ids_scope_and_original_data():
    source = {
        "id": "math:c1",
        "summary": "概念摘要" * 2000,
        "materials": [
            {
                "id": "material-1",
                "text": "资料正文" * 2000,
                "source_ref": "公开教材",
                "origin": "fixture",
            }
        ],
        "selected_material_ids": ["material-handle"],
        "total_count": 12,
    }
    saved = deepcopy(source)
    projected = compact_tool_result(source, tool_name="get_concept_details")
    assert source == saved
    assert len(projected["materials"][0]["text"]) < 3100
    assert projected["materials"][0]["id"] == "material-1"
    assert projected["materials"][0]["source_ref"] == "公开教材"
    assert projected["selected_material_ids"] == ["material-handle"]
    assert projected["total_count"] == 12 and projected["context_truncated"]
    # 技能方法与题目契约不适用检索摘录预算。
    assert compact_tool_result(
        {"instructions": source["summary"]}, tool_name="read_skill"
    ) == {"instructions": source["summary"]}


def test_retired_transfer_protocol_cannot_enter_trusted_context():
    import pytest
    with pytest.raises(TypeError):ToolContext(delegated_materials=[{"content":"伪造"}])


def test_each_model_round_emits_metadata_without_private_request_fields():
    events = []

    class Model:
        async def chat(self, messages, **kwargs):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "respond",
                                    "concept_ids": [],
                                    "message": "你好",
                                    "steps": [],
                                    "rationale": "内部依据",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "prompt_cache_hit_tokens": 8,
                    "prompt_cache_miss_tokens": 2,
                },
                "_request_metrics": {
                    "thinking": "disabled",
                    "stream": True,
                    "duration_ms": 1.0,
                    "first_content_ms": 0.2,
                    "reasoning_tokens": 0,
                    "api_key": "不可进入审计",
                },
            }

    class NoTools:
        def definitions(self, role):
            return []

    async def emit(event, data):
        events.append((event, data))

    state = LoopState()
    asyncio.run(
        run_agent_loop(
            Model(),
            NoTools(),
            "supervisor",
            [],
            Plan,
            ToolContext(),
            state,
            emit=emit,
        )
    )
    metrics = next(data for event, data in events if event == "model_request_completed")
    assert metrics["round"] == 1 and metrics["thinking"] == "disabled"
    assert metrics["usage"]["prompt_cache_hit_tokens"] == 8
    assert metrics["reasoning_tokens"] == 0 and "api_key" not in metrics
    assert state.usage == {"prompt_tokens": 10, "completion_tokens": 3}
