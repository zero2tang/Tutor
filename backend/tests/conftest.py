"""测试只注册表定义，各用例自行提供隔离数据库连接。"""

from app.application.model_registry import register_models

register_models()


import json
import pytest
from langchain_core.messages import AIMessage


@pytest.fixture(autouse=True, scope="session")
def isolated_memory_document_stores():
    """仅测试把 SQLite Engine 显式绑定到独立官方内存 Store，生产没有自动兜底。"""
    from weakref import WeakSet
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from langgraph.store.memory import InMemoryStore
    from app.infrastructure.persistence.memory_documents import (
        bind_memory_document_store, memory_document_repository,
        unbind_memory_document_store, MemoryDocumentError,
    )

    bound = WeakSet()

    def attach(connection):
        engine = connection.engine
        if engine.dialect.name != "sqlite":
            return
        try:
            memory_document_repository(engine)
        except MemoryDocumentError:
            bind_memory_document_store(engine, InMemoryStore())
            bound.add(engine)

    event.listen(Engine, "engine_connect", attach)
    yield
    event.remove(Engine, "engine_connect", attach)
    for engine in list(bound):
        unbind_memory_document_store(engine)


@pytest.fixture(autouse=True)
def scripted_structured_gateway(monkeypatch):
    """既有业务测试只替换模型出口；真实 ModelClient 仍走标准 SDK 和 HTTP。

    旧脚本模型提供的是预录制 JSON，不代表 provider 工具协议。标准协议、
    流式与取消另外由 MockTransport 和真实模型验收，生产代码没有此兼容分支。
    """
    from app.application.agent import structured
    from app.infrastructure.clients.llm import ModelClient

    original = structured.invoke_structured

    async def invoke(models, messages, schema, *, max_tokens=3000, on_content=None):
        if isinstance(models, ModelClient):
            return await original(models, messages, schema, max_tokens=max_tokens, on_content=on_content)
        response = await models.chat(
            messages, json_output=True, max_tokens=max_tokens,
            **({"on_content": on_content} if on_content else {}),
        )
        choice = response["choices"][0]
        content = choice["message"].get("content") or ""
        if content.startswith("```") and "\n" in content:
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        parsed, error = None, None
        try:
            parsed = schema.model_validate_json(content)
        except ValueError as exc:
            error = exc
        usage = response.get("usage") or {}
        raw = AIMessage(content=content, response_metadata={"token_usage": usage}, usage_metadata={
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": sum(usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")),
        })
        return {"raw": raw, "parsed": parsed, "parsing_error": error,
                "usage_complete": all(usage.get(key, 0) > 0 for key in ("prompt_tokens", "completion_tokens")),
                "request_metrics": response.get("_request_metrics"),
                "context_metrics": response.get("_context_metrics")}

    monkeypatch.setattr(structured, "invoke_structured", invoke)
    monkeypatch.setattr("app.application.agent.summary.invoke_structured", invoke)
    monkeypatch.setattr("app.domains.memory.concepts.observations.invoke_structured", invoke)
    monkeypatch.setattr("app.domains.memory.concepts.service.invoke_structured", invoke)
    monkeypatch.setattr("app.domains.memory.scopes.promotion.invoke_structured", invoke)


@pytest.fixture(autouse=True)
def scripted_native_chat_gateway(monkeypatch):
    """只替换录制模型，生产客户端与真实SDK测试仍走原有标准模型工厂。"""
    import asyncio
    from app.application.agent import graph, summary
    from app.infrastructure.clients.llm import ModelClient
    from native_scripted_model import ScriptedChatModel

    original = graph.create_chat_model

    def factory(models, **kwargs):
        if isinstance(models, ModelClient):
            return original(models, **kwargs)
        if not hasattr(models, "limit"):
            models.limit = asyncio.Semaphore(3)
        return ScriptedChatModel(source=models)

    monkeypatch.setattr(graph, "create_chat_model", factory)
    monkeypatch.setattr(summary, "create_chat_model", factory)
