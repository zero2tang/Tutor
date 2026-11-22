"""隔离验证当前模型部署与框架，不切换生产 Agent、不写学习业务数据。"""

import asyncio
import base64
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / ".tmp/f0-deps"), str(ROOT / "backend")]
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import httpx
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import tool
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel
from openai import APITimeoutError
from app.infrastructure.config import get_settings

REPORT = ROOT / "data/reports/simplification_p0/f0_model_compatibility.json"


class Answer(BaseModel):
    answer: int


class BodyMass(BaseModel):
    xiaoming: int
    xiaohua: int
    xiaoqiang: int


async def main():
    settings = get_settings()
    if REPORT.exists():
        REPORT.with_name(f"f0_previous_{time.time_ns()}.json").write_bytes(REPORT.read_bytes())
    report = {"model": settings.llm_model, "production_runtime_changed": False,
        "versions": {p: importlib.metadata.version(p) for p in ("langchain", "langgraph", "langchain-openai", "openai", "pydantic")},
        "checks": [], "wire": []}

    async def on_request(request):
        value = json.loads(request.content)
        # 只记录接口形状，绝不记录凭证、原题图片或完整模型上下文。
        report["wire"].append({"model": value.get("model"), "stream": value.get("stream", False),
            "thinking": value.get("thinking"), "tool_count": len(value.get("tools", [])),
            "response_format": (value.get("response_format") or {}).get("type")})

    client = httpx.AsyncClient(trust_env=False, event_hooks={"request": [on_request]})
    model = ChatOpenAI(model=settings.llm_model, api_key=settings.llm_key,
        base_url=settings.llm_url, timeout=40, max_retries=0, use_responses_api=False,
        extra_body={"thinking": {"type": "disabled"}}, http_async_client=client,
        stream_usage=True)

    async def check(name, action, *, optional=False):
        started = time.perf_counter()
        try:
            details = await action()
            result = {"name": name, "passed": True, "details": details}
        except Exception as exc:
            error = getattr(exc, "body", {})
            error = error.get("error", error) if isinstance(error, dict) else {}
            message = str(error.get("message", ""))[:240]
            result = {"name": name, "passed": False, "error_type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None), "optional": optional,
                "unsupported": optional and getattr(exc, "status_code", None) == 400 and "unavailable" in message.lower(),
                "message": message}
        result["seconds"] = round(time.perf_counter() - started, 3)
        report["checks"].append(result)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)

    async def text():
        reply = await model.ainvoke("只回答数字：17加19等于多少？")
        assert "36" in reply.content
        assert reply.usage_metadata and reply.usage_metadata["output_tokens"] > 0
        return {"text": reply.content, "usage": reply.usage_metadata}

    async def structured(method):
        value = await model.with_structured_output(Answer, method=method, include_raw=True).ainvoke('请用JSON返回17加19的结果，格式：{"answer":整数}')
        assert value["parsed"].answer == 36 and value["parsing_error"] is None
        return {"answer": value["parsed"].answer, "usage": value["raw"].usage_metadata}

    async def vision():
        source = ROOT / "data/evaluation/agent_f0/textbook_example.png"
        if not source.exists():
            original = Path(r"C:/Users/admin/AppData/Local/Temp/codex-clipboard-a015aa7a-63e2-47e7-b536-6ed0d1b9ab98.png")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(original.read_bytes())
        data = base64.b64encode(source.read_bytes()).decode()
        prompt = HumanMessage(content=[{"type": "text", "text": "阅读图片中例(1)，用带正负号的整数表示小明、小华、小强的体重增长值（kg），返回三个字段。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}}])
        value = await model.with_structured_output(BodyMass, method="function_calling", include_raw=True).ainvoke([prompt])
        assert value["parsed"].model_dump() == {"xiaoming": 2, "xiaohua": -1, "xiaoqiang": 0}
        return {"image": source.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "input_origin": "user_provided_textbook_page_screenshot",
            "parsed": value["parsed"].model_dump(), "usage": value["raw"].usage_metadata}

    async def agent():
        calls = []
        @tool
        def multiply(a: int, b: int) -> int:
            """调用计算器计算两个整数的乘积。"""
            calls.append([a, b])
            return a * b
        checkpoint = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=[(__name__, "Answer")]))
        runtime = create_agent(model=model, tools=[multiply], response_format=ToolStrategy(Answer),
            system_prompt="你必须实际调用 multiply 工具计算，再返回结构化结果。", checkpointer=checkpoint)
        config = {"configurable": {"thread_id": "f0-" + uuid4().hex}, "recursion_limit": 12}
        value = await runtime.ainvoke({"messages": [{"role": "user", "content": "计算17乘19。"}]}, config)
        assert value["structured_response"].answer == 323 and calls == [[17, 19]]
        state = await runtime.aget_state(config)
        assert not state.next
        usage = [m.usage_metadata for m in value["messages"] if getattr(m, "usage_metadata", None)]
        return {"answer": 323, "executed_tools": calls, "checkpoint_completed": True,
            "usage": {k: sum(u.get(k, 0) for u in usage) for k in ("input_tokens", "output_tokens", "total_tokens")},
            "message_types": [m.type for m in value["messages"]]}

    async def streaming():
        start, first, chunks, usage = time.perf_counter(), None, [], None
        async for part in model.astream("请用一句中文解释负数，不超过20字。"):
            if part.content:
                first = first or time.perf_counter()
                chunks.append(part.content)
            if part.usage_metadata:
                usage = part.usage_metadata
        assert chunks and usage and usage["output_tokens"] > 0
        return {"text": "".join(chunks), "chunks": len(chunks), "first_content_seconds": round(first - start, 3), "usage": usage}

    async def invalid_model():
        bad = model.model_copy(update={"model_name": "f0-model-that-does-not-exist"})
        before = len(report["wire"])
        try:
            await bad.ainvoke("你好")
        except Exception as exc:
            assert getattr(exc, "status_code", 0) >= 400
            return {"error_type": type(exc).__name__, "status_code": exc.status_code,
                "requests": len(report["wire"]) - before}
        raise AssertionError("无效模型未返回错误")

    async def timeout():
        limited = ChatOpenAI(model=settings.llm_model, api_key=settings.llm_key, base_url=settings.llm_url,
            timeout=0.001, max_retries=0, use_responses_api=False,
            extra_body={"thinking": {"type": "disabled"}}, http_async_client=client)
        try:
            await limited.ainvoke("请解释整数。")
        except Exception as exc:
            assert isinstance(exc, APITimeoutError)
            return {"sdk_timeout_error": type(exc).__name__, "configured_timeout_seconds": 0.001}
        raise AssertionError("未触发超时")

    async def cancel():
        before = len(report["wire"])
        task = asyncio.create_task(model.ainvoke("请详细解释有理数。"))
        for _ in range(100):
            if len(report["wire"]) > before:
                break
            await asyncio.sleep(0.01)
        assert len(report["wire"]) > before
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return {"cancelled_after_request_started": True}
        raise AssertionError("取消未生效")

    try:
        for name, action, optional in [
            ("text_and_usage", text, False),
            ("structured_tool_calling", lambda: structured("function_calling"), False),
            ("structured_json_mode", lambda: structured("json_mode"), False),
            ("native_json_schema", lambda: structured("json_schema"), True),
            ("textbook_image", vision, False), ("agent_tool_loop_and_graph_checkpoint", agent, False),
            ("incremental_stream_and_usage", streaming, False), ("http_error_no_retry", invalid_model, False),
            ("bounded_timeout", timeout, False), ("request_cancellation", cancel, False)]:
            await check(name, action, optional=optional)
        report["thinking_disabled_on_wire"] = all(w["thinking"] == {"type": "disabled"} for w in report["wire"])
        report["complete"] = all(c["passed"] or c.get("unsupported") for c in report["checks"]) and report["thinking_disabled_on_wire"]
        report["reported_usage_only"] = {k: sum(c.get("details", {}).get("usage", {}).get(k, 0) for c in report["checks"])
            for k in ("input_tokens", "output_tokens", "total_tokens")}
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await client.aclose()
    if not report.get("complete"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
