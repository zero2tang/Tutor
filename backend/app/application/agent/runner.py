"""唯一角色调用入口：运行准备、标准 Agent／结构化模型与记录生命周期。"""

import asyncio
import json
import math
import time
from uuid import uuid4

from langchain_core.messages import HumanMessage, convert_to_messages
from langchain_core.runnables.config import merge_configs
from langsmith import tracing_context

from app.agent.context.builder import ContextBuilder, fingerprint
from app.agent.runtime.contracts import EventSink, RuntimeContext, RunBudget
from app.agent.runtime.tool_policy import SPECIALIST_TOOL_ROUNDS, SUPERVISOR_TOOL_ROUNDS
from app.application.agent.graph import create_role_agent
from app.application.agent.structured import run_structured_model
from app.application.agent.images import QuestionImageContext
from app.application.agent.policy import TeachingRunPolicy
from app.application.agent.prompts import build_instruction
from app.application.agent.records import SQLRunRecorder
from app.infrastructure.persistence.database import db_session


def task_messages(payload, role, task_id, *, previous=None, image_context=None):
    """正式历史只投影一次；当前原题、原答与帮助快照始终完整保留。"""
    fixed, history = ContextBuilder().project(payload, role)
    values = list(convert_to_messages(previous or []))
    if previous:
        fixed.pop("recent_messages", None)
        # 已执行委派已存在原生ToolMessage中，不再重复携带整段公开正文。
        fixed.pop("completed_delegations", None)
    if not previous:
        values.extend(HumanMessage(content=json.dumps({"closed_history_source": unit}, ensure_ascii=False),
                                   id=unit["source_ref"], additional_kwargs={"tutor_source_refs": [unit["source_ref"]]})
                      for unit in history)
    # 持久消息已包含历史时，不再次注入recent_messages等可重建的历史副本。
    current = {"role": "user", "content": json.dumps(fixed, ensure_ascii=False)}
    values.append(HumanMessage(content=current["content"], id=task_id,
                               additional_kwargs={"tutor_current_run": task_id}))
    return values


class AgentRunner:
    def __init__(self, models, tools, *, memory=None, database=None, prepare_run=None, recorder=None):
        self.models, self.tools, self.memory = models, tools, memory
        database = database or db_session
        self.policy = TeachingRunPolicy(models, database, memory=lambda: self.memory,
            instruction_builder=lambda *args: build_instruction(*args),
            image_factory=lambda course_id: QuestionImageContext(course_id))
        self.prepare_run = prepare_run or self.policy.prepare
        self.recorder = recorder or SQLRunRecorder(database)

    async def run(self, role, skill, payload, schema, context, turn_id, *,
                  emit=None, use_tools=True, validate_output=None, max_tokens=3000,
                  max_tool_concurrency=4, tool_source=None, timeout_seconds=240,
                  context_purpose=None, runtime_context=None, config=None, messages=None):
        """子调用显式接收父运行上下文，不操作框架私有 ContextVar。"""
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Agent 超时必须为正的有限秒数")
        if type(max_tool_concurrency) is not int or not 1 <= max_tool_concurrency <= 16:
            raise ValueError("工具并发数应为1至16的整数")
        started, run_id = time.perf_counter(), str(uuid4())
        parent = runtime_context
        provider = tool_source if hasattr(tool_source, "for_role") else self.tools
        handler = (getattr(tool_source, "handler", None) or
                   (tool_source if callable(tool_source) else None))
        if handler is None and parent:
            handler = parent.delegation_handler
        budget = parent.budget if parent else RunBudget()
        if parent is None and getattr(tool_source, "runtime_policy", None) is not None:
            budget.failures = tool_source.runtime_policy
        prepared = await self.prepare_run(role, skill, payload, schema, context, run_id, emit, use_tools,
                                          max_tokens, context_purpose=context_purpose)
        runtime = RuntimeContext(scope=context, role=role, run_id=run_id,
            root_run_id=parent.root_run_id if parent else run_id,
            parent_run_id=parent.run_id if parent else None, depth=parent.depth + 1 if parent else 0,
            budget=budget, tools=provider, delegation_handler=handler, emit=emit,
            image_context=prepared.image_context, memory_validator=prepared.memory_validator,
            citations=list(prepared.citations), loaded_skills=set(prepared.preloaded_skills))
        metadata = {"parent_run_id": runtime.parent_run_id, "root_run_id": runtime.root_run_id, "depth": runtime.depth}
        task_id = "task:" + fingerprint({"request": context.request_id, "role": role, "skill": skill,
                                        "payload": prepared.payload})
        values = task_messages(prepared.payload, role, task_id, previous=messages, image_context=prepared.image_context)
        self.recorder.start(run_id, role, skill, context, turn_id, self.models.settings.llm_model)
        if emit:
            await emit("agent_started", {"agent": role, "name": prepared.display_name, "skill": skill, "run_id": run_id, **metadata})
        try:
            async with asyncio.timeout(timeout_seconds):
                if not prepared.use_tools:
                    output = await run_structured_model(self.models, values, schema, runtime,
                        instruction=prepared.instruction, max_tokens=max_tokens,
                        public_fields=prepared.public_fields, validate_output=validate_output)
                    final_messages = values
                else:
                    tools = provider.for_role(role, handler=handler)
                    graph, events = create_role_agent(self.models, runtime, schema, prepared.instruction,
                        tools=tools, max_rounds=SUPERVISOR_TOOL_ROUNDS if role == "supervisor" else SPECIALIST_TOOL_ROUNDS,
                        max_tokens=max_tokens, public_fields=prepared.public_fields, validate_output=validate_output,
                        observer=getattr(provider, "observe", None), max_tool_concurrency=max_tool_concurrency,
                        task_id=task_id)
                    call_config = merge_configs(config or {}, {"callbacks": [events], "recursion_limit": 80,
                        "metadata": {"tutor_agent_run_id": runtime.run_id}})
                    persistence = {"durability": "sync"} if config and config.get("metadata", {}).get("tutor_durable") else {}
                    with tracing_context(enabled=False):
                        result = await graph.ainvoke({"messages": values}, config=call_config, context=runtime, **persistence)
                    output, final_messages = result["structured_response"].model_dump(), result["messages"]
                if runtime.memory_validator:
                    await asyncio.to_thread(runtime.memory_validator)
                duration = round((time.perf_counter() - started) * 1000, 2)
                self.recorder.complete(run_id, duration, runtime.usage, runtime.trace, output)
                if emit:
                    await emit("agent_completed", {"agent": role, "name": prepared.display_name, "run_id": run_id,
                        **metadata, "duration_ms": duration,
                        "tokens": runtime.usage["prompt_tokens"] + runtime.usage["completion_tokens"],
                        "usage_status": "partial_or_unknown" if runtime.usage.get("_incomplete") else "reported"})
                return {"output": output, "messages": final_messages,
                        "citations": list({item["id"]: item for item in runtime.citations if item.get("id")}.values())[:10],
                        "run_id": run_id, **metadata, "usage": runtime.usage, "duration_ms": duration}
        except (Exception, asyncio.CancelledError) as exc:
            self.recorder.fail(run_id, (time.perf_counter() - started) * 1000, runtime.usage, runtime.trace, exc)
            if emit:
                await emit("agent_failed", {"agent": role, "run_id": run_id, **metadata, "error": type(exc).__name__})
            raise
