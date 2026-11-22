"""测试专用标准 ChatModel：旧的录制脚本在边界外转换成真实工具协议。"""

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    source: Any = Field(exclude=True)

    @property
    def _llm_type(self):
        return "native-scripted-test"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(item) for item in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("测试模型只支持异步执行")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        wire = convert_to_openai_messages(messages)
        # 录制脚本按旧快照的第二项读取任务；只调整测试边界的观察顺序。
        current = [index for index, message in enumerate(messages)
                   if message.additional_kwargs.get("tutor_current_run")]
        active_tools = [wire[index] for index, message in enumerate(messages)
                        if index > (current[-1] if current else 0)
                        and wire[index]["role"] == "tool"
                        and not (getattr(message, "name", "") or "")[0:1].isupper()]
        if current and current[-1] != 1:
            wire.insert(1, wire.pop(current[-1]))
        response = await self.source.chat(
            wire, tools=kwargs.get("tools"),
            json_output=True, max_tokens=kwargs.get("max_tokens", 3000),
            native_current_tool_messages=active_tools,
        )
        value = response["choices"][0]["message"]
        calls = value.get("tool_calls") or []
        native_calls = [
            {"id": call["id"], "name": call["function"]["name"],
             "args": json.loads(call["function"]["arguments"]), "type": "tool_call"}
            for call in calls
        ]
        content = value.get("content") or ""
        if not native_calls:
            output_name = next((
                tool["function"]["name"] for tool in reversed(kwargs.get("tools") or [])
                if tool["function"]["name"][0].isupper()
            ), None)
            if output_name:
                native_calls = [{
                    "id": "scripted-structured-output", "name": output_name,
                    "args": json.loads(content), "type": "tool_call",
                }]
                content = ""
        usage = response.get("usage") or {}
        message = AIMessage(
            content=content, tool_calls=native_calls,
            response_metadata={"finish_reason": "tool_calls" if native_calls else "stop"},
            usage_metadata={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": sum(usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = (await self._agenerate(messages, stop, run_manager, **kwargs)).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=message.content,
            tool_call_chunks=[{
                "index": index, "id": call["id"], "name": call["name"],
                "args": json.dumps(call["args"], ensure_ascii=False), "type": "tool_call_chunk",
            } for index, call in enumerate(message.tool_calls)],
            usage_metadata=message.usage_metadata, response_metadata=message.response_metadata,
        ))
