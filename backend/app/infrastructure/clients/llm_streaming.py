"""解析模型 SSE 响应，并还原普通对话响应供工具循环继续使用。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
import json
import time

import httpx


ContentCallback = Callable[[str], Awaitable[None]]


class ModelStreamError(RuntimeError):
    """模型流的格式错误或未完整结束。"""


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """按 SSE 事件边界读取数据，兼容注释心跳和多行 data 字段。"""
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
            continue
        if line.startswith(":"):
            continue
        name, separator, value = line.partition(":")
        if name == "data":
            data.append(value.removeprefix(" ") if separator else "")
    if data:
        yield "\n".join(data)


def _append_text(target: dict, key: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ModelStreamError("模型流包含无效的文本增量")
    target[key] = target.get(key, "") + value


@dataclass
class StreamAccumulator:
    """保留完整响应以及是否已经收到模型增量，供调用方决定能否重试。"""

    metadata: dict = field(default_factory=dict)
    choices: dict[int, dict] = field(default_factory=dict)
    tool_calls: dict[int, dict[int, dict]] = field(default_factory=dict)
    usage: dict | None = None
    has_increment: bool = False
    first_increment_at: float | None = None
    done: bool = False

    async def consume(self, response: httpx.Response, on_content: ContentCallback) -> dict:
        async for data in iter_sse_data(response):
            if data.strip() == "[DONE]":
                self.done = True
                break
            try:
                chunk = json.loads(data)
            except (TypeError, ValueError) as exc:
                raise ModelStreamError("模型流包含无效的 JSON 事件") from exc
            if not isinstance(chunk, dict) or chunk.get("error"):
                raise ModelStreamError("模型服务返回流式错误事件")
            for key, value in chunk.items():
                if key not in ("choices", "usage"):
                    self.metadata[key] = value
            if chunk.get("usage") is not None:
                self.usage = chunk["usage"]
            rows = chunk.get("choices", [])
            if not isinstance(rows, list):
                raise ModelStreamError("模型流包含无效的候选响应")
            for row in rows:
                await self._consume_choice(row, on_content)

        # 有些连接在最后一个正文分片后直接断开，不能将它伪装成成功完成。
        if not self.done or not self.choices or any(
            row["finish_reason"] is None for row in self.choices.values()
        ):
            raise ModelStreamError("模型流未完整结束")
        result = dict(self.metadata)
        result["object"] = "chat.completion"
        result["choices"] = []
        for index, choice in sorted(self.choices.items()):
            if index in self.tool_calls:
                choice["message"]["tool_calls"] = [
                    call for _, call in sorted(self.tool_calls[index].items())
                ]
            result["choices"].append(choice)
        if self.usage is not None:
            result["usage"] = self.usage
        return result

    async def _consume_choice(self, row: object, on_content: ContentCallback) -> None:
        if not isinstance(row, dict) or not isinstance(row.get("index", 0), int):
            raise ModelStreamError("模型流包含无效的候选索引")
        index = row.get("index", 0)
        choice = self.choices.setdefault(
            index,
            {
                "index": index,
                "message": {"role": "assistant", "content": None},
                "finish_reason": None,
            },
        )
        delta = row.get("delta") or {}
        if not isinstance(delta, dict):
            raise ModelStreamError("模型流包含无效的消息增量")
        if any(value is not None and value != "" and value != [] for value in delta.values()):
            # 包括推理和工具参数：它们开始生成后也不能重新请求整个模型响应。
            if not self.has_increment:
                self.first_increment_at = time.perf_counter()
            self.has_increment = True
        if delta.get("role"):
            choice["message"]["role"] = delta["role"]
        if delta.get("content") is not None:
            content = delta["content"]
            if choice["message"]["content"] is None:
                choice["message"]["content"] = ""
            _append_text(choice["message"], "content", content)
            if content:
                await on_content(content)
        _append_text(choice["message"], "reasoning_content", delta.get("reasoning_content"))
        calls = delta.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ModelStreamError("模型流包含无效的工具增量")
        for position, item in enumerate(calls):
            if not isinstance(item, dict) or not isinstance(item.get("index", position), int):
                raise ModelStreamError("模型流包含无效的工具索引")
            call = self.tool_calls.setdefault(index, {}).setdefault(
                item.get("index", position), {"id": "", "type": "function", "function": {}}
            )
            _append_text(call, "id", item.get("id"))
            if item.get("type"):
                call["type"] = item["type"]
            function = item.get("function") or {}
            if not isinstance(function, dict):
                raise ModelStreamError("模型流包含无效的工具参数")
            for key in ("name", "arguments"):
                _append_text(call["function"], key, function.get(key))
        if row.get("finish_reason") is not None:
            choice["finish_reason"] = row["finish_reason"]
