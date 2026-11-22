from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""验证评分隔离真正作用到模型请求，及撤销发生后不会发送旧输入。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

from pydantic import BaseModel
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agent.context.builder import ContextBuilder
from app.agent.runtime.contracts import RuntimeContext
from app.infrastructure.persistence.database import Base
from app.domains.sessions.models import LearningSession
from app.application.agent.purposes import grading_payload
from app.application.agent.runner import AgentRunner
from app.agent.tools.contracts import ToolContext


class GradeReply(BaseModel):
    feedback: str


def test_grading_keeps_raw_answer_keys_and_drops_nested_history():
    answer = {"prediction": "甲", "profile": "乙"}
    projected = grading_payload({"questions": [{"question": {"stem": "多空题", "answer": answer,
        "student_state": {"private": "旧判断"}}, "student_answer": "原始回答",
        "prediction": "旧预测"}], "student_state": ["旧状态"]})
    assert projected["questions"][0]["question"]["answer"] == answer
    assert "student_state" not in projected and "prediction" not in projected["questions"][0]
    assert "student_state" not in projected["questions"][0]["question"]


def test_real_runner_grading_never_reads_memory_goals_or_tools(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "grading.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def database():
        with Session(engine, expire_on_commit=False) as db:
            yield db
            db.commit()

    monkeypatch.setattr("app.application.agent.runner.db_session", database)
    with database() as db:
        db.add(LearningSession(id="grading-session", course_id="math", student_id="learner", title="评分测试", goal="HISTORY_GOAL"))

    class Model:
        settings = SimpleNamespace(llm_model="test")
        requests = []

        async def chat(self, messages, **kwargs):
            self.requests.append((deepcopy(messages), kwargs))
            return {"choices": [{"message": {"content": '{"feedback":"依据本题判分"}'}}]}

    class NoHistory:
        def context_for_session(self, *args, **kwargs):
            raise AssertionError("评分不允许加载历史记忆")

    class NoTools:
        def definitions(self, role):
            raise AssertionError("评分不允许开放工具")

    model = Model()
    runner = AgentRunner(model, NoTools())
    runner.memory = NoHistory()
    events = []

    async def emit(event, payload):
        events.append((event, payload))

    result = asyncio.run(runner.run("assessment", "diagnose", {
        "question": {"stem": "计算1+1", "answer": ["2"]}, "student_answer": "2",
        "student_state": ["HISTORY_LABEL"], "prediction": "HISTORY_PREDICTION",
        "learning_memory": {"content": "HISTORY_MEMORY"}, "session_goals": {"goal": "HISTORY_GOAL"},
    }, GradeReply, ToolContext(course_id="math", session_id="grading-session", student_id="learner"),
        "grading-turn", emit=emit, use_tools=True, context_purpose="grading"))
    assert result["output"]["feedback"] == "依据本题判分"
    wire = json.dumps(model.requests, ensure_ascii=False, default=str)
    assert "HISTORY_" not in wire and "student_answer" in wire
    receipt = next(payload for event, payload in events if event == "context_purpose_selected")
    assert not receipt["history_memory_allowed"] and not receipt["tools_enabled"]
    assert not any(event == "memory_context_loaded" for event, _ in events)
    engine.dispose()


def test_revocation_between_preparations_blocks_next_request():
    revoked = False

    def validate():
        if revoked:
            raise ValueError("来源已撤销")

    context = RuntimeContext(scope=ToolContext(), role="knowledge", run_id="r", root_run_id="r", memory_validator=validate)
    context.memory_validator()
    revoked = True
    with pytest.raises(ValueError, match="来源已撤销"):
        context.memory_validator()


def test_scoped_copies_cannot_bypass_memory_budget_and_minimal_projection():
    payload = {"student_message": "学生当前原话", "teaching_workspace": {"dialogue": [], "evidence": []},
        "learning_memory": {"schema_version": "business-memory-v2", "global_memory": None,
            "course_memory": {"scope_key": "course:1", "content": "课程内容"},
            "concept_memories": [{"scope_key": "c:1", "content": "当前概念"}, {"scope_key": "c:2", "content": "旁支概念"}]},
        "student_observations": [{"kind": "concept_memory", "scope_key": "discarded", "content": "预算外副本"}]}
    fixed, history = ContextBuilder().project(payload, "knowledge", minimal=True)
    assert fixed["student_observations"] == []
    assert fixed["learning_memory"]["course_memory"] is None
    assert len(fixed["learning_memory"]["concept_memories"]) == 1
    assert fixed["student_message"] == "学生当前原话"
    assert {item["kind"] for item in history} == {"memory.course", "memory.concept"}
