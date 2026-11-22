"""学习进度仅依赖会话事务；不调用模型，不连接现有学生数据库。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.domains.sessions.models import LearningSession
from app.domains.learning.schemas import LearningContinuation, Plan
from app.domains.sessions.progress import preserve_learning_context
from app.domains.learning.progress import save_progress


def continuation(**overrides):
    return {
        "status": "awaiting_student",
        "current_step": "通过例子理解应力",
        "next_step": "独立完成一道练习，再根据作答确定是否需要补充讲解。",
        "completed_steps": ["完成定义讲解"],
        **overrides,
    }


def session_row(*, goal="学习单向应力", pending=None):
    return LearningSession(
        id="progress-session",
        course_id="mechanics",
        student_id="evaluation_progress",
        title="进度隔离测试",
        goal=goal,
        pending_context=deepcopy(pending or {}),
    )


def test_preservation_copies_formal_progress_and_drops_retired_execution_cache():
    context = {
        "learning_progress": {"objective": "原目标", "completed_steps": ["定义讲解"]},
        "delegation_checkpoint": {"completed_calls": [{"id": "child-1"}]},
        "contract": {"answer": ["A"]},
        "assisted": True,
        "decision": {"question": "旧澄清"},
    }
    preserved = preserve_learning_context(context)
    assert set(preserved) == {"learning_progress"}
    preserved["learning_progress"]["completed_steps"].append("修改副本")
    assert context["learning_progress"]["completed_steps"] == ["定义讲解"]
    assert context["delegation_checkpoint"]["completed_calls"][0]["id"] == "child-1"
    assert preserve_learning_context(None) == {}


def test_progress_merges_execution_steps_and_preserves_current_question():
    row = session_row(
        pending={
            "learning_progress": {
                "objective": "学习单向应力",
                "completed_steps": ["完成定义讲解"],
            },
            "contract": {"id": "mechanics:q1", "answer": ["A"]},
        }
    )
    first = save_progress(
        row, continuation(completed_steps=["完成定义讲解", "完成单位换算示例"])
    )
    second = save_progress(
        row,
        continuation(
            status="completed",
            current_step="本阶段讲解完成",
            next_step="等待选择后续目标。",
            completed_steps=["完成单位换算示例", "布置练习"],
        ),
    )
    assert first["completed_steps"] == ["完成定义讲解", "完成单位换算示例"]
    assert second["completed_steps"] == ["完成定义讲解", "完成单位换算示例", "布置练习"]
    assert second["status"] == "completed"
    assert row.pending_context["contract"]["answer"] == ["A"]
    assert set(preserve_learning_context(row.pending_context)) == {"learning_progress"}
    assert "mastery" not in second and "evidence_ids" not in second
    second["completed_steps"].append("修改返回副本")
    assert (
        "修改返回副本"
        not in row.pending_context["learning_progress"]["completed_steps"]
    )


def test_changed_objective_resets_old_completed_steps():
    row = session_row(
        goal="学习应变",
        pending={
            "learning_progress": {
                "objective": "学习单向应力",
                "completed_steps": ["旧目标定义讲解", "旧目标练习"],
            }
        },
    )
    progress = save_progress(row, continuation(completed_steps=["应变定义讲解"]))
    assert progress["objective"] == "学习应变"
    assert progress["completed_steps"] == ["应变定义讲解"]


def test_completed_history_is_deduplicated_and_bounded_to_last_twenty_four():
    old = [f"执行步骤{index}" for index in range(24)]
    row = session_row(
        pending={
            "learning_progress": {"objective": "学习单向应力", "completed_steps": old}
        }
    )
    progress = save_progress(
        row, continuation(completed_steps=["执行步骤23", "执行步骤24", "执行步骤25"])
    )
    assert progress["completed_steps"] == [f"执行步骤{index}" for index in range(2, 26)]


def test_legacy_plan_and_none_progress_remain_compatible():
    plan = Plan.model_validate(
        {
            "action": "respond",
            "concept_ids": [],
            "message": "可以继续学习。",
            "steps": [],
            "rationale": "普通沟通",
        }
    )
    assert plan.continuation is None
    row = session_row(pending={"contract": {"id": "mechanics:q1"}})
    before = deepcopy(row.pending_context)
    assert save_progress(row, None) is None and row.pending_context == before


@pytest.mark.parametrize(
    "change",
    [
        {"status": "mastered"},
        {"current_step": ""},
        {"current_step": "步" * 241},
        {"next_step": ""},
        {"next_step": "步" * 501},
        {"completed_steps": ["完成"] * 7},
        {"completed_steps": ["步" * 241]},
        {"mastery": 1.0},
    ],
)
def test_invalid_progress_is_rejected_before_mutating_session(change):
    row = session_row(pending={"contract": {"id": "mechanics:q1"}})
    before = deepcopy(row.pending_context)
    with pytest.raises(ValidationError):
        save_progress(row, continuation(**change))
    assert row.pending_context == before


def test_progress_is_durable_in_callers_transaction():
    engine = create_engine("sqlite://")
    LearningSession.__table__.create(engine)
    with Session(engine) as db:
        row = session_row(pending={"contract": {"id": "mechanics:q1", "answer": ["A"]}})
        db.add(row)
        db.commit()
        save_progress(row, continuation())
        db.commit()
        db.expire_all()
        restored = db.get(LearningSession, "progress-session")
        assert (
            restored.pending_context["learning_progress"]["next_step"]
            == continuation()["next_step"]
        )
        assert restored.pending_context["contract"]["answer"] == ["A"]
        assert set(preserve_learning_context(restored.pending_context)) == {"learning_progress"}
    engine.dispose()
