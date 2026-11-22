"""模型完成与正式评分之间发生撤销时，候选必须重新核对。"""

import asyncio

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import select

from app.application.learning.service import LearningService
from app.agent.tools.contracts import ToolContext
from app.domains.assessment.models import Attempt
from app.domains.courses.catalog import question_contract
from app.domains.courses.models import Question
from app.domains.memory.scopes.dependencies import ensure_scope_state
from app.domains.sessions.models import Message
from app.shared.execution import LearningTurnLost
from test_native_session_graph import session_database, ScriptedRunner, pose, emit, answer_body


def test_assessment_candidate_cannot_commit_after_memory_revocation(session_database, tmp_path):
    class RevokingRunner(ScriptedRunner):
        revoked = False

        def __init__(self):
            super().__init__()
            self.request_ids = []

        async def run(self, role, skill, payload, schema, context, *args, **kwargs):
            self.request_ids.append((role, context.request_id))
            result = await super().run(role, skill, payload, schema, context, *args, **kwargs)
            if role == "assessment" and not self.revoked:
                self.revoked = True
                with session_database() as db:
                    ensure_scope_state(db, "local", "student").invalidation_version += 1
            return result

    async def scenario():
        runner = RevokingRunner()
        path = str(tmp_path / "revocation.sqlite")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)
            await pose(learning, session_database)
            body = answer_body(learning, "assessment-revoked")
            with pytest.raises(ValueError, match="记忆已撤销"):
                await learning.answer("session", body, "before-revocation", emit)
            with session_database() as db:
                assert not list(db.scalars(select(Attempt)))
            assert learning.wait("session")["question_id"] == body.question_id
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            result = await restored.answer("session", body, "after-revocation", emit)
            assert result["result"] == "correct"
            assert runner.calls.count("assessment") == 2
            assert all(identifier == body.request_id for _, identifier in runner.request_ids)
            with session_database() as db:
                assert len(list(db.scalars(select(Attempt)))) == 1

    asyncio.run(scenario())


def test_missing_owner_cannot_publish_frozen_question(session_database):
    async def scenario():
        learning = LearningService(ScriptedRunner())
        with session_database() as db:
            contract = question_contract(db.get(Question, "math:q"))
        with pytest.raises(LearningTurnLost):
            await learning.pose_contract("session", contract,
                ToolContext(course_id="math", session_id="session", student_id="local", concept_ids=["math:c"],
                    request_id="missing-owner"), None, "recall", emit)
        with session_database() as db:
            assert not list(db.scalars(select(Message)))

    asyncio.run(scenario())
