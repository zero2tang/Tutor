"""模型请求的安全观测数据，不包含凭据、消息内容或推理正文。"""

from dataclasses import dataclass, field
import time


@dataclass
class ModelRequestMetrics:
    """用单调时钟区分并发排队和上游生成时间，重试时累计排队耗时。"""

    started_at: float = field(default_factory=time.perf_counter)
    queue_wait_ms: float = 0.0
    request_attempts: int = 0
    first_delta_at: float | None = None
    first_content_at: float | None = None

    def request_started(self, queued_at: float) -> None:
        self.queue_wait_ms += (time.perf_counter() - queued_at) * 1000
        self.request_attempts += 1

    def content_received(self) -> None:
        if self.first_content_at is None:
            self.first_content_at = time.perf_counter()

    def _elapsed_ms(self, timestamp: float | None) -> float | None:
        if timestamp is None:
            return None
        return round((timestamp - self.started_at) * 1000, 3)

    def snapshot(self, body: dict, usage: dict | None) -> dict:
        """仅导出允许的请求配置、计时与计数；缺失计数保留为未知。"""
        usage = usage if isinstance(usage, dict) else {}
        prompt_details = usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        completion_details = usage.get("completion_tokens_details")
        completion_details = completion_details if isinstance(completion_details, dict) else {}
        return {
            "model": body["model"],
            "thinking": body["thinking"]["type"],
            "stream": body["stream"],
            "duration_ms": self._elapsed_ms(time.perf_counter()),
            "queue_wait_ms": round(self.queue_wait_ms, 3),
            "request_attempts": self.request_attempts,
            "first_delta_ms": self._elapsed_ms(self.first_delta_at),
            "first_content_ms": self._elapsed_ms(self.first_content_at),
            "prompt_cache_hit_tokens": usage.get(
                "prompt_cache_hit_tokens", prompt_details.get("cached_tokens")
            ),
            "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
            "reasoning_tokens": completion_details.get("reasoning_tokens"),
        }
