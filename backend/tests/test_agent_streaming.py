"""验证正文在模型结束前送出，且私有字段、修复草稿与最终消息保持隔离。"""

import asyncio
import json

import pytest

from native_runtime_helpers import LoopState, run_agent_loop
from app.application.agent.streaming import AgentTextStream, PublicJSONText
from app.domains.practice.schemas import ExerciseDraft
from app.domains.learning.schemas import Lesson, Plan
from app.shared.execution import LearningTurnLost
from app.agent.tools.contracts import ToolContext


@pytest.mark.parametrize("width", [1, 2, 7, 1000])
def test_public_fields_handle_fragmented_json_and_escapes(width):
    payload = {
        "private": {"content": "嵌套答案不公开"},
        "title": '引号"与括号{}',
        "content": "中文\n公式 \\(a+b\\) 与表情 😀",
        "array": [{"follow_up": "嵌套追问不公开"}],
        "follow_up": "你如何理解？",
        "answer": ["参考答案不公开"],
    }
    source = "```json\n" + json.dumps(payload, ensure_ascii=True) + "\n```"
    parser = PublicJSONText({"title", "content", "follow_up"})
    fields = {}
    for start in range(0, len(source), width):
        for field, delta in parser.feed(source[start : start + width]):
            fields[field] = fields.get(field, "") + delta
    assert fields == {key: payload[key] for key in ("title", "content", "follow_up")}


@pytest.mark.asyncio
async def test_private_exercise_schema_never_emits_draft_answers():
    events = []

    async def emit(event, data):
        events.append((event, data))

    stream = AgentTextStream(ExerciseDraft, "exercise", emit)
    await stream.feed('{"stem":"题干","answer":["秘密"],"explanation":"私有解析"}')
    assert not stream.active and events == []


class NoTools:
    def definitions(self, role):
        return []


def plan(message):
    return {
        "action": "respond",
        "concept_ids": [],
        "message": message,
        "steps": [],
        "rationale": "内部依据不显示",
    }


@pytest.mark.asyncio
async def test_text_reaches_sink_before_model_finishes():
    first, release = asyncio.Event(), asyncio.Event()
    events = []

    class PausedModel:
        async def chat(self, messages, **kwargs):
            await kwargs["on_content"]('{"message":"正在逐段')
            first.set()
            await release.wait()
            await kwargs["on_content"]('显示"}')
            return {
                "choices": [{"message": {"content": json.dumps(plan("正在逐段显示"))}}]
            }

    async def emit(event, data):
        events.append((event, data))

    task = asyncio.create_task(
        run_agent_loop(
            PausedModel(),
            NoTools(),
            "supervisor",
            [],
            Plan,
            ToolContext(),
            LoopState(),
            emit=emit,
        )
    )
    try:
        await asyncio.wait_for(first.wait(), timeout=1)
        assert not task.done()
        assert events[0][0] == "text_delta"
        assert events[0][1]["delta"] == "正在逐段"
    finally:
        release.set()
        output = await task
    assert output["message"] == "正在逐段显示"
    assert "内部依据" not in str(events)


@pytest.mark.asyncio
async def test_failed_validation_resets_only_failed_round():
    events = []

    class RepairingModel:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            output = plan("无效草稿" if self.calls == 1 else "修复后正文")
            raw = json.dumps(output, ensure_ascii=False)
            await kwargs["on_content"](raw)
            return {"choices": [{"message": {"content": raw}}]}

    async def emit(event, data):
        events.append((event, data))

    def validate(output):
        if output["message"] == "无效草稿":
            raise ValueError("测试业务约束失败")

    output = await run_agent_loop(
        RepairingModel(),
        NoTools(),
        "supervisor",
        [],
        Plan,
        ToolContext(),
        LoopState(),
        emit=emit,
        validate_output=validate,
    )
    assert output["message"] == "修复后正文"
    assert [event for event, _ in events] == ["text_delta", "text_reset", "text_delta"]
    assert events[0][1]["stream_id"] == events[1][1]["stream_id"]
    assert events[2][1]["stream_id"] != events[0][1]["stream_id"]


@pytest.mark.asyncio
async def test_model_failure_retracts_incomplete_text():
    events = []

    class FailingModel:
        async def chat(self, messages, **kwargs):
            await kwargs["on_content"]('{"content":"未完成讲解')
            raise RuntimeError("上游中断")

    async def emit(event, data):
        events.append((event, data))

    with pytest.raises(RuntimeError, match="上游中断"):
        await run_agent_loop(
            FailingModel(),
            NoTools(),
            "knowledge",
            [],
            Lesson,
            ToolContext(),
            LoopState(),
            emit=emit,
        )
    assert [event for event, _ in events] == ["text_delta", "text_reset"]


@pytest.mark.asyncio
async def test_invalid_escape_retracts_preview_but_leaves_final_repair_available():
    events = []

    async def emit(event, data):
        events.append((event, data))

    stream = AgentTextStream(Plan, "supervisor", emit)
    await stream.feed('{"message":"已显示')
    await stream.feed('\\x无效转义"}')
    assert [event for event, _ in events] == ["text_delta", "text_reset"]
    assert not stream.active


@pytest.mark.parametrize("error_type", [LearningTurnLost, PermissionError])
@pytest.mark.asyncio
async def test_fatal_validation_retracts_preview_without_model_retry(error_type):
    events = []

    class Model:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            raw = json.dumps(plan("不能提交的草稿"), ensure_ascii=False)
            await kwargs["on_content"](raw)
            return {"choices": [{"message": {"content": raw}}]}

    async def emit(event, data):
        events.append((event, data))

    def validate(_output):
        raise error_type("当前运行不再允许提交")

    model = Model()
    with pytest.raises(error_type, match="不再允许提交"):
        await run_agent_loop(
            model, NoTools(), "supervisor", [], Plan, ToolContext(), LoopState(),
            emit=emit, validate_output=validate,
        )
    assert model.calls == 1
    assert [event for event, _ in events] == ["text_delta", "text_reset"]
    assert events[0][1]["stream_id"] == events[1][1]["stream_id"]


@pytest.mark.asyncio
async def test_cancelling_model_request_retracts_preview():
    events = []
    preview_ready = asyncio.Event()

    class PausedModel:
        async def chat(self, messages, **kwargs):
            await kwargs["on_content"]('{"message":"已开始但未完成')
            preview_ready.set()
            await asyncio.Event().wait()

    async def emit(event, data):
        events.append((event, data))

    task = asyncio.create_task(run_agent_loop(
        PausedModel(), NoTools(), "supervisor", [], Plan, ToolContext(), LoopState(),
        emit=emit,
    ))
    try:
        await asyncio.wait_for(preview_ready.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [event for event, _ in events] == ["text_delta", "text_reset"]
    assert events[0][1]["stream_id"] == events[1][1]["stream_id"]
