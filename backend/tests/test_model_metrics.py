"""请求计时和用量白名单的确定性测试。"""

import pytest

from app.infrastructure.clients.llm_metrics import ModelRequestMetrics


def test_metrics_accumulate_queue_wait_and_keep_first_content(monkeypatch):
    metrics = ModelRequestMetrics(started_at=5.0)
    timestamps = iter([5.1, 5.3, 5.8, 6.0])
    monkeypatch.setattr("app.infrastructure.clients.llm_metrics.time.perf_counter", lambda: next(timestamps))

    metrics.request_started(5.0)
    metrics.request_started(5.2)
    metrics.first_delta_at = 5.5
    metrics.content_received()
    metrics.content_received()
    snapshot = metrics.snapshot({
        "model": "fake-model", "thinking": {"type": "disabled"}, "stream": True,
        "messages": [{"role": "user", "content": "不应写入计时数据"}],
    }, {
        "prompt_tokens_details": {"cached_tokens": 128},
        "completion_tokens_details": {"reasoning_tokens": 0},
        "unexpected_field": "不应透传到计时元数据",
    })

    assert snapshot == {
        "model": "fake-model", "thinking": "disabled", "stream": True,
        "duration_ms": 1000.0, "queue_wait_ms": 200.0, "request_attempts": 2,
        "first_delta_ms": 500.0, "first_content_ms": 800.0,
        "prompt_cache_hit_tokens": 128, "prompt_cache_miss_tokens": None,
        "reasoning_tokens": 0,
    }


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens_details": 3,
                                              "completion_tokens_details": []}])
def test_missing_or_incomplete_usage_does_not_break_successful_response(usage):
    snapshot = ModelRequestMetrics().snapshot({
        "model": "fake-model", "thinking": {"type": "disabled"}, "stream": False,
    }, usage)
    assert snapshot["prompt_cache_hit_tokens"] is None
    assert snapshot["prompt_cache_miss_tokens"] is None
    assert snapshot["reasoning_tokens"] is None
