"""标准聊天模型工厂与纯结构化请求；复用现有连接、并发和预算。"""

import json
import time

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_openai import ChatOpenAI

from app.infrastructure.clients.llm import ModelError
from app.infrastructure.clients.llm_metrics import ModelRequestMetrics
from app.shared.llm_budget import ProviderContextLimitError, assert_request_budget


def create_chat_model(client, *, max_tokens=3000, streaming=False):
    """沿用 F0 验证的部署配置；SDK 不重试，避免与上层修复次数相乘。"""
    settings = client.settings
    if not settings.llm_url or not settings.llm_key:
        raise ModelError("模型地址或密钥未配置")
    base = getattr(client, "_native_chat_model", None)
    if base is None:
        base = ChatOpenAI(
            model=settings.llm_model, api_key=settings.llm_key, base_url=settings.llm_url,
            http_async_client=client.http, timeout=100, max_retries=0,
            use_responses_api=False, extra_body={"thinking": {"type": "disabled"}},
            temperature=0.25, max_tokens=3000, stream_usage=True,
        )
        client._native_chat_model = base
    # 浅复制只改变本次请求的静态选项，共享 SDK／HTTP 连接，不共享可变执行状态。
    return base.model_copy(update={"max_tokens": max_tokens, "streaming": streaming})


class StructuredStream(AsyncCallbackHandler):
    """纯结构化调用只转发JSON正文；公开字段仍由调用方过滤。"""

    raise_error = True

    def __init__(self, on_content, metrics):
        self.on_content, self.metrics = on_content, metrics

    async def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        message = getattr(chunk, "message", None)
        if message is None:
            return
        if self.metrics.first_delta_at is None and (
            message.content or message.tool_call_chunks or message.additional_kwargs
        ):
            self.metrics.first_delta_at = time.perf_counter()
        if message.content and self.on_content:
            self.metrics.content_received()
            await self.on_content(message.content)


async def invoke_structured(client, messages, schema, *, max_tokens=3000, on_content=None):
    """唯一纯结构化路径：标准JSON mode与原始正文的严格Pydantic校验。"""
    from openai import APIStatusError, OpenAIError, LengthFinishReasonError, ContentFilterFinishReasonError

    # Schema只从业务类型生成；它作为真实输入计入同一预算，没有手写第二份声明。
    messages = [*messages, {"role": "system", "content": "本调用没有业务工具，请直接输出一个合法JSON对象；"
        "不要调用函数或添加Markdown围栏。必须符合以下JSON Schema：\n" +
        json.dumps(schema.model_json_schema(), ensure_ascii=False)}]
    budget, estimate = assert_request_budget(
        client.settings, messages, [], max_tokens=max_tokens
    )
    model = create_chat_model(client, max_tokens=max_tokens, streaming=bool(on_content))
    runnable = model.with_structured_output(
        schema, method="json_mode", include_raw=True
    )
    metrics = ModelRequestMetrics()
    callback = StructuredStream(on_content, metrics)
    queued_at = time.perf_counter()
    try:
        async with client.limit:
            metrics.request_started(queued_at)
            result = await runnable.ainvoke(
                messages,
                config={"callbacks": [callback]} if on_content else None,
            )
    except (LengthFinishReasonError, ContentFilterFinishReasonError):
        raise ModelError("模型返回不完整结果，拒绝结构化候选") from None
    except APIStatusError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body)
        text = str(error.get("code", "")) + " " + str(error.get("message", "")) if isinstance(error, dict) else ""
        if exc.status_code in (400, 413, 422) and any(
            marker in text.lower() for marker in (
                "context_length_exceeded", "maximum context length", "context window",
                "input token limit", "too many input tokens",
            )
        ):
            raise ProviderContextLimitError("服务商拒绝上下文长度，需要重建请求并校准部署预算。") from None
        raise ModelError(f"模型服务返回 HTTP {exc.status_code}") from None
    except OpenAIError as exc:
        # 网络失败不自动重放；公开草稿可能已经产生，且服务商用量可能未知。
        raise ModelError(f"模型请求失败：{type(exc).__name__}") from None
    raw = result["raw"]
    finish = raw.response_metadata.get("finish_reason")
    if finish not in ("stop", "tool_calls"):
        raise ModelError("模型返回不完整结果，拒绝结构化候选")
    # SDK可能把数字字符串强制转为数值，或从不完整JSON解析候选；只接受完整原文。
    try:
        if raw.tool_calls or raw.invalid_tool_calls or not isinstance(raw.content, str):
            raise ValueError("纯结构化调用必须返回JSON正文")
        result["parsed"] = schema.model_validate_json(raw.content, strict=True)
        result["parsing_error"] = None
    except ValueError as exc:
        result["parsed"], result["parsing_error"] = None, exc
    usage = raw.response_metadata.get("token_usage")
    # SDK会把缺失Token字段补零；本路径非空输入和JSON输出的零值保守视为未知。
    counts = raw.usage_metadata or {}
    result["usage_complete"] = all(
        type(counts.get(key)) is int and counts[key] > 0
        for key in ("input_tokens", "output_tokens")
    )
    result["request_metrics"] = metrics.snapshot(
        {"model": client.settings.llm_model, "thinking": {"type": "disabled"}, "stream": bool(on_content)},
        usage,
    )
    result["context_metrics"] = {
        **estimate.as_dict(), "input_budget": budget.available,
        "estimated_minus_actual_tokens": (
            estimate.tokens - counts["input_tokens"] if result["usage_complete"] else None
        ),
    }
    return result
