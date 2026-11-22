"""验证题图确实进入模型请求，同时保留工具协议、课程范围和私有解析边界。"""

import asyncio
import base64
from copy import deepcopy
import json
from types import SimpleNamespace

from pydantic import BaseModel
import pytest
import httpx

from app.infrastructure.clients.llm import ModelClient
from app.application.agent.images import QuestionImageContext
from native_runtime_helpers import LoopState, run_agent_loop

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)


def question(identifier="math:7"):
    return {
        "id": identifier,
        "course_id": "math",
        "stem": "数一数图形 ![题图](/api/assets/math/question_7-image_0.png)",
        "options": {"A": "![选项](/api/assets/math/question_7-image_1.png)"},
        "images": [{"url": "/api/assets/math/question_7-image_0.png"}],
        "answer": ["秘密答案"],
        "explanation": "![解析](/api/assets/math/analysis_7-image_0.png)",
        "validation": {"images": ["analysis_7-image_0.png"]},
    }


@pytest.fixture
def image_root(tmp_path, monkeypatch):
    for name in ("question_7-image_0.png", "question_7-image_1.png", "analysis_7-image_0.png"):
        (tmp_path / name).write_bytes(PNG)
    monkeypatch.setattr("app.application.agent.images.MATH_IMAGES", tmp_path)
    return tmp_path


def images(parts):
    return [part for part in parts if part["type"] == "image_url"]


def test_initial_context_reads_stem_and_options_without_analysis(image_root):
    message = {"role": "user", "content": json.dumps({"question_contract": question()})}
    context = QuestionImageContext("math")
    context.enrich([message])
    assert len(images(message["content"])) == 2
    assert (
        base64.b64decode(images(message["content"])[0]["image_url"]["url"].split(",", 1)[1]) == PNG
    )
    assert "analysis_7-image_0.png" not in context.seen
    assert context.parts({"question": question()}) == []


def test_missing_and_invalid_images_are_explicit_not_silently_read(image_root):
    (image_root / "question_7-image_0.png").unlink()
    (image_root / "question_7-image_1.png").write_bytes(b"not a PNG")
    parts = QuestionImageContext("math").parts(question())
    assert not images(parts)
    assert "缺失" in parts[0]["text"]
    assert "不是受支持" in parts[1]["text"]


def test_scope_external_urls_and_traversal_are_not_images(image_root):
    assert QuestionImageContext("mechanics").parts(question()) == []
    assert QuestionImageContext("math").parts(question("mechanics:7")) == []
    q = question()
    q.update(stem="![外链](https://example.org/api/assets/math/question_7-image_0.png)", options={})
    q["images"] = [{"url": "/api/assets/math/../question_7-image_0.png"}]
    assert QuestionImageContext("math").parts(q) == []


def test_image_budget_is_reported_and_plain_text_stays_unchanged(image_root):
    q = question()
    q["options"] = {}
    q["stem"] = "数图形"
    q["images"] = []
    for index in range(17):
        filename = f"question_7-image_{index}.png"
        (image_root / filename).write_bytes(PNG)
        q["images"].append({"url": "/api/assets/math/" + filename})
    parts = QuestionImageContext("math").parts(q)
    assert len(images(parts)) == 16
    assert "超出本轮" in parts[-1]["text"]
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "user", "content": '{"goal":"学习计数"}'},
    ]
    original = deepcopy(messages)
    QuestionImageContext("math").enrich(messages)
    assert messages == original


class Answer(BaseModel):
    count: int


@pytest.mark.asyncio
async def test_tool_images_follow_all_tool_receipts_in_user_message(image_root):
    captured = []

    class Model:
        async def chat(self, messages, **kwargs):
            captured.append(deepcopy(messages))
            if len(captured) == 1:
                reply = {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": "get_session_exercise", "arguments": "{}"},
                        }
                        for i in range(2)
                    ],
                }
            else:
                reply = {"content": '{"count":3}'}
            return {"choices": [{"message": reply}]}

    class Tools:
        def definitions(self, role):
            return []

        async def execute(self, *args):
            return {"question": question()}, {"name": "get_session_exercise", "status": "completed"}

    await run_agent_loop(
        Model(), Tools(), "supervisor", [], Answer, SimpleNamespace(course_id="math"), LoopState()
    )
    messages = captured[-1]
    assert [m["role"] for m in messages if m["role"] != "system"] == ["assistant", "tool", "tool", "user"]
    assert len(images(messages[-1]["content"])) == 2
    assert all(
        isinstance(m["content"], str) and "base64" not in m["content"]
        for m in messages
        if m["role"] == "tool"
    )


@pytest.mark.asyncio
async def test_model_transport_keeps_initial_image_blocks(image_root):
    request = {}
    model = object.__new__(ModelClient)
    model.settings = SimpleNamespace(
        llm_model="deepseek-flash", llm_url="https://example.invalid", llm_key="test-only"
    )

    async def post(incoming):
        request.update(json.loads(incoming.content))
        return httpx.Response(200, json={"id": "image-probe", "object": "chat.completion", "created": 123,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"count":3}'}}]})

    model.http = httpx.AsyncClient(transport=httpx.MockTransport(post), trust_env=False)
    model.limit = asyncio.Semaphore(3)
    messages = [{"role": "user", "content": json.dumps({"question": question()})}]
    output = await run_agent_loop(
        model,
        None,
        "assessment",
        messages,
        Answer,
        SimpleNamespace(course_id="math"),
        LoopState(),
        use_tools=False,
    )
    assert output["count"] == 3
    await model.close()
    assert request["model"] == "deepseek-flash"
    assert request["thinking"] == {"type": "disabled"}
    assert request["response_format"] == {"type": "json_object"} and "tools" not in request
    assert len(images(next(item["content"] for item in request["messages"] if item["role"] == "user"))) == 2
