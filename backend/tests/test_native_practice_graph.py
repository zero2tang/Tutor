"""以真实 LangGraph 与短事务验证质量门槛、并发和正式回执恢复。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4
from typing import TypedDict

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agent.tools.contracts import ToolContext
from app.agent.tools.errors import ToolRejected
from app.application.learning.subgraphs.practice import batch_generation_graph, generation_graph
from app.domains.courses.models import Concept, Course, Question
from app.domains.practice.batches import ExerciseBatchService
from app.domains.practice.service import PracticeService
from app.domains.sessions.models import LearningSession, Message
from app.infrastructure.persistence.database import Base
from app.infrastructure.persistence.execution_models import AgentRun
from app.shared.execution import LearningTurnLost


@pytest.fixture
def practice_database(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "practice.db").as_posix())
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

    for module in ("service", "batches"):
        monkeypatch.setattr(f"app.domains.practice.{module}.db_session", database)
    monkeypatch.setattr("app.domains.courses.catalog.db_session", database)
    with database() as db:
        db.add(Course(id="math", title="数学", description="测试", source_ref="test"))
        db.add(Concept(id="math:c1", course_id="math", source_id="c1", name="计数"))
        db.add(LearningSession(
            id="session", course_id="math", student_id="student", title="测试",
            goal="计数", concept_ids=["math:c1"], active_turn_id="owner",
        ))
    yield database
    engine.dispose()


async def emit(event, data):
    return None


def context(request="request"):
    return ToolContext(
        course_id="math", session_id="session", student_id="student",
        concept_ids=["math:c1"], request_id=request,
    )


def specification(count=3):
    return {"title": "计数练习", "tasks": [
        {"concept_ids": ["math:c1"], "question_type": "fill", "instruction": f"要求{index}"}
        for index in range(count)
    ]}


class ScriptedRunner:
    def __init__(self, database):
        self.database = database
        self.calls = []
        self.inflight = self.peak = 0
        self.fail_index = None
        self.reject_reviews = 0

    async def run(self, role, skill, payload, schema, ctx, turn_id, **options):
        self.calls.append(deepcopy(payload))
        task = payload["task"]
        if task == "generate":
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            assignment = payload["assignment"]
            index = assignment.get("index", 1)
            try:
                await asyncio.sleep(0.01 if index != self.fail_index else 0.1)
                if index == self.fail_index:
                    raise ValueError("模拟一个子任务生成失败")
                output = {
                    "stem": f"第{index}题：从两个不同物品中任取一个，共有多少种不同选择？",
                    "question_type": "fill", "options": {}, "answer": ["2"],
                    "explanation": "PRIVATE_GENERATOR：逐个计数得到两个。",
                    "concept_ids": list(ctx.concept_ids), "cognitive_type": skill,
                }
            finally:
                self.inflight -= 1
        elif task == "validate":
            assert "answer" not in payload and "explanation" not in payload
            assert "PRIVATE_GENERATOR" not in str(payload)
            output = {"answer": ["2"], "explanation": "独立计数得到两个。", "valid": True, "issue": ""}
        elif task == "reconcile":
            accepted = self.reject_reviews == 0
            self.reject_reviews = max(self.reject_reviews - 1, 0)
            output = {
                "generation_assessment": {
                    "submitted_answer": payload["generation_solution"]["answer"],
                    "reason": "核对真实生成答案及其解析。", "correctness": "correct",
                    "answer_explanation_consistency": "consistent",
                },
                "independent_assessment": {
                    "submitted_answer": payload["independent_solution"]["answer"],
                    "reason": "核对真实盲解答案及其解析。", "correctness": "correct",
                    "answer_explanation_consistency": "consistent",
                },
                "verdict": "equivalent" if accepted else "uncertain",
                "answer": ["2"] if accepted else [],
                "reason": "两份解答均正确。" if accepted else "信息仍需检查。",
                "explanation": "PRIVATE_REVIEW：每个物品对应一种选择。" if accepted else "",
            }
        else:
            raise AssertionError(payload)
        output = schema.model_validate(output).model_dump()
        if options.get("validate_output"):
            options["validate_output"](output)
        run_id = str(uuid4())
        with self.database() as db:
            db.add(AgentRun(
                id=run_id, session_id=ctx.session_id, turn_id=turn_id, agent=role,
                skill=skill, status="completed", model="isolated-script",
            ))
        return {"output": output, "run_id": run_id, "citations": []}


def services(database):
    runner = ScriptedRunner(database)
    exercises = PracticeService(runner, generation_graph=generation_graph())
    return exercises, ExerciseBatchService(exercises, batch_graph=batch_generation_graph()), runner


@pytest.mark.asyncio
async def test_single_generation_replays_frozen_contract_without_model(practice_database):
    exercises, _, runner = services(practice_database)
    first, result = await exercises.generate(
        context(), "owner", "understanding", emit, assignment={"question_type": "fill"},
    )
    assert [item["task"] for item in runner.calls] == ["generate", "validate", "reconcile"]
    with practice_database() as db:
        row = db.get(Question, first["id"])
        row.stem = "题库题干后续被修改，不能改写历史冻结结果。"
    replay, prior = await exercises.generate(
        context(), "owner", "understanding", emit, assignment={"question_type": "fill"},
    )
    assert replay == first and prior["run_id"] == result["run_id"]
    assert len(runner.calls) == 3
    with pytest.raises(ValueError, match="不同输入"):
        await exercises.generate(context(), "owner", "recall", emit, assignment={"question_type": "fill"})


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "different"])
async def test_missing_or_changed_owner_rejects_generation_before_models(practice_database, owner):
    exercises, _, runner = services(practice_database)
    with practice_database() as db:
        db.get(LearningSession, "session").active_turn_id = owner
    with pytest.raises(LearningTurnLost):
        await exercises.generate(context(), "owner", "understanding", emit)
    assert not runner.calls


@pytest.mark.asyncio
async def test_identity_is_required_and_review_retries_are_bounded(practice_database):
    exercises, _, runner = services(practice_database)
    with pytest.raises(ValueError, match="稳定"):
        await exercises.generate(context(None), "owner", "understanding", emit)
    runner.reject_reviews = 2
    with pytest.raises(ToolRejected):
        await exercises.generate(context(), "owner", "understanding", emit, assignment={"question_type": "fill"})
    assert [p["task"] for p in runner.calls] == ["generate", "validate", "reconcile"] * 2
    with pytest.raises(ToolRejected):
        await exercises.generate(context(), "owner", "understanding", emit, assignment={"question_type": "fill"})
    assert len(runner.calls) == 6
    with practice_database() as db:
        assert db.scalar(select(func.count()).select_from(Question)) == 0


@pytest.mark.asyncio
async def test_changed_business_version_rejects_candidate_before_review(practice_database):
    exercises, _, runner = services(practice_database)
    original = runner.run

    async def changed_version(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[2]["task"] == "validate":
            with practice_database() as db:
                db.get(LearningSession, "session").version += 1
        return result

    runner.run = changed_version
    with pytest.raises(LearningTurnLost, match="版本"):
        await exercises.generate(context(), "owner", "understanding", emit, assignment={"question_type": "fill"})
    assert [item["task"] for item in runner.calls] == ["generate", "validate"]
    with practice_database() as db:
        assert db.scalar(select(func.count()).select_from(Question)) == 0


@pytest.mark.asyncio
async def test_send_limits_six_items_to_three_and_publishes_in_input_order(practice_database):
    _, batches, runner = services(practice_database)
    result, _ = await batches.generate(specification(6), context(), "owner", emit)
    public = result["exercise_batch"]
    assert runner.peak == 3
    assert [question["stem"].split("：")[0] for question in public["questions"]] == [f"第{i}题" for i in range(1, 7)]
    assert "PRIVATE_" not in str(result)
    calls = len(runner.calls)
    replay, _ = await batches.generate(specification(6), context(), "owner", emit)
    assert replay == result and len(runner.calls) == calls
    with practice_database() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 6
        assert db.get(LearningSession, "session").pending_question_id
    with pytest.raises(ValueError, match="不同输入"):
        await batches.generate(specification(5), context(), "owner", emit)


@pytest.mark.asyncio
async def test_failed_batch_does_not_publish_and_retry_reuses_accepted_questions(practice_database):
    _, batches, runner = services(practice_database)
    runner.fail_index = 6
    with pytest.raises(ValueError, match="模拟"):
        await batches.generate(specification(6), context(), "owner", emit)
    with practice_database() as db:
        accepted = db.scalar(select(func.count()).select_from(Question))
        assert accepted > 0
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        assert db.get(LearningSession, "session").pending_question_id is None
    first_calls = sum(item["task"] == "generate" for item in runner.calls)
    runner.fail_index = None
    result, _ = await batches.generate(specification(6), context(), "owner", emit)
    assert len(result["exercise_batch"]["questions"]) == 6
    assert sum(item["task"] == "generate" for item in runner.calls) - first_calls == 6 - accepted


@pytest.mark.asyncio
async def test_send_failure_waits_for_sibling_cancellation_before_return(practice_database):
    exercises, batches, runner = services(practice_database)
    started, finished = set(), set()
    ready = asyncio.Event()

    async def blocked_runner(role, skill, payload, *args, **kwargs):
        assert payload["task"] == "generate"
        index = payload["assignment"]["index"]
        started.add(index)
        if len(started) == 3:
            ready.set()
        try:
            await ready.wait()
            if index == 1:
                raise RuntimeError("模拟并发生成中的系统失败")
            await asyncio.Event().wait()
        finally:
            finished.add(index)

    runner.run = blocked_runner
    with pytest.raises(RuntimeError, match="系统失败"):
        await asyncio.wait_for(batches.generate(specification(3), context(), "owner", emit), 5)
    assert started == finished == {1, 2, 3}
    with practice_database() as db:
        assert db.scalar(select(func.count()).select_from(Question)) == 0
        assert db.scalar(select(func.count()).select_from(Message)) == 0


@pytest.mark.asyncio
async def test_batch_commit_with_lost_return_replays_before_pending_check(practice_database):
    _, batches, runner = services(practice_database)

    async def lost_return(event, data):
        if event == "exercise_batch":
            raise ConnectionError("模拟正式提交之后返回丢失")

    with pytest.raises(ConnectionError):
        await batches.generate(specification(1), context(), "owner", lost_return)
    calls = len(runner.calls)
    result, _ = await batches.generate(specification(1), context(), "owner", emit)
    assert len(result["exercise_batch"]["questions"]) == 1 and len(runner.calls) == calls
    with practice_database() as db:
        assert db.scalar(select(func.count()).select_from(Question)) == 1
        assert db.scalar(select(func.count()).select_from(Message)) == 1


@pytest.mark.asyncio
async def test_sqlite_parent_reopen_reuses_committed_child_after_failed_checkpoint_boundary(
    practice_database, tmp_path,
):
    exercises, batches, runner = services(practice_database)
    fail = True

    async def interrupted_emit(event, data):
        nonlocal fail
        if fail and event == "exercise_quality_checked" and data.get("status") == "accepted":
            fail = False
            raise RuntimeError("模拟题目提交后父子图尚未完成")

    class ParentState(TypedDict, total=False):
        result: dict

    def compile_parent(saver, service):
        async def work(state, config):
            result, _ = await service.generate(
                specification(1), context(), "owner", interrupted_emit, config=config,
            )
            return {"result": result}

        graph = StateGraph(ParentState)
        graph.add_node("work", work)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": "session:durable-practice"}}
    checkpoint_file = str(tmp_path / "checkpoints.sqlite")
    async with AsyncSqliteSaver.from_conn_string(checkpoint_file) as saver:
        graph = compile_parent(saver, batches)
        with pytest.raises(RuntimeError, match="父子图"):
            await graph.ainvoke({}, config, durability="sync")
    calls = len(runner.calls)
    assert calls == 3
    # 重新创建 saver、生成图与批图；恢复不依赖原来的内存图对象。
    new_exercises = PracticeService(runner, generation_graph=generation_graph())
    new_batches = ExerciseBatchService(new_exercises, batch_graph=batch_generation_graph())
    async with AsyncSqliteSaver.from_conn_string(checkpoint_file) as saver:
        graph = compile_parent(saver, new_batches)
        resumed = await graph.ainvoke(None, config, durability="sync")
    assert len(resumed["result"]["exercise_batch"]["questions"]) == 1
    assert len(runner.calls) == calls
    with practice_database() as db:
        assert db.scalar(select(func.count()).select_from(Question)) == 1
        assert db.scalar(select(func.count()).select_from(Message)) == 1
