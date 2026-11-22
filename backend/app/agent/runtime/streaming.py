"""从结构化模型流中提取可公开的正文；草稿不替代最终契约校验。"""

import json
import time
from langchain_core.callbacks import AsyncCallbackHandler
from uuid import uuid4


class PublicJSONText:
    """逐字符读取 JSON 字符串，只接受白名单内的顶层字符串值。

    字符串内的括号、引号和转义不改变层级；嵌套答案、工具参数和推理
    不会因名称相同而被当成正文。完整 JSON 的合法性仍由 Pydantic 检查。
    """

    def __init__(self, fields: set[str]):
        self.fields = fields
        self.depth = 0
        self.started = False
        self.finished = False
        self.phase = "key"
        self.key = ""
        self.in_string = False
        self.kind = ""
        self.token = ""
        self.escaped = False
        self.unicode_digits: str | None = None
        self.high_surrogate = ""

    def feed(self, delta: str) -> list[tuple[str, str]]:
        chunks: list[tuple[str, str]] = []

        def append(value):
            if self.kind != "public":
                return
            for character in value:
                code = ord(character)
                if self.high_surrogate:
                    high = ord(self.high_surrogate)
                    self.high_surrogate = ""
                    if 0xDC00 <= code <= 0xDFFF:
                        character = chr(0x10000 + ((high - 0xD800) << 10) + code - 0xDC00)
                    else:
                        # 非法代理项不向 UTF-8 事件通道传播，最终校验负责拒绝该输出。
                        raise ValueError("模型正文含不完整的 Unicode 代理项")
                elif 0xD800 <= code <= 0xDBFF:
                    self.high_surrogate = character
                    continue
                elif 0xDC00 <= code <= 0xDFFF:
                    raise ValueError("模型正文含孤立的 Unicode 代理项")
                if chunks and chunks[-1][0] == self.key:
                    chunks[-1] = (self.key, chunks[-1][1] + character)
                else:
                    chunks.append((self.key, character))

        for character in delta:
            if self.finished:
                break
            if self.in_string:
                if self.kind == "key":
                    self.token += character
                if self.unicode_digits is not None:
                    self.unicode_digits += character
                    if len(self.unicode_digits) == 4:
                        append(chr(int(self.unicode_digits, 16)))
                        self.unicode_digits = None
                    continue
                if self.escaped:
                    self.escaped = False
                    if character == "u":
                        self.unicode_digits = ""
                    else:
                        append(
                            {
                                '"': '"',
                                "\\": "\\",
                                "/": "/",
                                "b": "\b",
                                "f": "\f",
                                "n": "\n",
                                "r": "\r",
                                "t": "\t",
                            }[character]
                        )
                    continue
                if character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
                    if self.high_surrogate:
                        raise ValueError("模型正文含不完整的 Unicode 代理项")
                    if self.kind == "key":
                        self.key = json.loads(self.token)
                        self.phase = "colon"
                    elif self.depth == 1:
                        self.phase = "after"
                else:
                    append(character)
                continue
            if not self.started:
                # 容忍模型外包的 Markdown 围栏，与最终 JSON 解析行为保持一致。
                if character == "{":
                    self.started, self.depth = True, 1
                continue
            if character == '"':
                self.in_string, self.token = True, '"'
                self.kind = (
                    "key"
                    if self.depth == 1 and self.phase == "key"
                    else (
                        "public"
                        if self.depth == 1 and self.phase == "value" and self.key in self.fields
                        else "private"
                    )
                )
            elif character in "{[":
                self.depth += 1
            elif character in "}]":
                self.depth -= 1
                if self.depth == 0:
                    self.finished = True
                elif self.depth == 1:
                    self.phase = "after"
            elif self.depth == 1:
                if character == ":":
                    self.phase = "value"
                elif character == ",":
                    self.phase, self.key = "key", ""
        return chunks


class AgentTextStream:
    """一次模型轮次对应一个可撤销的临时文本流。"""

    def __init__(self, public_fields, role, emit):
        self.stream_id = str(uuid4())
        self.role, self.emit = role, emit
        self.parser = PublicJSONText(set(public_fields))
        self.active = bool(emit and public_fields)
        self.sent = False

    async def feed(self, delta):
        if not self.active:
            return
        try:
            chunks = self.parser.feed(delta)
        except (ValueError, KeyError):
            # 不完整或非法转义交给原有契约修复循环，不让展示解析打断模型读取。
            await self.reset()
            self.active = False
            return
        for field, content in chunks:
            self.sent = True
            await self.emit(
                "text_delta",
                {
                    "stream_id": self.stream_id,
                    "agent": self.role,
                    "field": field,
                    "delta": content,
                },
            )

    async def reset(self):
        if self.sent:
            await self.emit("text_reset", {"stream_id": self.stream_id})
            self.sent = False


class NativeTextEvents(AsyncCallbackHandler):
    """只公开最终结构化函数的白名单字段；业务参数及模型旁白均不透出。"""

    raise_error = True

    def __init__(self, context, output_name, fields):
        self.context, self.output_name, self.fields = context, output_name, fields
        self.streams, self.names = {}, {}
        self.last_stream = None
        self.first_delta_at = None

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        if metadata and metadata.get("tutor_agent_run_id", self.context.run_id) != self.context.run_id:
            return
        stream = AgentTextStream(self.fields, self.context.role, self.context.emit)
        self.streams[run_id], self.names[run_id] = stream, {}
        self.last_stream, self.first_delta_at = stream, None

    async def on_llm_new_token(self, token, *, chunk=None, run_id, **kwargs):
        message = getattr(chunk, "message", None)
        if message is None or run_id not in self.streams:
            return
        if self.first_delta_at is None and (message.content or message.tool_call_chunks):
            self.first_delta_at = time.perf_counter()
        names = self.names[run_id]
        for call in message.tool_call_chunks:
            index = call.get("index", 0)
            if call.get("name"):
                names[index] = names.get(index, "") + call["name"]
            if names.get(index) == self.output_name and call.get("args"):
                await self.streams[run_id].feed(call["args"])

    async def on_llm_error(self, error, *, run_id, **kwargs):
        if stream := self.streams.pop(run_id, None):
            await stream.reset()
        self.names.pop(run_id, None)

    async def on_llm_end(self, response, *, run_id, **kwargs):
        self.streams.pop(run_id, None)
        self.names.pop(run_id, None)

    async def reset(self):
        if self.last_stream:
            await self.last_stream.reset()
