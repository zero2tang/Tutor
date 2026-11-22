"""会话业务图：节点只承担分支、昂贵结果保存或正式提交恢复边界。"""

from dataclasses import dataclass
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Overwrite, interrupt
from langgraph.runtime import Runtime
from langchain_core.runnables import RunnableConfig

from app.domains.assessment.schemas import BatchAnswerRequest
from app.domains.guidance.schemas import GuidanceRequest
from app.domains.learning.schemas import MessageRequest


class TeachingState(TypedDict, total=False):
    schema_version: int
    workflow_version: str
    messages: Annotated[list, add_messages]
    input_event: dict
    prepared: dict | None
    result: dict
    proposal: dict
    pending_interaction: dict | None
    memory_manifest: list
    goal_version: int
    memory_epoch: int


@dataclass
class SessionRuntime:
    learning: object
    session_id: str
    turn_id: str
    emit: object
    memory_epoch: int = 0


def public_summary(value):
    if isinstance(value, dict):
        return {k: public_summary(v) for k, v in value.items() if k != "answer_key" and not k.startswith("_")}
    if isinstance(value, list):
        return [public_summary(v) for v in value]
    return value


def build_session_graph(checkpointer=None):
    async def supervisor(state, config: RunnableConfig, runtime: Runtime[SessionRuntime]):
        ctx = runtime.context
        learning = ctx.learning
        scope = learning.owned(ctx.session_id, ctx.turn_id)
        scope["memory_epoch"] = ctx.memory_epoch
        event = state["input_event"]
        assessment = state.get("result") if event["kind"] in {"answer", "batch_answer"} else None
        fields = {key: value for key, value in event.items() if key in MessageRequest.model_fields}
        if assessment:
            fields["content"] = "本次真实作答已完成评估，请依据评估结果继续原学习目标，执行必要步骤或给出明确学生停点。"
        request = MessageRequest(**fields)
        proposal = await learning.orchestrate(ctx.session_id, scope, learning.sessions.get(ctx.session_id),
            request, ctx.turn_id, ctx.emit, publish=False, config=config, messages=state.get("messages"),
            extra_payload={"assessment_result": public_summary(assessment)} if assessment else None,
            metadata_extra={"continuation_for": event["request_id"]} if assessment else None)
        messages = proposal.pop("messages", state.get("messages", []))
        return {"proposal": proposal, "messages": Overwrite(messages)}

    async def prepare_assessment(state, runtime: Runtime[SessionRuntime]):
        ctx = runtime.context
        ctx.learning.owned(ctx.session_id, ctx.turn_id)
        event = state["input_event"]
        if event["kind"] == "batch_answer":
            request = BatchAnswerRequest(**{k: v for k, v in event.items() if k in BatchAnswerRequest.model_fields})
            prepared = await ctx.learning.batch_assessment.prepare(ctx.session_id, request, ctx.turn_id, ctx.emit)
        else:
            prepared = await ctx.learning.assessment.prepare(ctx.session_id, event["answer"], event["request_id"], ctx.turn_id, ctx.emit)
        return {"prepared": {**prepared, "memory_epoch": ctx.memory_epoch}}

    async def commit_assessment(state, runtime: Runtime[SessionRuntime]):
        ctx, event = runtime.context, state["input_event"]
        ctx.learning.owned(ctx.session_id, ctx.turn_id)
        if event["kind"] == "batch_answer":
            request = BatchAnswerRequest(**{k: v for k, v in event.items() if k in BatchAnswerRequest.model_fields})
            result = await ctx.learning.batch_assessment.commit(ctx.session_id, request, ctx.turn_id, ctx.emit, state["prepared"], publish=False)
        else:
            result = await ctx.learning.assessment.commit(ctx.session_id, event["answer"], event["request_id"], ctx.turn_id, ctx.emit, state["prepared"], publish=False)
        await ctx.learning.publish_feedback(ctx, event, result)
        return {"result": result, "prepared": None}

    async def guidance(state, config: RunnableConfig, runtime: Runtime[SessionRuntime]):
        ctx, event = runtime.context, state["input_event"]
        data = {k: v for k, v in event.items() if k in GuidanceRequest.model_fields}
        if event["kind"] in {"message", "followup", "clarify", "change_goal"}:
            data.update(action="reply", text=event["content"])
        result = await ctx.learning.guidance.act_owned(ctx.session_id, GuidanceRequest(**data), ctx.turn_id, ctx.emit, config=config)
        result = ctx.learning.finish(ctx.session_id, ctx.turn_id, event, result,
                                     [result["message"]["id"]])
        return {"result": result, "messages": ctx.learning.guidance_messages(ctx.session_id, event, result), "pending_interaction": ctx.learning.wait(ctx.session_id), "memory_manifest": ctx.learning.memory_manifest(ctx.session_id), "goal_version": ctx.learning.sessions.get(ctx.session_id)["session_goals"]["version"]}

    async def publish(state, runtime: Runtime[SessionRuntime]):
        ctx = runtime.context
        result = await ctx.learning.publish_proposal(ctx, state["input_event"], state["proposal"], state.get("result"))
        return {"result": result, "proposal": {}, "pending_interaction": ctx.learning.wait(ctx.session_id), "memory_manifest": ctx.learning.memory_manifest(ctx.session_id), "goal_version": ctx.learning.sessions.get(ctx.session_id)["session_goals"]["version"]}

    async def apply_action(state, config: RunnableConfig, runtime: Runtime[SessionRuntime]):
        ctx = runtime.context
        result = await ctx.learning.apply_action(ctx, state["input_event"], config=config)
        return {"result": result, "messages": ctx.learning.guidance_messages(ctx.session_id, state["input_event"], result), "pending_interaction": ctx.learning.wait(ctx.session_id), "memory_manifest": ctx.learning.memory_manifest(ctx.session_id), "goal_version": ctx.learning.sessions.get(ctx.session_id)["session_goals"]["version"]}

    def await_student(state):
        event = interrupt(state["pending_interaction"])
        return {"input_event": event, "prepared": None, "proposal": {}, "result": {}}

    def route(state, runtime: Runtime[SessionRuntime]):
        ctx, event = runtime.context, state["input_event"]
        if event["kind"] in {"answer", "batch_answer"}:
            return "prepare_assessment"
        if event["kind"] == "guidance_action" or (event["kind"] in {"message", "followup", "clarify", "change_goal"} and ctx.learning.guidance.active(ctx.learning.sessions.get(ctx.session_id))):
            return "guidance"
        return "apply_action" if event["kind"] in {"practice_existing", "skip"} else "supervisor"

    def stop(state):
        return "await_student" if state.get("pending_interaction") and state["pending_interaction"].get("requires_response", True) else END

    builder = StateGraph(TeachingState, context_schema=SessionRuntime)
    for name, handler in (("supervisor", supervisor), ("prepare_assessment", prepare_assessment),
                          ("commit_assessment", commit_assessment), ("guidance", guidance),
                          ("publish", publish), ("apply_action", apply_action), ("await_student", await_student)):
        builder.add_node(name, handler)
    routes = ["supervisor", "prepare_assessment", "guidance", "apply_action"]
    builder.add_conditional_edges(START, route, routes)
    builder.add_edge("prepare_assessment", "commit_assessment")
    builder.add_edge("commit_assessment", "supervisor")
    builder.add_edge("supervisor", "publish")
    for name in ("publish", "guidance", "apply_action"):
        builder.add_conditional_edges(name, stop, ["await_student", END])
    builder.add_conditional_edges("await_student", route, routes)
    return builder.compile(checkpointer=checkpointer)
