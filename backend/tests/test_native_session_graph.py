"""真实 SQLite 会话检查点与正式事务的故障恢复，不连接模型服务。"""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import asyncio
import importlib

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage

from app.infrastructure.persistence.database import Base
from app.domains.courses.models import Course, Concept, Question
from app.domains.sessions.models import LearningSession, Message
from app.domains.assessment.models import Attempt
from app.domains.assessment.schemas import AnswerRequest
from app.domains.learning.schemas import MessageRequest
from app.domains.courses.catalog import question_contract
from app.agent.tools.contracts import ToolContext
from app.application.learning.service import LearningService
from app.shared.execution import LearningTurnLost


class ScriptedRunner:
    def __init__(self):
        self.tools = SimpleNamespace()
        self.calls = []

    async def run(self, role, skill, payload, schema, context, turn_id, **kwargs):
        self.calls.append(role)
        if role == "assessment":
            output = {"result": "correct", "score": 1, "confidence": .95, "feedback": "原答选择正确。",
                "misconception": "", "prerequisite_hypothesis": "", "level": "beginner", "suggested_delta": .1,
                "reflection": "依据完整原答与冻结契约。", "next_action": "continue", "concept_results": []}
        else:
            clarify = payload["user_message"] == "需要澄清"
            output = {"action": "clarify" if clarify else "respond", "concept_ids": [],
                "message": "请选择需要解释的具体条件。" if clarify else "已记录本次观察，请继续原学习目标。",
                "steps": [], "rationale": "依据当前输入", "continuation": {"status": "awaiting_clarification" if clarify else "completed",
                    "current_step": "核对输入", "next_step": "等待具体条件" if clarify else "按需继续", "completed_steps": []}}
        output = schema.model_validate(output).model_dump()
        validator = kwargs.get("validate_output")
        if validator:
            validator(output)
        return {"output": output, "run_id": "scripted-" + str(len(self.calls)), "citations": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "messages": [*(kwargs.get("messages") or []), AIMessage(content="已验证正文", id="response-" + context.request_id)]}


@pytest.fixture
def session_database(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "formal.sqlite").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def database():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    for module in ("app.domains.courses.catalog", "app.domains.sessions.service", "app.domains.students.service",
        "app.domains.assessment.service", "app.domains.assessment.batch", "app.domains.practice.service",
        "app.application.learning.service", "app.application.learning.planning", "app.application.learning.entry",
        "app.application.learning.publication", "app.application.learning.actions"):
        monkeypatch.setattr(importlib.import_module(module), "db_session", database)
    with database() as db:
        db.add(Course(id="math", title="数学", description="隔离", source_ref="fixture"))
        db.add(Concept(id="math:c", course_id="math", source_id="c", name="计数"))
        db.add(LearningSession(id="session", course_id="math", student_id="local", title="测试", goal="学习计数", concept_ids=["math:c"]))
        db.add(Question(id="math:q", course_id="math", source_id="q", stem="一加一等于几？", options={"A": "2", "B": "3"},
            answer=["A"], question_type="choice", cognitive_type="recall", concept_ids=["math:c"], explanation="PRIVATE_ANSWER", source_ref="fixture"))
    yield database
    engine.dispose()


async def emit(*args):
    pass


async def pose(learning, database):
    learning.claim("session", "pose")
    with database() as db:
        contract = question_contract(db.get(Question, "math:q"))
    await learning.pose_contract("session", contract, ToolContext(course_id="math", session_id="session",
        student_id="local", concept_ids=["math:c"], request_id="initial-pose"), "pose", "recall", emit)
    learning.release("session", "pose")


def answer_body(learning, key="answer-001"):
    pending = learning.wait("session")
    return AnswerRequest(answer="A", request_id=key, wait_id=pending["wait_id"], question_id="math:q")


def test_wait_survives_saver_reopen_and_rejects_stale_input(session_database, tmp_path):
    async def scenario():
        path = str(tmp_path / "graph.sqlite")
        runner = ScriptedRunner()
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            first = LearningService(runner, checkpointer=saver)
            result = await first.turn("session", MessageRequest(content="需要澄清", request_id="clarify-001"), "turn-1", emit)
            wait = result["session"]["pending_interaction"]
            assert wait["kind"] == "clarify"
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            second = LearningService(runner, checkpointer=saver)
            async def unexpected_delete(*args):
                raise AssertionError("有效等待点必须 resume，不能删检查点伪装恢复")
            saver.adelete_thread = unexpected_delete
            with pytest.raises(ValueError, match="等待点"):
                await second.turn("session", MessageRequest(content="补充", request_id="clarify-002", wait_id="stale"), "turn-2", emit)
            result = await second.turn("session", MessageRequest(content="补充", request_id="clarify-002", wait_id=wait["wait_id"]), "turn-3", emit)
            assert result["session"]["pending_interaction"] is None
            before = len(runner.calls)
            assert (await second.turn("session", MessageRequest(content="补充", request_id="clarify-002", wait_id=wait["wait_id"]), "turn-4", emit))["replayed"]
            assert len(runner.calls) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["commit", "feedback", "supervisor", "publication"])
def test_answer_recovers_committed_boundaries(session_database, tmp_path, monkeypatch, boundary):
    async def scenario():
        runner = ScriptedRunner()
        path = str(tmp_path / "graph.sqlite")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)
            await pose(learning, session_database)
            body = answer_body(learning)
            failed = False
            original = learning.assessment.commit if boundary == "commit" else learning.orchestrate if boundary == "supervisor" else learning.publish_proposal

            async def fail_once(*args, **kwargs):
                nonlocal failed
                if boundary == "supervisor" and not failed:
                    failed = True
                    raise RuntimeError("模型接续暂时失败")
                result = await original(*args, **kwargs)
                if not failed:
                    failed = True
                    raise RuntimeError("正式提交后返回丢失")
                return result

            if boundary == "commit":
                monkeypatch.setattr(learning.assessment, "commit", fail_once)
            elif boundary == "supervisor":
                monkeypatch.setattr(learning, "orchestrate", fail_once)
            elif boundary == "publication":
                monkeypatch.setattr(learning, "publish_proposal", fail_once)

            async def lost_feedback(name, value):
                nonlocal failed
                if boundary == "feedback" and name == "message" and value.get("agent") == "assessment" and not failed:
                    failed = True
                    raise RuntimeError("反馈事件丢失")

            with pytest.raises(RuntimeError):
                await learning.answer("session", body, "first", lost_feedback)
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            result = await restored.answer("session", body, "second", emit)
            assert result["next_plan"]
            assert runner.calls.count("assessment") == 1
            with session_database() as db:
                assert db.scalar(select(func.count()).select_from(Attempt)) == 1
                messages = list(db.scalars(select(Message)))
                assert sum(m.metadata_json.get("kind") == "assessment" for m in messages) == 1
                assert sum(bool(m.metadata_json.get("continuation_for") and m.metadata_json.get("plan")) for m in messages) == 1
            with pytest.raises(ValueError, match="不同输入"):
                await restored.answer("session", body.model_copy(update={"answer": "B"}), "third", emit)
    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", [None, "new-owner"])
def test_transaction_rechecks_owner(session_database, tmp_path, monkeypatch, replacement):
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.sqlite")) as saver:
            learning = LearningService(ScriptedRunner(), checkpointer=saver)
            await pose(learning, session_database)
            body = answer_body(learning)
            original = learning.assessment.commit

            async def change_owner(*args, **kwargs):
                with session_database() as db:
                    db.get(LearningSession, "session").active_turn_id = replacement
                return await original(*args, **kwargs)

            monkeypatch.setattr(learning.assessment, "commit", change_owner)
            with pytest.raises(LearningTurnLost):
                await learning.answer("session", body, "old-owner", emit)
            with session_database() as db:
                assert db.scalar(select(func.count()).select_from(Attempt)) == 0
                assert db.get(LearningSession, "session").active_turn_id == replacement
    asyncio.run(scenario())


def test_new_process_resumes_existing_interrupt(session_database, tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    path = str(tmp_path / "process-graph.sqlite")
    async def first_process():
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(ScriptedRunner(), checkpointer=saver)
            result = await learning.turn("session", MessageRequest(content="需要澄清", request_id="process-first"), "before-restart", emit)
            return result["session"]["pending_interaction"]["wait_id"]
    wait = asyncio.run(first_process())
    formal = str(tmp_path / "formal.sqlite")
    child = r"""
import asyncio, importlib, json, sys
from pathlib import Path
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
sys.path.insert(0,str(Path.cwd()/"backend/tests"))
from test_native_session_graph import ScriptedRunner, emit
from app.application.learning.service import LearningService
from app.domains.learning.schemas import MessageRequest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
engine=create_engine("sqlite:///"+sys.argv[1])
@contextmanager
def database():
 with Session(engine,expire_on_commit=False) as db:
  try: yield db; db.commit()
  except BaseException: db.rollback(); raise
for name in ("app.domains.courses.catalog","app.domains.sessions.service","app.domains.students.service","app.domains.assessment.service","app.domains.assessment.batch","app.domains.practice.service","app.application.learning.service","app.application.learning.planning","app.application.learning.entry","app.application.learning.publication","app.application.learning.actions"):
 setattr(importlib.import_module(name),"db_session",database)
async def main():
 async with AsyncSqliteSaver.from_conn_string(sys.argv[2]) as saver:
  async def forbidden(*args): raise AssertionError("不能删除有效暂停栈")
  saver.adelete_thread=forbidden
  runner=ScriptedRunner(); learning=LearningService(runner,checkpointer=saver)
  before=await learning.graph.aget_state({"configurable":{"thread_id":"learning:session"}})
  assert before.next==("await_student",)
  result=await learning.turn("session",MessageRequest(content="补充条件",request_id="process-second",wait_id=sys.argv[3]),"after-restart",emit)
  print(json.dumps({"calls":runner.calls,"wait":result["session"]["pending_interaction"]}))
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", child, formal, path, wait], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=60, env={**__import__("os").environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"calls": ["supervisor"], "wait": None}


def test_request_cost_limit_survives_new_recorder(session_database):
    from app.application.agent.records import SQLRunRecorder
    context = ToolContext(course_id="math", session_id="session", student_id="local", request_id="logical-retry")
    for index in range(64):
        SQLRunRecorder(session_database).start("run-"+str(index), "knowledge", "auto", context, "turn", "fixture")
    with pytest.raises(ValueError, match="总预算"):
        SQLRunRecorder(session_database).start("overflow", "knowledge", "auto", context, "turn", "fixture")
    context.request_id = "new-request"
    SQLRunRecorder(session_database).start("allowed", "knowledge", "auto", context, "turn", "fixture")
    context.student_id = "someone"
    with pytest.raises(PermissionError):
        SQLRunRecorder(session_database).start("foreign", "knowledge", "auto", context, "turn", "fixture")
