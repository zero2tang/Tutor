"""原生 Agent 中间件：保留权限、预算、事务失权与公开投影边界。"""

import asyncio
import json
import time

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, convert_to_openai_messages
from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import APIStatusError, OpenAIError

from app.agent.runtime.tool_policy import ToolCircuitOpen, is_execution_error, raise_ownership_loss
from app.agent.tools.errors import ToolRejected
from app.application.agent.tools import compact_tool_result
from app.application.agent.images import provider_messages
from app.infrastructure.clients.llm import ModelError
from app.infrastructure.clients.llm_metrics import ModelRequestMetrics
from app.shared.execution import LearningTurnLost
from app.shared.llm_budget import ContextBudgetExceeded, ProviderContextLimitError, RequestBudget, estimate_request


class TeachingBoundaries(AgentMiddleware):
    """仅执行权限、额度、输入预算和公开投影，不接管 Agent 循环。"""

    def __init__(self, models, *, tools, output_name, max_rounds, max_tokens):
        self.models = models
        self.allowed = {item.name for item in tools}
        self.output_name, self.max_rounds, self.max_tokens = output_name, max_rounds, max_tokens

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        request = request.override(system_message=SystemMessage(content=context.instruction))
        if context.memory_validator:
            await asyncio.to_thread(context.memory_validator)
        context.rounds += 1
        if context.rounds > self.max_rounds + 3:
            raise ValueError("Agent 在运行预算内未能生成有效结果")
        allowed = context.rounds <= self.max_rounds and context.budget.calls_used < context.budget.max_calls
        tools = [item for item in request.tools if allowed and not context.tool_policy.is_blocked(item.name)]
        messages = list(request.messages)
        messages = await provider_messages(messages, context.image_context)
        if not allowed:
            messages.append(HumanMessage(content="工具预算已到，请依据现有结果结束，不得编造。"))
        # 输出契约同样消耗上下文；最终工具由框架绑定，计数不能遗漏。
        definitions = [convert_to_openai_tool(item) for item in tools]
        definitions.append(convert_to_openai_tool(self.output_schema))
        wire = convert_to_openai_messages(([request.system_message] if request.system_message else []) + messages)
        output_tokens = self.max_tokens
        budget = RequestBudget.from_settings(self.models.settings, output_tokens)
        estimate = estimate_request(wire, definitions, image_tokens=budget.image_tokens)
        growth = output_tokens + 1024 if tools else 0
        if estimate.tokens + growth >= 0.9 * budget.available and tools:
            tools, definitions, growth = [], [convert_to_openai_tool(self.output_schema)], 0
            estimate = estimate_request(wire, definitions, image_tokens=budget.image_tokens)
        if estimate.tokens >= 0.9 * budget.available:
            output_tokens = min(1024, output_tokens)
            budget = RequestBudget.from_settings(self.models.settings, output_tokens)
        if estimate.tokens >= 0.9 * budget.available:
            raise ContextBudgetExceeded("完整当前任务与工具契约超过安全预算；原题和学生答案未截断，请分段处理。")
        queued_at = time.perf_counter()
        try:
            async with self.models.limit:
                started = time.perf_counter()
                response = await handler(request.override(tools=tools, messages=messages,
                    model_settings={**request.model_settings, "max_tokens": output_tokens}))
        except (Exception, asyncio.CancelledError) as exc:
            context.usage["_incomplete"] = True
            await context.events.reset()
            if isinstance(exc, APIStatusError):
                body = exc.body if isinstance(exc.body, dict) else {}
                details = str(body.get("error", body)).lower()
                if exc.status_code in (400, 413, 422) and any(marker in details for marker in (
                    "context_length_exceeded", "maximum context length", "context window", "input token limit", "too many input tokens")):
                    raise ProviderContextLimitError("服务商拒绝完整上下文，请缩小材料范围并检查部署预算。") from None
                raise ModelError(f"模型服务返回 HTTP {exc.status_code}") from None
            if isinstance(exc, OpenAIError):
                raise ModelError(f"模型请求失败：{type(exc).__name__}") from None
            raise
        raw = next((message for message in response.result if isinstance(message, AIMessage)), None)
        counts = raw.usage_metadata if raw is not None else None
        if counts:
            context.usage["prompt_tokens"] += counts.get("input_tokens", 0)
            context.usage["completion_tokens"] += counts.get("output_tokens", 0)
        if not counts or not all(type(counts.get(key)) is int and counts[key] > 0 for key in ("input_tokens", "output_tokens")):
            context.usage["_incomplete"] = True
        if raw is not None:
            finish = raw.response_metadata.get("finish_reason")
            if finish not in ("stop", "tool_calls"):
                await context.events.reset()
                raise ModelError("模型返回不完整结果，拒绝继续执行")
            calls = raw.tool_calls
            ids = [call["id"] for call in calls]
            if any(not identifier for identifier in ids) or len(ids) != len(set(ids)):
                raise ValueError("模型工具调用 ID 缺失或重复")
            if any(call["name"] == self.output_name for call in calls) and response.structured_response is None:
                await context.events.reset()
        if context.emit:
            now = time.perf_counter()
            metrics = ModelRequestMetrics(started_at=started, queue_wait_ms=(started-queued_at)*1000,
                request_attempts=1, first_delta_at=context.events.first_delta_at)
            token_usage = raw.response_metadata.get("token_usage") if raw is not None else None
            await context.emit("model_request_completed", {"agent": context.role, "run_id": context.run_id,
                "round": context.rounds, **metrics.snapshot({"model": self.models.settings.llm_model,
                    "thinking": {"type": "disabled"}, "stream": bool(context.events.fields and context.emit)}, token_usage),
                "usage": {"prompt_tokens": counts.get("input_tokens") if counts else None,
                          "completion_tokens": counts.get("output_tokens") if counts else None},
                "usage_status": "partial_or_unknown" if context.usage.get("_incomplete") else "reported",
                "context": {**estimate.as_dict(), "input_budget": budget.available, "effective_output_tokens": output_tokens}})
        return response

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        """动态业务校验使用原生条件跳转，校验闭包不会进入共享编译图。"""
        context = runtime.context
        candidate = state.get("structured_response")
        last = next((message for message in reversed(state["messages"]) if isinstance(message, AIMessage)), None)
        if last is not None and any(call["name"] != self.output_name for call in last.tool_calls):
            # 同轮混合最终输出和业务调用时，先读完真实工具回执再提交最终提案。
            if candidate is not None:
                await context.events.reset()
                feedback = [message.model_copy(update={"status": "error", "content": "先读取本轮业务工具回执，再提交最终结果。"})
                    for message in state["messages"] if isinstance(message, ToolMessage) and message.name == self.output_name
                    and message.tool_call_id in {call["id"] for call in last.tool_calls}]
                return {"structured_response": None, "messages": feedback}
            return None
        invalid_final = candidate is None and last is not None and (
            not last.tool_calls or any(call["name"] == self.output_name for call in last.tool_calls))
        error = ValueError("请调用本次结构化输出函数提交最终结果") if invalid_final else None
        if candidate is not None:
            try:
                arguments = next(call["args"] for call in last.tool_calls if call["name"] == self.output_name)
                strict = self.output_schema.model_validate(arguments, strict=True)
                if context.validate_output:
                    context.validate_output(strict.model_dump())
            except (Exception, asyncio.CancelledError) as exc:
                await context.events.reset()
                if not isinstance(exc, ValueError):
                    raise
                error = exc
        if error:
            context.repairs += 1
            await context.events.reset()
            if context.repairs > 2:
                raise ValueError("Agent 三次结构化尝试均未通过校验") from None
            return {"structured_response": None, "messages": [HumanMessage(content="输出未通过校验，请修正：" + str(error)[:1400])], "jump_to": "model"}

    async def awrap_tool_call(self, request, handler):
        context, call = request.runtime.context, request.tool_call
        name, arguments = call["name"], call["args"]
        index = context.budget.calls_used
        context.budget.calls_used += 1
        started, executing = time.perf_counter(), False
        try:
            if name not in self.allowed or request.tool is None:
                raise PermissionError("当前角色无权调用此工具")
            if index >= context.budget.max_calls:
                raise ValueError("本任务工具调用预算已耗尽")
            # 工具自身类型是唯一协议；显式拒绝额外字段与隐式类型强转。
            schema = request.tool.tool_call_schema
            if set(arguments) - set(schema.model_fields):
                raise ValueError("工具参数含有未声明字段")
            schema.model_validate(arguments, strict=True)

            async def execute():
                nonlocal executing
                async with context.tool_policy.execution_slot(name):
                    executing = True
                    async with context.tool_slots:
                        result = await handler(request)
                    value = json.loads(result.content) if isinstance(result.content, str) else result.content
                    if not isinstance(value, dict):
                        raise ValueError("工具必须返回 JSON 对象")
                    json.dumps(value, ensure_ascii=False, allow_nan=False)
                    return value

            if name.startswith("call_"):
                async with context.write_lock:
                    value = await execute()
            elif name == "read_skill":
                skill = arguments["name"]
                async with context.skill_locks.setdefault(skill, asyncio.Lock()):
                    if skill in context.loaded_skills:
                        value = {"name": skill, "already_loaded": True, "notice": "技能正文已经提供，可直接应用。"}
                    else:
                        value = await execute()
                        context.loaded_skills.add(skill)
            else:
                value = await execute()
            if context.observer:
                value = await context.observer(name, arguments, value, context.scope)
            context.citations.extend(value.get("citations", []))
            if name == "get_concept_details":
                context.citations.extend({key: item[key] for key in ("id", "title", "source_ref", "origin") if key in item}
                                         for item in value.get("materials", []) if item.get("id"))
            trace = {"name": name, "arguments": arguments, "status": "completed"}
            if name == "read_skill" and value.get("already_loaded"):
                trace["cached"] = True
            if name.startswith("call_"):
                trace.update(parent_run_id=context.run_id, root_run_id=context.root_run_id,
                             depth=context.depth, child_run_ids=value.get("child_run_ids", []))
        except (LearningTurnLost, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise_ownership_loss(exc)
            if executing and is_execution_error(exc):
                context.tool_policy.record_failure(name)
            kind = ("circuit_open" if isinstance(exc, ToolCircuitOpen) else
                    "quality_rejected" if isinstance(exc, ToolRejected) else
                    "execution_error" if executing and is_execution_error(exc) else "request_rejected")
            message = "习题质量校验未通过，请调整生成要求后重试。" if name == "call_exercise_agent" and isinstance(exc, ToolRejected) else str(exc)[:500]
            value = {"error": type(exc).__name__, "message": message, "error_kind": kind}
            trace = {"name": name, "arguments": arguments, "status": "failed", **value}
        trace.update(tool_call_id=call["id"], call_index=index,
                     duration_ms=round((time.perf_counter() - started) * 1000, 2),
                     failure_count=context.tool_policy.failure_counts.get(name, 0),
                     failure_limit=context.tool_policy.limit, circuit_open=context.tool_policy.is_blocked(name))
        recorded = ({key: item for key, item in trace.items() if key in {"name", "status", "duration_ms", "tool_call_id", "call_index", "error", "cached"}}
                    if context.role == "exercise" else trace)
        context.trace.append(recorded)
        if context.emit:
            await context.emit("tool_completed", {"agent": context.role, **recorded})
        return ToolMessage(content=json.dumps(compact_tool_result(value, tool_name=name), ensure_ascii=False),
                           name=name, tool_call_id=call["id"])
