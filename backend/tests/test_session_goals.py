"""验证目标图与题目恢复的服务边界，不把模型的活动总结当作能力证据。"""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.infrastructure.persistence.database import Base
from app.domains.sessions.models import LearningSession, Message
from app.domains.students.models import StudentState
from app.domains.sessions.models import SessionGoalState
from app.domains.learning.schemas import Plan
from app.application.learning.planning import LearningPlanner
from app.domains.sessions.goals import GoalVersionConflict, apply_goal_update, build_goal_candidate, context_projection, ensure_session_goals, public_goals, sync_workflow


@pytest.fixture
def database(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "goals.db").as_posix())
    Base.metadata.create_all(engine)

    @contextmanager
    def transaction():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    with transaction() as db:
        db.add(LearningSession(
            id="mine", course_id="math", student_id="student-1", title="方程学习",
            goal="独立解含括号的一元一次方程", active_turn_id="turn-1",
            pending_question_id="math:q1", pending_context={
                "contract": {"id": "math:q1", "version": 1, "stem": "-2(x-3)=8", "answer": ["-1"]},
                "assisted": True,
                "learning_progress": {"current_step": "去括号", "next_step": "在原题写下一行"},
            },
        ))
        db.add(Message(id="own-message", session_id="mine", role="user", content="先补负数乘法"))
        db.add(Message(id="foreign-message", session_id="other", role="user", content="无关请求"))
    yield transaction
    engine.dispose()


def node(identifier, parent, **changes):
    return {
        "goal_id": identifier, "parent_id": parent, "description": "正确去括号",
        "source_ref": "message:own-message", "success_criteria": ["独立写出本题去括号的一行"],
        "status": "planned", "prerequisites": [], "evidence_refs": [], **changes,
    }


def add_current(db, row, goals):
    return apply_goal_update(db, row, {
        "expected_version": goals.version,
        "nodes": [node("expand", goals.root_goal_id)],
        "active_goal_id": "expand", "next_action": "请写出去括号后的等式",
    })


def test_new_session_does_not_copy_another_sessions_goals(database):
    with database() as db:
        original = db.get(LearningSession, "mine")
        old = add_current(db, original, ensure_session_goals(db, original))
        new = LearningSession(id="new", course_id="math", student_id="student-1", title="新会话", goal="学习因式分解", pending_context={})
        db.add(new)
        fresh = ensure_session_goals(db, new)
        assert fresh.root_goal_id != old.root_goal_id
        assert fresh.active_goal_id == fresh.root_goal_id and fresh.return_stack == []
        assert len(fresh.nodes) == 1 and fresh.nodes[0]["description"] == "学习因式分解"


def test_goal_state_survives_reload_and_does_not_touch_mastery(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        state = add_current(db, row, ensure_session_goals(db, row))
        identifier, version = state.active_goal_id, state.version
    with database() as db:
        state = db.get(SessionGoalState, "mine")
        assert state.active_goal_id == identifier == "expand" and state.version == version == 1
        assert db.scalars(select(StudentState)).all() == []
        assert db.get(LearningSession, "mine").pending_context["assisted"] is True


@pytest.mark.parametrize("change", [
    {"parent_id": "missing"},
    {"parent_id": "expand"},
    {"prerequisites": ["expand"]},
    {"prerequisites": ["missing"]},
    {"source_ref": "message:foreign-message"},
    {"evidence_refs": ["attempt:missing"]},
])
def test_invalid_graph_or_foreign_source_is_rejected_atomically(database, change):
    with database() as db:
        row = db.get(LearningSession, "mine")
        goals = ensure_session_goals(db, row)
        before = public_goals(goals)
        with pytest.raises(ValueError):
            apply_goal_update(db, row, {"expected_version": 0, "nodes": [node("expand", goals.root_goal_id, **change)], "active_goal_id": "expand"})
        assert public_goals(goals) == before


def test_uncompleted_parent_cannot_be_used_as_prerequisite(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        goals = ensure_session_goals(db, row)
        with pytest.raises(ValueError, match="已完成的前置条件"):
            apply_goal_update(db, row, {"expected_version": 0, "nodes": [
                node("expand", goals.root_goal_id, prerequisites=[goals.root_goal_id]),
            ]})


def test_stale_version_cannot_overwrite_new_state(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        state = add_current(db, row, ensure_session_goals(db, row))
        with pytest.raises(GoalVersionConflict):
            apply_goal_update(db, row, {"expected_version": 0, "next_action": "过时动作"})
        assert state.next_action == "请写出去括号后的等式"


def test_remediation_push_and_resume_same_original_question(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        goals = add_current(db, row, ensure_session_goals(db, row))
        original = deepcopy(row.pending_context)
        apply_goal_update(db, row, {
            "expected_version": goals.version, "nodes": [node("sign", "expand", description="补负数乘法")],
            "operation": "push_remediation", "remediation_goal_id": "sign",
        })
        assert goals.active_goal_id == "sign"
        assert goals.return_stack[0]["goal_id"] == "expand"
        assert goals.return_stack[0]["pending_question_id"] == "math:q1"
        assert goals.return_stack[0]["current_step"] == "去括号"
        assert "answer" not in str(goals.return_stack)
        with pytest.raises(ValueError, match="补救未结束"):
            apply_goal_update(db, row, {"expected_version": goals.version, "operation": "resume"})
        row.pending_context = {**row.pending_context, "learning_progress": {"current_step": "计算负数乘法", "next_step": "补救活动"}}
        remedial = next(value for value in goals.nodes if value["goal_id"] == "sign")
        apply_goal_update(db, row, {"expected_version": goals.version, "nodes": [{**remedial, "status": "completed"}], "operation": "resume"})
        assert goals.active_goal_id == "expand" and goals.return_stack == []
        assert row.pending_context == original and row.pending_question_id == "math:q1"


def test_resume_rejects_changed_question_even_if_id_unchanged(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        goals = add_current(db, row, ensure_session_goals(db, row))
        apply_goal_update(db, row, {"expected_version": goals.version, "nodes": [node("sign", "expand")], "operation": "push_remediation", "remediation_goal_id": "sign"})
        row.pending_context = {**row.pending_context, "contract": {"id": "math:q1", "version": 2}}
        remedial = next(value for value in goals.nodes if value["goal_id"] == "sign")
        with pytest.raises(ValueError, match="原题或待答契约已变化"):
            apply_goal_update(db, row, {"expected_version": goals.version, "nodes": [{**remedial, "status": "completed"}], "operation": "resume"})
        assert goals.active_goal_id == "sign" and len(goals.return_stack) == 1


def test_root_cannot_be_rewritten_or_completed_with_pending_question(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        goals = ensure_session_goals(db, row)
        root = goals.nodes[0]
        for modification in ({"description": "偷偷换成另一目标"}, {"status": "completed"}):
            with pytest.raises(ValueError):
                apply_goal_update(db, row, {"expected_version": goals.version, "nodes": [{**root, **modification}]})


def test_planner_save_requires_current_request_and_commits_goals_with_progress(database, monkeypatch):
    monkeypatch.setattr("app.application.learning.planning.db_session", database)
    plan = Plan(action="respond", concept_ids=[], message="继续原题", steps=[], rationale="测试", continuation={
        "status": "awaiting_student", "current_step": "去括号", "next_step": "回原题写下一行", "completed_steps": [],
    }).model_dump()
    LearningPlanner.save("mine", plan, "继续", turn_id="turn-1")
    with database() as db:
        goals = db.get(SessionGoalState, "mine")
        assert goals.nodes[0]["status"] == "awaiting_student"
        assert goals.next_action == "回原题写下一行"
        before_id = goals.root_goal_id
        assert db.get(LearningSession, "mine").pending_context["learning_progress"]["next_step"] == goals.next_action
    invalid = {**plan, "goal_update": "学习因式分解", "goal_update_request": "换个目标"}
    with pytest.raises(ValueError, match="当前用户"):
        LearningPlanner.save("mine", invalid, "继续原题", turn_id="turn-1")
    LearningPlanner.save("mine", invalid, "换个目标", turn_id="turn-1")
    with database() as db:
        goals = db.get(SessionGoalState, "mine")
        assert goals.root_goal_id != before_id
        assert next(node for node in goals.nodes if node["goal_id"] == before_id)["status"] == "cancelled"
        assert db.get(LearningSession, "mine").pending_question_id == "math:q1"


def test_workflow_adapter_tracks_probe_without_replacing_current_equation(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        kwargs = dict(workflow="tutoring", workspace_id="equation-1", description="解-2(x-3)=8", current_step="-2(x-3)=8", next_action="请回答负数乘法补问")
        goals = sync_workflow(db, row, **kwargs)
        origin_id = goals.active_goal_id
        sync_workflow(db, row, **kwargs, remediation={"key": "sign", "description": "计算-2乘-3"})
        remedial_id = goals.active_goal_id
        assert remedial_id != origin_id and len(goals.return_stack) == 1
        sync_workflow(db, row, **kwargs, remediation={"key": "sign", "description": "计算-2乘-3"})
        assert goals.active_goal_id == remedial_id and len(goals.return_stack) == 1
        sync_workflow(db, row, **kwargs)
        assert goals.active_goal_id == origin_id and goals.return_stack == []
        assert next(node for node in goals.nodes if node["goal_id"] == remedial_id)["status"] == "completed"
        assert row.pending_context["contract"]["stem"] == "-2(x-3)=8"
        assert db.scalars(select(StudentState)).all() == []


def test_projection_keeps_goal_ids_and_current_path(database):
    with database() as db:
        row = db.get(LearningSession, "mine")
        goals = add_current(db, row, ensure_session_goals(db, row))
        apply_goal_update(db, row, {"expected_version": goals.version, "nodes": [node("future", goals.root_goal_id)]})
        projected = context_projection(goals)
        assert {node["goal_id"] for node in projected["nodes"]} == {goals.root_goal_id, "expand"}
        assert projected["other_goal_index"][0]["goal_id"] == "future"
        assert projected["version"] == goals.version
