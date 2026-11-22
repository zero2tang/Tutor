"""真实工具声明、显式父子运行上下文及取消边界，不保留旧注册表或私有调用栈。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

from langchain.tools import tool, ToolRuntime
from pydantic import BaseModel, ValidationError
import pytest

from app.agent.runtime.contracts import RuntimeContext
from app.application.agent.runner import AgentRunner
from app.application.agent.tools import TeachingTools, KnowledgeArguments, ExerciseArguments
from app.agent.tools.contracts import ToolContext


class Result(BaseModel):
    text: str


class BaseTools:
    def for_role(self, role, *, handler=None):
        @tool
        async def ordinary_lookup(runtime: ToolRuntime[RuntimeContext]):
            """读取当前课程身份。"""
            return {"course": runtime.context.scope.course_id}
        return [ordinary_lookup]


class NoImages:
    def __init__(self, course): pass
    def enrich(self, messages): pass
    def parts(self, result): return []


@pytest.fixture
def runtime(monkeypatch):
    rows = {}
    class Database:
        def add(self, row): rows[row.id] = row
        def get(self, model, identifier): return rows[identifier]
    @contextmanager
    def database(): yield Database()
    monkeypatch.setattr("app.application.agent.runner.db_session", database)
    monkeypatch.setattr("app.application.agent.runner.build_instruction", lambda role, schema, skill: role)
    return rows


def response(message):
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}


class NestedModel:
    settings = SimpleNamespace(llm_model="isolated-model")
    def __init__(self): self.requests = []
    async def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        role = messages[0]["content"]
        receipts = [message for message in messages if message["role"] == "tool"]
        if role == "exercise":
            return response({"content": json.dumps({"text": "习题子任务完成"}, ensure_ascii=False)})
        if receipts:
            content = json.loads(receipts[-1]["content"])["content"]
            return response({"content": json.dumps({"text": "已读取：" + content}, ensure_ascii=False)})
        name = "call_knowledge_agent" if role == "supervisor" else "call_exercise_agent"
        args = {"task": "解释应力", "concept_ids": ["mechanics:c19"]}
        if role == "knowledge": args["operation"] = "select"
        return response({"tool_calls": [{"id": "call-" + role, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})


@pytest.mark.asyncio
async def test_nested_agent_results_resume_each_calling_model_loop_and_persist_parent_links(runtime):
    model, events, inherited = NestedModel(), [], []
    runner = AgentRunner(model, TeachingTools(None))
    async def emit(name, data): events.append((name, data))
    async def handler(role, name, args, context, *, runtime_context, config):
        inherited.append(runtime_context)
        child = await runner.run("knowledge" if name == "call_knowledge_agent" else "exercise",
            "auto", {"task": args["task"]}, Result, context, "turn", emit=emit,
            tool_source=handler, runtime_context=runtime_context, config=config)
        return {"content": child["output"]["text"], "child_run_ids": [child["run_id"]]}
    result = await runner.run("supervisor", "auto", {}, Result, ToolContext(), "turn", emit=emit, tool_source=handler)
    assert result["output"]["text"] == "已读取：已读取：习题子任务完成"
    started = {data["agent"]: data for name, data in events if name == "agent_started"}
    assert started["knowledge"]["parent_run_id"] == result["run_id"]
    assert started["exercise"]["parent_run_id"] == started["knowledge"]["run_id"]
    assert started["exercise"]["depth"] == 2
    assert all(data["root_run_id"] == result["run_id"] for data in started.values())
    assert inherited[0].budget is inherited[1].budget
    assert all(row.status == "completed" for row in runtime.values())


@pytest.mark.parametrize("role,name,allowed", [
    ("supervisor", "call_knowledge_agent", True), ("knowledge", "call_knowledge_agent", False),
    ("supervisor", "call_exercise_agent", True), ("knowledge", "call_exercise_agent", True),
    ("exercise", "call_exercise_agent", False), ("assessment", "call_exercise_agent", False),
])
def test_role_permissions_match_model_visible_definitions(role, name, allowed):
    names = {item.name for item in TeachingTools(None).for_role(role, handler=object())}
    assert (name in names) is allowed


@pytest.mark.parametrize("extra", ["course_id", "session_id", "student_id", "parent_run_id"])
def test_model_arguments_cannot_inject_trusted_identity(extra):
    with pytest.raises(ValidationError):
        KnowledgeArguments.model_validate({"task": "解释", "concept_ids": ["c"], extra: "overridden"})


def test_nested_batch_arguments_cannot_inject_identity_and_uuid_is_validated():
    with pytest.raises(ValidationError):
        ExerciseArguments.model_validate({"operation": "batch", "concept_ids": ["c"], "exercise_batch": {
            "tasks": [{"concept_ids": ["c"], "student_id": "other"}]}})
    with pytest.raises(ValidationError):
        ExerciseArguments.model_validate({"operation": "explain", "concept_ids": ["c"], "exercise_id": "question-id"})


@pytest.mark.parametrize("operation,count,allowed", [
    ("generate", None, True), ("generate", 1, True), ("generate", 2, False),
    ("batch", 1, True), ("batch", 2, True), ("batch", None, False), ("select", 1, False), ("explain", 1, False),
])
def test_generation_operation_requires_a_compatible_task_plan(operation, count, allowed):
    args = {"operation": operation, "concept_ids": ["c"]}
    if count is not None: args["exercise_batch"] = {"tasks": [{"concept_ids": ["c"]} for _ in range(count)]}
    if allowed: ExerciseArguments.model_validate(args)
    else:
        with pytest.raises(ValidationError): ExerciseArguments.model_validate(args)


class BlockingModel:
    settings = SimpleNamespace(llm_model="local")
    def __init__(self): self.started = asyncio.Event()
    async def chat(self, messages, **options):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancelled_child_keeps_explicit_parent_context_and_records_failure(runtime):
    model, events = BlockingModel(), []
    runner = AgentRunner(model, BaseTools())
    parent = RuntimeContext(scope=ToolContext(), role="supervisor", run_id="parent", root_run_id="parent")
    async def emit(name, data): events.append((name, data))
    task = asyncio.create_task(runner.run("knowledge", "auto", {}, Result, ToolContext(), "turn",
                                         runtime_context=parent, emit=emit))
    await asyncio.wait_for(model.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert parent.run_id == "parent" and next(iter(runtime.values())).status == "cancelled"
    failure = next(data for name, data in events if name == "agent_failed")
    assert failure["parent_run_id"] == "parent" and failure["depth"] == 1


@pytest.mark.asyncio
async def test_timeout_records_failed_run_without_private_context_state(runtime):
    runner = AgentRunner(BlockingModel(), BaseTools())
    with pytest.raises(TimeoutError):
        await runner.run("supervisor", "auto", {}, Result, ToolContext(), "turn", timeout_seconds=0.01)
    assert next(iter(runtime.values())).status == "failed"
