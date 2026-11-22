"""习题编排只保留质量重试与并发边界，校验和正式写入仍由领域函数负责。"""

from dataclasses import dataclass, replace
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app.agent.tools.errors import ToolRejected


@dataclass
class PracticeRuntime:
    exercises: Any
    context: Any
    turn_id: str
    emit: Any
    batch_id: str = ""
    runtime_context: Any = None


class GenerationState(TypedDict, total=False):
    operation_id: str
    task_id: str
    input_hash: str
    question_id: str
    session_version: int
    cognitive_type: str
    assignment: dict
    attempt_number: int
    last_issue: str
    previous_attempt: dict | None
    draft_result: dict
    independent_result: dict
    contract: dict
    result: dict


def generation_graph():
    async def draft(state: GenerationState, config: RunnableConfig, runtime: Runtime[PracticeRuntime]):
        run = runtime.context
        return await run.exercises.draft_generation(state, run, config=config)

    async def blind_solve(state: GenerationState, config: RunnableConfig, runtime: Runtime[PracticeRuntime]):
        run = runtime.context
        return await run.exercises.solve_generation(state, run, config=config)

    async def review(state: GenerationState, config: RunnableConfig, runtime: Runtime[PracticeRuntime]):
        run = runtime.context
        return await run.exercises.review_generation(state, run, config=config)

    def after_draft(state):
        if state.get("contract"):
            return END
        if state.get("last_issue"):
            return retry(state)
        return "blind_solve"

    def retry(state):
        if state["attempt_number"] >= 2:
            raise ToolRejected("生成题在预算内未通过质量检查：" + state["last_issue"])
        return "draft"

    graph = StateGraph(GenerationState, context_schema=PracticeRuntime)
    graph.add_node("draft", draft)
    graph.add_node("blind_solve", blind_solve)
    graph.add_node("review", review)
    graph.add_edge(START, "draft")
    graph.add_conditional_edges("draft", after_draft, [END, "draft", "blind_solve"])
    graph.add_edge("blind_solve", "review")
    graph.add_conditional_edges(
        "review", lambda state: END if state.get("contract") else retry(state), [END, "draft"]
    )
    # 不另建 saver：作为子图时继承父级持久化，独立测试调用则只执行一次图。
    return graph.compile(checkpointer=None, name="practice_generation")


def merge_results(left, right):
    return {**left, **right}


class BatchState(TypedDict, total=False):
    tasks: list[dict]
    results: Annotated[dict, merge_results]


def batch_generation_graph():
    async def generate(state: dict, config: RunnableConfig, runtime: Runtime[PracticeRuntime]):
        run = runtime.context
        context = replace(
            run.context,
            concept_ids=list(state["concept_ids"]),
            excluded_question_ids=list(run.context.excluded_question_ids),
            explanation_exercise_ids=list(run.context.explanation_exercise_ids),
        )
        await run.emit("batch_item_started", {"batch_id": run.batch_id, "index": state["index"]})
        contract, result = await run.exercises.generate(
            context,
            run.turn_id,
            state["cognitive_type"],
            run.emit,
            assignment=state["assignment"],
            operation_id=state["operation_id"],
            task_id=state["task_id"],
            config=config,
            runtime_context=run.runtime_context,
        )
        if contract.get("course_id") != context.course_id or not set(
            contract["concept_ids"]
        ).issubset(state["concept_ids"]):
            raise ValueError("生成结果超出子任务范围")
        await run.emit("batch_item_completed", {"batch_id": run.batch_id, "index": state["index"]})
        return {"results": {state["task_id"]: {"contract": contract, "result": result}}}

    graph = StateGraph(BatchState, context_schema=PracticeRuntime)
    graph.add_node("generate", generate)
    graph.add_conditional_edges(START, lambda state: [Send("generate", task) for task in state["tasks"]])
    graph.add_edge("generate", END)
    return graph.compile(checkpointer=None, name="practice_batch")
