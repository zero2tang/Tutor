"""原生执行器测试夹具；预录制工具替身只在测试边界转换为真实工具。"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import json
from types import SimpleNamespace
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, convert_to_messages
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from pydantic import ConfigDict, Field, create_model

from app.agent.runtime.contracts import RuntimeContext, RunBudget
from app.application.agent.graph import create_role_agent
from app.application.agent.middleware import TeachingBoundaries
from app.agent.runtime.streaming import NativeTextEvents
from app.application.agent.structured import run_structured_model
from app.application.agent.images import QuestionImageContext
from app.application.agent.streaming import public_fields


@dataclass
class LoopState:
    trace: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})


class RecordedTools:
    """旧业务用例的录制回调不是生产注册表；实际调度仍经过 ToolNode。"""

    def __init__(self, source, names=()):
        self.source, self.names = source, names

    def for_role(self, role, *, handler=None):
        definitions = self.source.definitions(role) if hasattr(self.source, "definitions") else []
        by_name = {item["function"]["name"]: item["function"] for item in definitions}
        for name in self.names:
            by_name.setdefault(name, {"name": name, "parameters": {"properties": {"index": {"type": "integer"}}}})
        result = []
        for name, definition in by_name.items():
            properties = definition.get("parameters", {}).get("properties", {})
            required = definition.get("parameters", {}).get("required", [])
            fields = {}
            for key, prop in properties.items():
                kind = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}.get(prop.get("type"), Any)
                if prop.get("enum"):
                    kind = Literal[tuple(prop["enum"])]
                fields[key] = (kind, Field(default=... if key in required else None))
            fields["runtime"] = (ToolRuntime[RuntimeContext], ...)
            schema = create_model("Recorded_" + name, __config__=ConfigDict(arbitrary_types_allowed=True, extra="forbid"), **fields)

            def make_call(tool_name):
                async def call(runtime: ToolRuntime[RuntimeContext], **arguments):
                    arguments = {key: value for key, value in arguments.items() if value is not None}
                    if hasattr(self.source, "validate_call"):
                        self.source.validate_call(role, tool_name, arguments)
                    value, trace = await self.source.execute(role, tool_name, arguments, deepcopy(runtime.context.scope))
                    if trace.get("status") == "failed" and trace.get("error_kind", "execution_error") == "execution_error":
                        raise RuntimeError("录制工具返回依赖执行失败")
                    return value
                return call

            result.append(StructuredTool(name=name, description=definition.get("description") or "本地录制工具",
                                         args_schema=schema, coroutine=make_call(name)))
        return result


def native_tools(source, names=()):
    return source if hasattr(source, "for_role") else RecordedTools(source, names)


def runtime_context(context, role, state, *, emit=None, max_calls=24, failure_policy=None):
    budget = RunBudget(max_calls=max_calls)
    if failure_policy:
        budget.failures = failure_policy
    return RuntimeContext(scope=context, role=role, run_id="test-run", root_run_id="test-run", emit=emit,
                          budget=budget, usage=state.usage, trace=state.trace, citations=state.citations)


async def run_agent_loop(models, tools, role, messages, schema, context, state, **options):
    if not hasattr(models, "settings"):
        models.settings = SimpleNamespace(llm_model="recorded")
    if not hasattr(models, "limit"):
        models.limit = asyncio.Semaphore(3)
    runtime = options.pop("runtime_context", None) or runtime_context(context, role, state,
        emit=options.get("emit"), max_calls=options.get("max_calls", 24), failure_policy=options.get("failure_policy"))
    runtime.trace, runtime.citations, runtime.usage = state.trace, state.citations, state.usage
    fields = options.get("public_fields", public_fields(schema))
    image = options.get("image_context") or QuestionImageContext(getattr(context, "course_id", "mechanics"))
    wire = [{"role": "user", "content": item} if isinstance(item, str) else deepcopy(item) for item in messages]
    values = convert_to_messages(wire)
    runtime.image_context = image
    if not options.get("use_tools", True) or tools is None or not hasattr(tools, "execute") and not hasattr(tools, "for_role"):
        image.enrich(wire)
        values = convert_to_messages(wire)
        return await run_structured_model(models, values, schema, runtime, instruction="测试任务",
            max_tokens=options.get("max_tokens", 3000), public_fields=fields, validate_output=options.get("validate_output"))
    provider = native_tools(tools, ["lookup", "get_session_exercise"] if not hasattr(tools, "for_role") and not tools.definitions(role) else [])
    graph, events = create_role_agent(models, runtime, schema, "", tools=provider.for_role(role),
        max_rounds=options.get("max_rounds", 12), max_tokens=options.get("max_tokens", 3000), public_fields=fields,
        validate_output=options.get("validate_output"), max_tool_concurrency=options.get("max_tool_concurrency", 4))
    result = await graph.ainvoke({"messages": values}, context=runtime, config={"callbacks": [events], "recursion_limit": 80})
    return result["structured_response"].model_dump()


async def execute_tool_batch(calls, tools, role, context, *, remaining_calls, loaded_skills,
                             max_concurrency=4, on_complete=None, failure_policy=None):
    """验证生产边界中间件与真实 ToolNode，不保存旧批次执行器。"""
    from pydantic import BaseModel
    state = LoopState()
    runtime = runtime_context(context, role, state, max_calls=remaining_calls, failure_policy=failure_policy)
    runtime.loaded_skills = loaded_skills
    runtime.tool_slots = asyncio.Semaphore(max_concurrency)
    names = [call["function"]["name"] for call in calls]
    selected = native_tools(tools, names).for_role(role)
    model = SimpleNamespace(settings=SimpleNamespace(llm_model="test"))
    events = NativeTextEvents(runtime, "Unused", ())
    runtime.events = events
    middleware = TeachingBoundaries(model, tools=selected, output_name="Unused", max_rounds=12, max_tokens=3000)
    converted = []
    identifiers = [call["id"] for call in calls]
    if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("工具调用ID缺失或重复")
    for call in calls:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except ValueError:
            args = {"invalid_json": True}
        converted.append({"id": call["id"], "name": call["function"]["name"],
                          "args": args if isinstance(args, dict) else {"invalid_json": True}, "type": "tool_call"})
    graph = StateGraph(MessagesState, context_schema=RuntimeContext)
    graph.add_node("tools", ToolNode(selected, awrap_tool_call=middleware.awrap_tool_call, handle_tool_errors=False))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = await graph.compile().ainvoke({"messages": [AIMessage(content="", tool_calls=converted)]}, context=runtime)
    traces = {trace["tool_call_id"]: trace for trace in state.trace}
    outcomes = [SimpleNamespace(call_id=message.tool_call_id, name=message.name,
        arguments=traces[message.tool_call_id].get("arguments"), result=json.loads(message.content), trace=traces[message.tool_call_id])
        for message in result["messages"] if message.type == "tool"]
    if on_complete:
        for outcome in outcomes:
            await on_complete(outcome)
    return outcomes
