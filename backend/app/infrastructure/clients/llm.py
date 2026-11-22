"""真实模型接口与可复用向量缓存。"""

import asyncio
import hashlib
import json
import math
from pathlib import Path
import time

import httpx

from app.infrastructure.clients.llm_metrics import ModelRequestMetrics
from app.infrastructure.clients.llm_streaming import (
    ContentCallback,
    ModelStreamError,
    StreamAccumulator,
)
from app.infrastructure.config import ROOT, get_settings
from app.shared.llm_budget import ProviderContextLimitError, assert_request_budget


class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self) -> None:
        """创建共享 HTTP 连接、向量缓存目录和最多 3 个并发模型请求的限流器。"""
        self.settings = get_settings()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(100, connect=15), trust_env=False)
        self.cache = ROOT / "data" / "cache" / "embeddings"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.limit = asyncio.Semaphore(3)

    @staticmethod
    def _raise_http_error(response):
        # 只识别错误类别，不把服务商响应中的密钥或学生文本传播到日志。
        error = response.text.lower()
        if response.status_code in (400, 413, 422) and any(
            marker in error for marker in (
                "context_length_exceeded", "maximum context length", "context window",
                "input token limit", "too many input tokens",
            )
        ):
            raise ProviderContextLimitError("服务商拒绝上下文长度，需要重建请求并校准部署预算。")
        raise ModelError(f"模型服务返回 HTTP {response.status_code}")

    async def _post(
        self, url: str, key: str, body: dict, *, metrics: ModelRequestMetrics | None = None,
    ) -> dict:
        """发送带鉴权的模型请求，对临时服务错误或网络异常最多尝试 3 次。

        请求仅在实际发送时占用并发额度；错误只暴露状态码或异常类型，
        不把密钥和完整响应输出到业务错误信息中。
        """
        if not url or not key:
            raise ModelError("模型地址或密钥未配置")
        for attempt in range(3):
            try:
                queued_at = time.perf_counter()
                async with self.limit:
                    if metrics is not None:
                        metrics.request_started(queued_at)
                    response = await self.http.post(
                        url, headers={"Authorization": f"Bearer {key}"}, json=body
                    )
                if response.status_code in (429, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                if response.is_error:
                    self._raise_http_error(response)
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2:
                    raise ModelError(f"模型请求失败：{type(exc).__name__}") from exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise ModelError("模型请求重试耗尽")

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list | None = None,
        json_output: bool = False,
        required_tool: bool = False,
        thinking: bool | None = None,
        max_tokens: int = 3000,
        on_content: ContentCallback | None = None,
    ) -> dict:
        """构造对话模型请求，按调用阶段提供工具定义或要求 JSON 输出。

        提供工具时使用 auto 让模型自行选择；此方法只转发模型请求，
        实际工具执行、结果回填和最终结构校验由 runtime/loop.py 完成。
        提供 on_content 时逐片回调正文，同时仍返回完整响应供后续处理。
        thinking 参数仅保留调用兼容性；当前所有对话请求统一关闭思考模式。
        """
        # 所有调用（包括记忆提取、摘要和JSON修复）都经过最后一道输入守卫。
        budget, estimate = assert_request_budget(
            self.settings, messages, tools, max_tokens=max_tokens, json_output=json_output
        )
        metrics = ModelRequestMetrics()
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.25,
            "stream": on_content is not None,
            # 在共享出口显式关闭，避免服务端默认开启或旧调用参数重新打开。
            "thinking": {"type": "disabled"},
        }
        if tools:
            # 由模型自主选择是否调用工具；不强制普通沟通和澄清产生检索。
            body.update(tools=tools, tool_choice="auto")
        elif json_output:
            body["response_format"] = {"type": "json_object"}
        if on_content is not None:
            body["stream_options"] = {"include_usage": True}

            async def measured_content(content: str) -> None:
                metrics.content_received()
                await on_content(content)

            result = await self._stream_chat(body, measured_content, metrics=metrics)
        else:
            result = await self._post(
                self.settings.llm_url + "/chat/completions", self.settings.llm_key, body,
                metrics=metrics,
            )
        result["_request_metrics"] = metrics.snapshot(body, result.get("usage"))
        context_metrics = {
            **estimate.as_dict(), "input_budget": budget.available,
            "estimated_minus_actual_tokens": (
                estimate.tokens - result["usage"]["prompt_tokens"]
                if isinstance((result.get("usage") or {}).get("prompt_tokens"), int)
                else None
            ),
        }
        if hasattr(self.settings, "context_window_tokens"):
            result["_context_metrics"] = context_metrics
        return result

    async def _stream_chat(
        self, body: dict, on_content: ContentCallback, *, metrics: ModelRequestMetrics,
    ) -> dict:
        """边接收边转发正文；收到任何模型增量后禁止重放请求。"""
        if not self.settings.llm_url or not self.settings.llm_key:
            raise ModelError("模型地址或密钥未配置")
        for attempt in range(3):
            accumulator = StreamAccumulator()
            try:
                queued_at = time.perf_counter()
                async with self.limit:
                    metrics.request_started(queued_at)
                    async with self.http.stream(
                        "POST",
                        self.settings.llm_url + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.llm_key}"},
                        json=body,
                    ) as response:
                        if response.status_code in (429, 502, 503, 504) and attempt < 2:
                            retry_status = True
                        else:
                            retry_status = False
                            if response.is_error:
                                await response.aread()
                                self._raise_http_error(response)
                            result = await accumulator.consume(response, on_content)
                            metrics.first_delta_at = accumulator.first_increment_at
                            return result
                if retry_status:
                    await asyncio.sleep(0.6 * (attempt + 1))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if accumulator.has_increment or attempt == 2:
                    raise ModelError(f"模型流请求失败：{type(exc).__name__}") from exc
                await asyncio.sleep(0.5 * (attempt + 1))
            except ModelStreamError as exc:
                raise ModelError(str(exc)) from exc
        raise ModelError("模型请求重试耗尽")

    def embedding_key(self, text: str) -> str:
        """用模型名、向量维度、缓存版本与完整文本生成键，隔离不同向量配置。"""
        value = f"{self.settings.embedding_model}:{self.settings.embedding_dim}:{self.settings.embedding_url}:v2:{text}"
        return hashlib.sha256(value.encode()).hexdigest()

    async def embed(self, texts: list[str], *, receipt: dict | None = None) -> list[list[float]]:
        """按输入顺序返回向量，优先读取缓存，只为缺失文本分批请求服务。

        每批最多 10 条，请求文本最多 15000 字符；检查数量、维度和有限值，
        通过后原子替换缓存文件。索引与在线检索复用同一缓存和模型配置。
        """
        if receipt is not None:
            receipt.update(model=self.settings.embedding_model, dimensions=self.settings.embedding_dim, cache_hits=0, batches=[])
        vectors: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str, Path]] = []
        for index, text in enumerate(texts):
            path = self.cache / (self.embedding_key(text) + ".json")
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                if len(value) == self.settings.embedding_dim:
                    vectors[index] = value
                    if receipt is not None:
                        receipt["cache_hits"] += 1
                    continue
            missing.append((index, text[:15000], path))
        for offset in range(0, len(missing), 10):
            batch = missing[offset : offset + 10]
            body = {
                "model": self.settings.embedding_model,
                "input": [row[1] for row in batch],
                "dimensions": self.settings.embedding_dim,
                "encoding_format": "float",
            }
            metrics = ModelRequestMetrics() if receipt is not None else None
            result = await self._post(
                self.settings.embedding_url + "/embeddings",
                self.settings.embedding_key,
                body,
                **({"metrics": metrics} if metrics is not None else {}),
            )
            if receipt is not None:
                receipt["batches"].append({"usage": result.get("usage", {}),
                    "request_metrics": metrics.snapshot({"model": self.settings.embedding_model, "thinking": {"type": "disabled"}, "stream": False}, result.get("usage"))})
            data = sorted(result.get("data", []), key=lambda row: row["index"])
            if len(data) != len(batch):
                raise ModelError("向量服务返回数量与请求不一致")
            for (index, _, path), row in zip(batch, data, strict=True):
                vector = row["embedding"]
                if (
                    len(vector) != self.settings.embedding_dim
                    or not all(math.isfinite(x) for x in vector)
                    or not any(vector)
                ):
                    raise ModelError("向量维度或数值无效")
                # 临时文件名唯一，避免并发查询写入半个 JSON。
                tmp = path.with_suffix(f".{time.time_ns()}.tmp")
                tmp.write_text(json.dumps(vector), encoding="utf-8")
                tmp.replace(path)
                vectors[index] = vector
        return vectors  # type: ignore[return-value]

    async def embed_tracked(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        receipt = {}
        vectors = await self.embed(texts, receipt=receipt)
        return vectors, receipt

    async def close(self) -> None:
        """应用退出时关闭模型 HTTP 连接池，释放网络资源。"""
        try:
            if native := getattr(self, "_native_chat_model", None):
                native.root_client.close()
                await native.root_async_client.close()
        finally:
            await self.http.aclose()
