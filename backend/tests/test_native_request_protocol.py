"""验收客户端绑定公开等待点，不能把旧答案误投给新题。"""

import importlib.util
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location("request_protocol", Path(__file__).resolve().parents[2]
    / "scripts/evaluation/request_protocol.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
request_body = _module.request_body


@pytest.mark.asyncio
async def test_replay_retains_first_public_wait_and_full_answer():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"pending_interaction": {
            "wait_id": "first" if len(calls) == 1 else "later", "question_id": "q1"}})

    async with httpx.AsyncClient(base_url="http://test/api", transport=httpx.MockTransport(handler)) as client:
        bindings = {}
        body = {"answer": "  原答\n2x+1  ", "request_id": "request-a"}
        first = await request_body(client, "POST", "/sessions/s/answers?stream=false", body, bindings)
        repeated = await request_body(client, "POST", "/sessions/s/answers?stream=false", body, bindings)
    assert first == repeated == {**body, "wait_id": "first", "question_id": "q1"}
    assert len(calls) == 1 and calls[0].url.path == "/api/sessions/s"


@pytest.mark.asyncio
async def test_missing_public_wait_rejects_answer_and_skip():
    async with httpx.AsyncClient(base_url="http://test/api", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"pending_question": {"id": "must-not-guess"}}))) as client:
        for operation in ("answers", "batch-answers", "skip"):
            with pytest.raises(AssertionError, match="不能猜测"):
                await request_body(client, "POST", f"/sessions/s/{operation}", {"request_id": operation}, {})


@pytest.mark.asyncio
async def test_existing_practice_has_request_id_and_no_foreign_wait():
    async with httpx.AsyncClient(base_url="http://test/api", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"pending_interaction": {"wait_id": "active"}}))) as client:
        result = await request_body(client, "POST", "/sessions/s/practice", {"question_id": "q"}, {})
    assert result["request_id"] and "wait_id" not in result
