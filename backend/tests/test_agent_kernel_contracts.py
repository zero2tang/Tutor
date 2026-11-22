"""验证执行内核的注入边界、原模型接续及取消行为，不加载真实数据库。"""

import ast
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel
import pytest

from app.agent.runtime.contracts import PreparedRun
from app.application.agent.runner import AgentRunner
from app.agent.tools.contracts import ToolContext
from app.shared.execution import LearningTurnLost


class Output(BaseModel):
    text: str
    private: str = ""


class Recorder:
    def __init__(self):
        self.records = []

    def start(self, *args):
        self.records.append(("started", args[0]))

    def complete(self, run_id, duration, usage, trace, output):
        self.records.append(("completed", deepcopy(trace), dict(usage)))

    def fail(self, run_id, duration, usage, trace, error):
        self.records.append(("failed", error, deepcopy(trace), dict(usage)))


async def prepare(
    role, skill, payload, schema, context, run_id, emit, use_tools, max_tokens, **options
):
    return PreparedRun(
        payload=deepcopy(payload),
        instruction="只使用当前输入",
        purpose="unit",
        use_tools=use_tools,
        display_name="执行测试",
        public_fields=frozenset({"text"}),
    )


def test_agent_modules_do_not_import_business_application_or_infrastructure():
    root = Path(__file__).resolve().parents[1] / "app/agent"
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            names = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [item.name for item in node.names] if isinstance(node, ast.Import) else []
            )
            violations.extend(
                (path.name, node.lineno, name)
                for name in names
                if name
                and name.startswith(("app.domains", "app.application", "app.infrastructure"))
            )
    assert violations == []




@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LearningTurnLost("任务已被接管"), asyncio.CancelledError()])
async def test_lease_loss_and_cancellation_reset_partial_text_and_reach_recorder(error):
    events = []

    class Models:
        settings = SimpleNamespace(llm_model="isolated")

        async def chat(self, messages, **options):
            await options["on_content"]('{"text":"未完成草稿')
            raise error

    async def emit(event, data):
        events.append((event, data))

    recorder = Recorder()
    runner = AgentRunner(Models(), object(), prepare_run=prepare, recorder=recorder)
    with pytest.raises(type(error)):
        await runner.run("worker", "auto", {}, Output, ToolContext(), "turn", emit=emit, use_tools=False)
    assert any(event == "text_reset" for event, _ in events)
    assert recorder.records[-1][0] == "failed" and recorder.records[-1][1] is error




@pytest.mark.asyncio
async def test_teaching_policy_keeps_scoped_revocation_validator_and_budgeted_projection():
    from app.application.agent.policy import TeachingRunPolicy

    revoked = False

    class Hierarchy:
        def validate_context(self, manifest):
            assert manifest == {"version": 3}
            return not revoked

    class Memory:
        business_pipeline = SimpleNamespace(hierarchy=Hierarchy())

        def context_for_session(self, session_id, concepts, **options):
            assert options["purpose"] == "teaching" and options["input_budget"] > 0
            return {
                "memory_manifest": {"version": 3},
                "private_full_snapshot": "不发送模型",
                "model_payload": {"course_memory": {"content": "预算内记忆"}},
                "memory_invalidation_fingerprint": "barrier-1",
            }

    @contextmanager
    def database():
        yield SimpleNamespace(get=lambda *args: None)

    policy = TeachingRunPolicy(
        SimpleNamespace(settings=SimpleNamespace()),
        database,
        memory=lambda: Memory(),
        instruction_builder=lambda *args: "教学规则",
        image_factory=lambda *args: None,
    )
    prepared = await policy.prepare(
        "knowledge", "auto", {}, Output, ToolContext(session_id="session"), "run", None, True, 1000
    )
    assert prepared.payload["learning_memory"]["course_memory"]["content"] == "预算内记忆"
    assert "private_full_snapshot" not in prepared.payload["learning_memory"]
    prepared.memory_validator()
    revoked = True
    with pytest.raises(ValueError, match="纠正或撤销"):
        prepared.memory_validator()
