"""唯一学习执行生命周期：先领取执行权，再核对正式事实和原生等待栈。"""

from langgraph.types import Command
from app.infrastructure.persistence.database import db_session
from app.application.learning.graph import SessionRuntime
from app.application.learning.receipts import receive, receipt, formal_effects, abandon, input_row
from app.domains.memory.context import read_context, validate_manifest, invalidation_epoch
from app.application.learning.protocol import validate_event
from app.shared.execution import LearningConflict


async def execute(learning, session_id, event, turn_id, emit):
    event = validate_event(event)
    learning.sessions.get(session_id)
    learning.claim(session_id, turn_id)
    failed = True
    try:
        # claim 前的检查不能作为写入依据，当前等待点和回执必须在领取后重读。
        snapshot = learning.sessions.get(session_id)
        scope = learning.owned(session_id, turn_id)
        with db_session() as db:
            prior = receipt(db, session_id, event)
            if prior and prior["status"] == "abandoned":
                raise LearningConflict("该请求已被后续输入终止；原答与已提交结果保留，请刷新会话。")
            effects = formal_effects(db, session_id, event["request_id"]) if prior else []
            if prior and prior["status"] == "completed":
                failed = False
                return {**prior["outcome"], "replayed": True, "session": snapshot}
        config = {"configurable": {"thread_id": "learning:" + session_id}, "recursion_limit": 120, "metadata": {"tutor_durable": bool(learning.checkpointer)}}
        current = await learning.graph.aget_state(config) if learning.checkpointer else None
        values = current.values if current else {}
        same_execution = bool(prior and current and current.next and
            (values.get("input_event") or {}).get("request_id") == event["request_id"])
        committed_answer = False
        if prior and event["kind"] == "answer":
            committed_answer = bool(learning.assessment.existing(session_id, event["request_id"], event["answer"]))
        elif prior and event["kind"] == "batch_answer":
            from app.domains.assessment.schemas import BatchAnswerRequest
            committed_answer = bool(learning.batch_assessment.existing(session_id,
                BatchAnswerRequest(**{k:v for k,v in event.items() if k in BatchAnswerRequest.model_fields})))
        wait = snapshot.get("pending_interaction")
        supplied = event.get("wait_id")
        requires_wait = event["kind"] in {"answer", "batch_answer", "skip"} or bool(supplied) or bool(wait and event["kind"] in {"message", "followup", "clarify", "change_goal", "guidance_action"})
        if requires_wait and not same_execution and not committed_answer and not effects and (not wait or supplied != wait["wait_id"]):
            raise LearningConflict("等待点已过期，请刷新会话后提交。")
        if event["kind"] == "answer" and not committed_answer and (not wait or wait.get("question_id") != event["question_id"]):
            raise LearningConflict("提交题目与当前冻结题目不一致")
        if event["kind"] == "batch_answer" and not committed_answer and (not wait or wait.get("batch_id") != event["batch_id"]):
            raise LearningConflict("提交练习组与当前等待点不一致")
        if not prior and event["kind"] == "practice_existing":
            learning.catalog.question(event["question_id"], scope["course_id"])
        if not prior and event["kind"] in {"message", "followup", "clarify", "change_goal"} and event.get("concept_ids"):
            learning.catalog.resolve_concepts("", scope["course_id"], event["concept_ids"])
        old_event = values.get("input_event") or {}
        abandon_old = bool(current and current.next and current.next != ("await_student",)
                           and old_event.get("request_id") != event["request_id"])
        with db_session() as db:
            if abandon_old:
                abandon(db, session_id, turn_id, old_event)
            accepted = receive(db, session_id, turn_id, event)
            if accepted["status"] == "completed":
                failed = False
                return {**accepted["outcome"], "replayed": True, "session": snapshot}
            if event["kind"] in {"answer", "batch_answer"} and not committed_answer:
                from app.domains.assessment.evidence import submission_help_snapshot
                from app.domains.sessions.interactions import digest
                pending = scope["pending_context"]
                frozen = ({"batch": pending.get("exercise_batch")} if event["kind"] == "batch_answer" else
                          {"contract": pending.get("contract"), "help": submission_help_snapshot(pending)})
                row = input_row(db, session_id, event)
                before = row.metadata_json.get("_grading_input")
                fingerprint = digest(frozen)
                if before is not None and before != fingerprint:
                    raise LearningConflict("原始作答的题目或提交前帮助条件已变化，请使用新请求重新提交。")
                row.metadata_json = {**row.metadata_json, "_grading_input": fingerprint}
            epoch = invalidation_epoch(db, scope["student_id"])
            received = input_row(db, session_id, event)
            received.metadata_json = {**received.metadata_json, "_native_request": {**received.metadata_json["_native_request"], "memory_epoch": epoch}}
            manifest = read_context(db, scope["student_id"], scope["course_id"], scope["concept_ids"], purpose="grading")["memory_manifest"]
        ctx = SessionRuntime(learning, session_id, turn_id, emit, epoch)
        with db_session() as db:
            valid_memory = validate_manifest(db, values.get("memory_manifest", []))
        incompatible = values and (values.get("schema_version") != 2 or not valid_memory or values.get("memory_epoch", epoch) != epoch or values.get("goal_version", snapshot["session_goals"]["version"]) != snapshot["session_goals"]["version"])
        checkpoint_wait = values.get("pending_interaction")
        stale_wait = current and current.next == ("await_student",) and checkpoint_wait != wait
        completed_old = False
        if current and current.next and current.next != ("await_student",) and not same_execution and values.get("input_event"):
            with db_session() as db:
                previous = receipt(db, session_id, values["input_event"])
                completed_old = bool(previous and previous["status"] == "completed")
        if incompatible or stale_wait or completed_old or abandon_old:
            await learning.checkpointer.adelete_thread(config["configurable"]["thread_id"])
            current, values = None, {}
        if current and current.next:
            old_event = values.get("input_event") or {}
            if current.next == ("await_student",):
                graph_input = Command(resume=event)
            elif old_event.get("request_id") == event["request_id"]:
                graph_input = None
            else:
                raise LearningConflict("上一请求尚未完成，请先以原请求标识恢复。")
        else:
            graph_input = {"schema_version": 2, "workflow_version": "teaching-session-v2", "input_event": event,
                "messages": [], "result": {}, "prepared": None, "proposal": {},
                "pending_interaction": wait, "memory_manifest": manifest, "memory_epoch": epoch, "goal_version": snapshot["session_goals"]["version"]}
            # 普通增量由唯一 State 维护；已有线程不重建完整历史。
            if values:
                graph_input.pop("messages")
        result = await learning.graph.ainvoke(graph_input, config=config, context=ctx,
            **({"durability": "sync"} if learning.checkpointer else {}))
        failed = False
        return {**result.get("result", {}), "session": learning.sessions.get(session_id)}
    finally:
        learning.release(session_id, turn_id, failed=failed)
