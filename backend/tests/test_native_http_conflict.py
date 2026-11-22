"""v2 协议冲突在普通 HTTP 返回409，SSE返回明确的冲突事件。"""

from types import SimpleNamespace

import httpx
import pytest

from app.main import app
from app.shared.execution import LearningConflict


@pytest.mark.asyncio
async def test_http_and_sse_expose_stale_wait_conflict(monkeypatch):
    async def turn(*args):
        raise LearningConflict("等待点已过期")

    monkeypatch.setattr(app.state, "services", {"sessions": SimpleNamespace(get=lambda sid: {}),
        "learning": SimpleNamespace(turn=turn)}, raising=False)
    monkeypatch.setattr(app.state, "tasks", set(), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"content": "回答当前问题", "request_id": "stale-http-wait", "wait_id": "stale"}
        response = await client.post("/api/sessions/s/messages?stream=false", json=body)
        assert response.status_code == 409 and "等待点" in response.json()["detail"]
        streamed = await client.post("/api/sessions/s/messages?stream=true", json=body)
        assert streamed.status_code == 200
        assert 'event: error' in streamed.text and '"status_code": 409' in streamed.text
        missing = await client.post("/api/sessions/s/answers?stream=false", json={"answer": "原答", "idempotency_key": "old-client"})
        assert missing.status_code == 422
