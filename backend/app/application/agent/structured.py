"""无业务工具的评分、盲解和复核直接调用标准结构化模型。"""

import asyncio

from langchain_core.messages import HumanMessage, SystemMessage, convert_to_openai_messages
from langsmith import tracing_context

from app.agent.runtime.streaming import AgentTextStream
from app.application.agent.images import provider_messages
from app.infrastructure.clients.chat import invoke_structured


async def run_structured_model(models, messages, schema, runtime, *, instruction, max_tokens,
                               public_fields=(), validate_output=None):
    """仅修复结构和业务校验，网络、截断、权限、取消均直接传播。"""
    values = [SystemMessage(content=instruction), *messages]
    rejections = []
    for attempt in range(3):
        stream = AgentTextStream(public_fields, runtime.role, runtime.emit)
        try:
            if runtime.memory_validator:
                await asyncio.to_thread(runtime.memory_validator)
            with tracing_context(enabled=False):
                try:
                    request_messages = await provider_messages(values, runtime.image_context)
                    result = await invoke_structured(models, convert_to_openai_messages(request_messages), schema,
                        max_tokens=max_tokens, on_content=stream.feed if stream.active else None)
                except (Exception, asyncio.CancelledError):
                    runtime.usage["_incomplete"] = True
                    raise
            counts = result["raw"].usage_metadata or {}
            runtime.usage["prompt_tokens"] += counts.get("input_tokens", 0)
            runtime.usage["completion_tokens"] += counts.get("output_tokens", 0)
            if not result.get("usage_complete", bool(counts)):
                runtime.usage["_incomplete"] = True
            if runtime.emit and result.get("request_metrics"):
                await runtime.emit("model_request_completed", {"agent": runtime.role, "run_id": runtime.run_id,
                    "round": attempt + 1, **{key: value for key, value in result["request_metrics"].items()
                        if key in {"model", "thinking", "stream", "duration_ms", "queue_wait_ms", "request_attempts",
                                   "first_delta_ms", "first_content_ms", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens"}},
                    "usage": {"prompt_tokens": counts.get("input_tokens"), "completion_tokens": counts.get("output_tokens"),
                        **{key: value for key, value in (result["raw"].response_metadata.get("token_usage") or {}).items()
                           if key in {"prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}}},
                    "usage_status": "partial_or_unknown" if runtime.usage.get("_incomplete") else "reported",
                    "context": result.get("context_metrics", {})})
            try:
                if result.get("parsing_error"):
                    raise result["parsing_error"]
                if result.get("parsed") is None:
                    raise ValueError("结构化模型未返回可校验结果")
                output = result["parsed"].model_dump()
                if validate_output:
                    validate_output(output)
            except ValueError as exc:
                await stream.reset()
                details = ([{key: item[key] for key in ("loc", "msg", "type") if key in item}
                            for item in exc.errors(include_input=False, include_context=False)]
                           if hasattr(exc, "errors") else [{"type": type(exc).__name__, "msg": str(exc)[:1400]}])
                rejections.append({"attempt": attempt + 1, "errors": details})
                if attempt == 2:
                    error = ValueError("模型三次结构化尝试均未通过校验")
                    error.validation_rejections = rejections
                    raise error from None
                # 让模型看到被拒绝的真实候选及校验反馈，避免只见错误提示的盲重试。
                # 候选只留在本次私有调用消息中，不作为正式评分或公开正文提交。
                raw = result["raw"]
                feedback = "输出未通过字段或业务校验，请修正：" + str(details)[:1400]
                values.extend([raw, HumanMessage(content=feedback)])
                continue
            return output
        except (Exception, asyncio.CancelledError):
            await stream.reset()
            raise
