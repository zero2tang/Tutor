"""短角色工厂：静态配置编译一次，运行态通过RuntimeContext注入。"""

import asyncio
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.utils.function_calling import convert_to_openai_tool
from app.agent.runtime.contracts import RuntimeContext
from app.agent.runtime.streaming import NativeTextEvents
from app.application.agent.middleware import TeachingBoundaries
from app.application.agent.summary import TeachingSummaryMiddleware
from app.infrastructure.clients.chat import create_chat_model


def create_role_agent(models, context, schema, instruction, *, tools, max_rounds, max_tokens,
                      public_fields=(), validate_output=None, observer=None, max_tool_concurrency=4, task_id=None):
    """按静态角色配置缓存编译图；学生、输入、校验器和展示器只在RuntimeContext。"""
    context.instruction, context.validate_output, context.observer = instruction, validate_output, observer
    context.task_id = task_id or context.run_id
    context.tool_slots = asyncio.Semaphore(max_tool_concurrency)
    context.events = NativeTextEvents(context, schema.__name__, public_fields)
    cache = getattr(models, "_native_role_agents", None)
    if cache is None:
        cache = models._native_role_agents = {}
    key = (context.role, schema, tuple(id(item) for item in tools), max_rounds, max_tokens,
           bool(context.emit and public_fields), max_tool_concurrency)
    if key not in cache:
        output = schema
        boundaries = TeachingBoundaries(models, tools=tools, output_name=output.__name__,
            max_rounds=max_rounds, max_tokens=max_tokens)
        boundaries.output_schema = output
        cache[key] = create_agent(
            model=create_chat_model(models, max_tokens=max_tokens, streaming=bool(context.emit and public_fields)),
            tools=tools, response_format=ToolStrategy(output),
            middleware=[TeachingSummaryMiddleware(models, max_tokens=max_tokens,
                definitions=[*[convert_to_openai_tool(item) for item in tools], convert_to_openai_tool(output)]), boundaries],
            context_schema=RuntimeContext, checkpointer=None, name=context.role,
        )
    return cache[key], context.events
