"""正式事实与 SQLite 检查点的边缘恢复：拒绝旧输入，避免重复发布。"""

import asyncio
from copy import deepcopy

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.application.learning.service import LearningService
from app.application.learning.receipts import operation_id
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.assessment.models import Attempt
from app.domains.courses.models import Course, Question
from app.domains.learning.schemas import MessageRequest
from app.domains.sessions.models import LearningSession, Message
from app.domains.memory.scopes.dependencies import ensure_scope_state
from app.infrastructure.persistence.database import db_session
from test_native_session_graph import session_database, ScriptedRunner, pose, emit, answer_body
from test_guided_teaching import teaching, output, PRIVATE_REFERENCE
from test_exercise_context import exercise_database
from test_exercise_batches import batch_database, service, submission
from test_agent_autonomy import learning_with, final
from test_learning_delegation import delegated, lesson, continuation


def revoke_student_memory(database, sid):
    with database() as db:
        student = db.get(LearningSession, sid).student_id
        state = ensure_scope_state(db, student, "student")
        state.invalidation_version += 1


def test_failed_request_can_be_abandoned_but_its_key_cannot_run_again(session_database, tmp_path):
    class FailingRunner(ScriptedRunner):
        fail = True

        async def run(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeError("模型请求失败，原始输入已收到")
            return await super().run(*args, **kwargs)

    async def scenario():
        runner = FailingRunner()
        original = MessageRequest(content="保留这次失败输入的原话。", request_id="failed-original")
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "abandon.sqlite")) as saver:
            learning = LearningService(runner, checkpointer=saver)
            with pytest.raises(RuntimeError, match="原始输入"):
                await learning.turn("session", original, "failed-turn", emit)
            result = await learning.turn("session", MessageRequest(content="请继续当前学习。",
                request_id="new-after-failure"), "new-turn", emit)
            assert result["message"]["agent"] == "supervisor"
            before = len(runner.calls)
            with pytest.raises(ValueError, match="终止"):
                await learning.turn("session", original, "obsolete-retry", emit)
            assert len(runner.calls) == before
        with session_database() as db:
            row = db.get(Message, operation_id("session", original.request_id, "input"))
            assert row.content == original.content
            assert row.metadata_json["_native_request"]["status"] == "abandoned"

    asyncio.run(scenario())


def test_invalid_cross_course_question_does_not_block_next_input(session_database, tmp_path):
    with session_database() as db:
        db.add(Course(id="mechanics", title="力学", description="隔离课程", source_ref="fixture"))
        db.add(Question(id="mechanics:foreign", course_id="mechanics", source_id="foreign",
            stem="跨课程题目不能进入数学会话。", options={}, answer=[], question_type="open",
            concept_ids=[], explanation="", source_ref="fixture"))

    async def scenario():
        runner = ScriptedRunner()
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "invalid.sqlite")) as saver:
            learning = LearningService(runner, checkpointer=saver)
            with pytest.raises(ValueError):
                await learning.execute("session", {"kind": "practice_existing", "question_id": "mechanics:foreign",
                    "request_id": "foreign-practice"}, "foreign-turn", emit)
            with session_database() as db:
                assert db.get(Message, operation_id("session", "foreign-practice", "input")) is None
            result = await learning.turn("session", MessageRequest(content="继续学习数学。",
                request_id="valid-after-foreign"), "valid-turn", emit)
            assert result["message"]["agent"] == "supervisor"

    asyncio.run(scenario())


def test_missing_checkpoint_cannot_reinterpret_answer_after_help_changed(session_database, tmp_path):
    class FailingAssessment(ScriptedRunner):
        fail = True

        async def run(self, role, *args, **kwargs):
            if role == "assessment" and self.fail:
                self.fail = False
                raise RuntimeError("评分前失败")
            return await super().run(role, *args, **kwargs)

    async def scenario():
        path = str(tmp_path / "grading.sqlite")
        runner = FailingAssessment()
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)
            await pose(learning, session_database)
            body = answer_body(learning, "original-raw-answer").model_copy(update={"answer": "  A\n原答片段  "})
            with pytest.raises(RuntimeError, match="评分前失败"):
                await learning.answer("session", body, "first-answer", emit)
            with session_database() as db:
                stored = db.get(Message, operation_id("session", body.request_id, "input"))
                original_hash = stored.metadata_json["_grading_input"]
                row = db.get(LearningSession, "session")
                row.pending_context = {**deepcopy(row.pending_context), "assisted": True}
                row.version += 1
            await saver.adelete_thread("learning:session")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            calls = len(runner.calls)
            with pytest.raises(ValueError, match="提交前帮助条件"):
                await restored.answer("session", body, "after-help", emit)
            assert len(runner.calls) == calls
        with session_database() as db:
            stored = db.get(Message, operation_id("session", body.request_id, "input"))
            assert stored.content == body.answer and stored.metadata_json["_grading_input"] == original_hash
            assert not list(db.scalars(select(Attempt)))

    asyncio.run(scenario())


@pytest.mark.parametrize("entry_kind", ["guidance_action", "practice_existing"])
def test_guidance_effect_replays_after_finish_failure_and_checkpoint_loss(teaching, tmp_path, monkeypatch, entry_kind):
    _, runner, sid = teaching
    body = {"kind": entry_kind, "request_id": "guidance-effect-recovery"}
    if entry_kind == "guidance_action":
        body.update(action="start", problem={"stem": "每本3元，运费6元，共42元，买了几本？", "source_kind": "pasted"})
    else:
        body["question_id"] = "junior_algebra:worked-example"

    async def scenario():
        path = str(tmp_path / (entry_kind + ".sqlite"))
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)

            def fail_before_native_receipt(*args, **kwargs):
                raise RuntimeError("讲题已正式发布，完成回执前失败")

            monkeypatch.setattr(learning, "finish", fail_before_native_receipt)
            with pytest.raises(RuntimeError, match="完成回执前"):
                await learning.execute(sid, body, "guidance-before-failure", emit)
            published = learning.sessions.get(sid)["guidance"]
            assert published["turn_count"] == 1
            await saver.adelete_thread("learning:" + sid)
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            calls = len(runner.calls)
            replay = await restored.execute(sid, body, "guidance-restored", emit)
            assert replay["replayed"] and replay["guidance"] == published
            assert len(runner.calls) == calls
            assert PRIVATE_REFERENCE not in str(replay)
        with db_session() as db:
            responses = [row for row in db.scalars(select(Message).where(Message.session_id == sid))
                if row.metadata_json.get("_guidance_receipt", {}).get("request_id") == body["request_id"]]
            assert len(responses) == 1

    asyncio.run(scenario())


def test_optional_guidance_has_version_token_without_required_interrupt(teaching, tmp_path):
    _, runner, sid = teaching
    runner.responses = [output(action="explain", question="", response_expectation="optional", help_level=1),
        output(action="explain", question="", response_expectation="none", analysis=None, help_level=1)]

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "optional.sqlite")) as saver:
            learning = LearningService(runner, checkpointer=saver)
            first = await learning.execute(sid, {"kind": "guidance_action", "action": "start",
                "problem": {"stem": "每本3元，运费6元，共42元，买了几本？"}, "request_id": "optional-first"}, "optional-a", emit)
            wait = first["session"]["pending_interaction"]
            assert wait["requires_response"] is False
            state = await learning.graph.aget_state({"configurable": {"thread_id": "learning:" + sid}})
            assert not state.next and not state.interrupts
            second = await learning.execute(sid, {"kind": "guidance_action", "action": "reply", "text": "请再解释固定费用。",
                "wait_id": wait["wait_id"], "expected_version": first["guidance"]["version"], "request_id": "optional-second"}, "optional-b", emit)
            assert second["session"]["pending_interaction"]["wait_id"] != wait["wait_id"]
            calls = len(runner.calls)
            with pytest.raises(ValueError, match="等待点"):
                await learning.execute(sid, {"kind": "guidance_action", "action": "reply", "text": "旧页面原答不能套到新工作区。",
                    "wait_id": wait["wait_id"], "expected_version": first["guidance"]["version"], "request_id": "optional-stale"}, "optional-c", emit)
            assert len(runner.calls) == calls

    asyncio.run(scenario())


def test_batch_input_and_feedback_have_one_formal_message_each(batch_database, tmp_path):
    configured, model = service()

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "batch.sqlite")) as saver:
            learning = LearningService(configured.runner, checkpointer=saver)
            generated = await learning.turn("mine", MessageRequest(content="安排一组巩固练习。",
                request_id="edge-batch-generation"), "generate-edge", emit)
            body = submission(generated["exercise_batch"], key="edge-batch-answer")
            result = await learning.answer_batch("mine", body, "answer-edge", emit)
            calls = len(model.calls)
            assert (await learning.answer_batch("mine", body, "answer-replay", emit))["replayed"]
            assert len(model.calls) == calls and result["total"] == 3
            public_input = next(message for message in learning.sessions.get("mine")["messages"]
                if message["role"] == "user" and message["metadata"].get("kind") == "batch_answer")
            assert public_input["metadata"]["batch_id"] == body.batch_id
            assert public_input["metadata"]["answers"] == {item.question_id: item.answer for item in body.answers}
        with batch_database() as db:
            messages = list(db.scalars(select(Message).where(Message.session_id == "mine")))
            users = [row for row in messages if row.role == "user" and row.metadata_json.get("kind") == "batch_answer"]
            feedback = [row for row in messages if row.role == "assistant" and row.metadata_json.get("kind") == "batch_assessment"]
            assert len(users) == len(feedback) == 1
            assert users[0].metadata_json["_native_request"]["event"]["answers"] == body.model_dump()["answers"]

    asyncio.run(scenario())


def test_revocation_after_supervisor_return_blocks_publication_and_rebuilds_candidate(session_database, tmp_path, monkeypatch):
    class CandidateRunner(ScriptedRunner):
        async def run(self, role, *args, **kwargs):
            result = await super().run(role, *args, **kwargs)
            text = "旧候选不可发布" if len(self.calls) == 1 else "新候选已重新核对"
            result["output"]["message"] = text
            result["messages"] = [*(kwargs.get("messages") or []), AIMessage(content=text, id="candidate-" + str(len(self.calls)))]
            return result

    async def scenario():
        runner = CandidateRunner()
        path = str(tmp_path / "supervisor-revocation.sqlite")
        body = MessageRequest(content="解释这个计数问题。", request_id="revoke-before-publish")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)
            original = learning.orchestrate

            async def revoke_after_model(*args, **kwargs):
                result = await original(*args, **kwargs)
                revoke_student_memory(session_database, "session")
                return result

            monkeypatch.setattr(learning, "orchestrate", revoke_after_model)
            with pytest.raises(ValueError, match="撤销"):
                await learning.turn("session", body, "revoked-first", emit)
            with session_database() as db:
                assert db.get(Message, operation_id("session", body.request_id, "response")) is None
            state = await learning.graph.aget_state({"configurable": {"thread_id": "learning:session"}})
            assert "旧候选不可发布" in state.values["proposal"]["content"]
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            result = await restored.turn("session", body, "revoked-retry", emit)
            assert runner.calls == ["supervisor", "supervisor"]
            assert "新候选已重新核对" in result["message"]["content"]
            assert "旧候选不可发布" not in str(result)
            state = await restored.graph.aget_state({"configurable": {"thread_id": "learning:session"}})
            assert "旧候选不可发布" not in str(state.values["messages"])

    asyncio.run(scenario())


def test_revocation_after_guidance_model_return_rejects_old_publication(teaching, tmp_path, monkeypatch):
    _, runner, sid = teaching
    runner.responses = [output(content="旧讲题候选应被撤回，先区分固定费用。"),
        output(content="新讲题候选已经重新核对，先找出固定费用。")]
    original = runner.run
    revoked = False

    async def revoke_after_model(*args, **kwargs):
        nonlocal revoked
        result = await original(*args, **kwargs)
        if not revoked:
            revoked = True
            revoke_student_memory(db_session, sid)
        return result

    monkeypatch.setattr(runner, "run", revoke_after_model)

    async def scenario():
        path = str(tmp_path / "guidance-revocation.sqlite")
        body = {"kind": "guidance_action", "action": "start", "request_id": "revoke-guidance-publication",
            "problem": {"stem": "每本3元，运费6元，共42元，买了几本？"}}
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(runner, checkpointer=saver)
            with pytest.raises(ValueError, match="撤销"):
                await learning.execute(sid, body, "guidance-revoked-first", emit)
            with db_session() as db:
                assert db.get(Message, operation_id(sid, body["request_id"], "guidance_response")) is None
            assert learning.sessions.get(sid)["guidance"] is None
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            restored = LearningService(runner, checkpointer=saver)
            result = await restored.execute(sid, body, "guidance-revoked-retry", emit)
            assert len(runner.calls) == 2
            assert "新讲题候选" in result["message"]["content"]
            assert "旧讲题候选" not in str(result) and PRIVATE_REFERENCE not in str(result)

    asyncio.run(scenario())


def test_delegation_receipt_survives_new_epoch_and_lost_parent_checkpoint(exercise_database, tmp_path):
    old_lesson = {**lesson(), "title": "撤销前候选"}
    new_lesson = {**lesson(), "title": "撤销后重建候选"}
    configured, model = learning_with([delegated("call_knowledge_agent", task="说明限制条件"), old_lesson])
    body = MessageRequest(content="请说明计数的限制条件。", request_id="epoch-delegation-recovery")

    async def scenario():
        path = str(tmp_path / "delegation-epoch.sqlite")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(configured.runner, checkpointer=saver)
            with pytest.raises(RuntimeError, match="StopIteration"):
                await learning.turn("mine", body, "epoch-zero", emit)
            revoke_student_memory(exercise_database, "mine")
        # 同一正式操作在新撤销版本重建私有候选，父模型再次失败。
        model.responses = iter([delegated("call_knowledge_agent", task="说明限制条件"), new_lesson])
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(configured.runner, checkpointer=saver)
            with pytest.raises(RuntimeError, match="StopIteration"):
                await learning.turn("mine", body, "epoch-one", emit)
            await saver.adelete_thread("learning:mine")
        # 检查点丢失后仅回放当前版本子任务，不再调用知识 Agent。
        model.responses = iter([final("respond", ["math:c1"], continuation=continuation())])
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            learning = LearningService(configured.runner, checkpointer=saver)
            result = await learning.turn("mine", body, "epoch-one-recovered", emit)
            assert result["lesson"]["title"] == new_lesson["title"]
            assert old_lesson["title"] not in str(result)
        with exercise_database() as db:
            receipts = [row for row in db.scalars(select(Message).where(Message.session_id == "mine"))
                if row.metadata_json.get("_delegation_receipt")]
            assert len(receipts) == 2 and len({row.id for row in receipts}) == 2
            versions = {row.metadata_json["_delegation_receipt"]["memory_epoch"]:
                row.metadata_json["_delegation_receipt"]["outcome"]["result"] for row in receipts}
            assert all(versions[0]["lesson"][key] == value for key, value in old_lesson.items())
            assert all(versions[1]["lesson"][key] == value for key, value in new_lesson.items())
            assert versions[0]["child_run_ids"] != versions[1]["child_run_ids"]
            runs = list(db.scalars(select(AgentRun).where(AgentRun.agent == "knowledge")))
            assert len(runs) == 2

    asyncio.run(scenario())


def test_revocation_after_child_return_cannot_save_delegation_candidate(exercise_database, tmp_path, monkeypatch):
    configured, _ = learning_with([delegated("call_knowledge_agent", task="说明限制条件"), lesson(),
        final("respond", ["math:c1"], continuation=continuation())])
    original = configured.runner.run

    async def revoke_after_child(role, *args, **kwargs):
        result = await original(role, *args, **kwargs)
        if role == "knowledge":
            revoke_student_memory(exercise_database, "mine")
        return result

    monkeypatch.setattr(configured.runner, "run", revoke_after_child)

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "child-revocation.sqlite")) as saver:
            learning = LearningService(configured.runner, checkpointer=saver)
            with pytest.raises(ValueError, match="撤销"):
                await learning.turn("mine", MessageRequest(content="说明计数限制条件。",
                    request_id="revoked-child-receipt"), "revoked-child", emit)
        with exercise_database() as db:
            assert not [row for row in db.scalars(select(Message).where(Message.session_id == "mine"))
                if row.metadata_json.get("_delegation_receipt")]
            assert db.get(Message, operation_id("mine", "revoked-child-receipt", "response")) is None

    asyncio.run(scenario())
