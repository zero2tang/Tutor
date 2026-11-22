"""后台记忆使用真实标准模型出口，来源修复仍由业务校验控制。"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.domains.memory.concepts.observations import ObservationExtractor
from app.domains.memory.concepts.service import BusinessMemoryPipeline
from app.domains.memory.scopes.promotion import empty_metrics, json_call
from test_model_streaming import client, use_transport


def completion(request, value, *, usage=True):
    body = json.loads(request.content)
    assert body["response_format"] == {"type": "json_object"} and "tools" not in body
    response = {"id": "memory-native", "object": "chat.completion", "created": 123,
        "model": "fake-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(value, ensure_ascii=False)}}]}
    if usage:
        response["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    return httpx.Response(200, json=response)


@pytest.mark.asyncio
async def test_observation_native_schema_keeps_bounded_source_repair(client):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        quote = "不存在的原话" if len(calls) == 1 else "提示后我改成2x+2"
        return completion(request, {"observations": [{"concept_id": "c1", "claims": [{
            "text": "提示后修正漏乘，仍需独立验证", "source_refs": ["message:a"],
            "span_refs": [{"source_ref": "message:a", "quote": quote}]}]}]})

    await use_transport(client, handler)
    fact = {"seq": 1, "source_ref": "message:a", "kind": "message",
        "payload": {"role": "user", "content": "提示后我改成2x+2"}}
    value, metrics = await ObservationExtractor(client).extract(facts=[fact], all_facts=[fact],
        concepts=[{"id": "c1"}], planned_concepts=[], observations=[], preferences=[],
        experience={}, course_id="math")
    assert len(calls) == metrics["model_calls"] == 2
    assert metrics["prompt_tokens"] == 200 and metrics["completion_tokens"] == 100
    assert '"title": "Extraction"' in calls[0]["messages"][-1]["content"]
    assert "候选未通过校验" in calls[1]["messages"][-2]["content"]
    assert value["observations"][0]["claims"][0]["source_refs"] == ["message:a"]


@pytest.mark.asyncio
async def test_concept_aggregation_native_output_cannot_invent_sources(client):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return completion(request, {"claims": [{"text": "保留有来源的观察",
            "source_refs": ["message:foreign"]}]}, usage=False)

    await use_transport(client, handler)
    source = {"claim_id": "child", "text": "提示后修正", "source_refs": ["message:a"],
        "counter_source_refs": [], "span_refs": [], "source_kind": "model_observation",
        "help_condition": "unknown"}
    snapshot = {"inputs": [{"claims": [source]}], "old_claims": []}
    pipeline = BusinessMemoryPipeline(client, SimpleNamespace())
    with pytest.raises(ValueError, match="候选校验失败"):
        await pipeline._aggregate_batch(snapshot, "math", "c1")
    assert len(calls) == snapshot["metrics"]["usage_unavailable_calls"] == 2
    assert '"title": "AggregatedClaims"' in calls[0]["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,value,name", [
    ("aggregation", {"no_change": True}, "PromotionCandidate"),
    ("review", {"decisions": [{"claim_id": "c", "decision": "reject", "reason": "范围不足"}]}, "PromotionReview"),
])
async def test_upper_memory_uses_typed_json_and_existing_usage(client, phase, value, name):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return completion(request, value)

    await use_transport(client, handler)
    metrics = empty_metrics()
    result = await json_call(client, [{"role": "user", "content": "核对来源"}], metrics, phase=phase)
    assert result.get("no_change") is True if phase == "aggregation" else result["decisions"][0]["decision"] == "reject"
    assert f'"title": "{name}"' in calls[0]["messages"][-1]["content"]
    assert metrics["model_calls"] == metrics[phase + "_calls"] == 1
    assert metrics["prompt_tokens"] == 100 and metrics["completion_tokens"] == 50
