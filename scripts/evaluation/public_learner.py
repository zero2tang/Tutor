"""仅根据公开题干和已发布对话续写学生发言，不读取教学私有状态。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

from app.infrastructure.clients.llm import ModelClient


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class PublicLearner:
    """反转公开师生角色续写学生，模型请求没有系统提示、工具或隐藏答案。"""

    def __init__(self):
        self.client = ModelClient()
        self.calls: list[dict] = []

    async def answer(self, stem: str, dialogue: list[dict]) -> str:
        # 教师公开发言作为user，学生公开发言作为assistant；模型续写下一次学生发言。
        messages = [{"role": "user", "content": "公开题目：\n" + stem}]
        messages.extend(
            {
                "role": "user" if item["speaker"] == "teacher" else "assistant",
                "content": item["content"],
            }
            for item in dialogue
        )
        assert dialogue and dialogue[-1]["speaker"] == "teacher"
        assert all(set(item) == {"role", "content"} for item in messages)
        # 保存真正发送的完整请求体；鉴权凭据不属于内容，也不会写入审计文件。
        payload = {
            "model": self.client.settings.llm_model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.25,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        audit = {
            "index": len(self.calls) + 1,
            "input_origin": "public_problem_and_published_dialogue_only",
            "role_mapping": {"teacher": "user", "student": "assistant"},
            "payload": deepcopy(payload),
            "payload_sha256": canonical_hash(payload),
            "system_messages": 0,
            "tools": 0,
            "private_state_fields": 0,
        }
        self.calls.append(audit)
        started = time.perf_counter()
        try:
            result = await self.client.chat(messages, max_tokens=2000)
            message = result["choices"][0]["message"]
            assert not message.get("tool_calls")
            content = message["content"].strip()
            assert content, "隔离learner返回空内容"
            audit.update(
                output=content,
                output_sha256=canonical_hash(content),
                usage=result.get("usage", {}),
                request_metrics=result.get("_request_metrics", {}),
                status="completed",
            )
            return content
        except Exception as exc:
            audit.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            audit["wall_time_ms"] = round((time.perf_counter() - started) * 1000, 3)

    def usage(self) -> dict:
        completed = [item for item in self.calls if item.get("status") == "completed"]
        attempts = [
            item.get("request_metrics", {}).get("request_attempts")
            for item in completed
        ]
        measured = [value for value in attempts if value is not None]
        return {
            "requests": len(self.calls),
            "completed_requests": len(completed),
            "failed_requests": len(self.calls) - len(completed),
            "prompt_tokens": sum(
                item.get("usage", {}).get("prompt_tokens", 0) for item in completed
            ),
            "completion_tokens": sum(
                item.get("usage", {}).get("completion_tokens", 0) for item in completed
            ),
            "upstream_request_attempts": sum(measured),
            "upstream_retry_count": sum(max(0, value - 1) for value in measured),
            "requests_missing_attempt_metrics": len(completed) - len(measured),
        }

    async def close(self):
        await self.client.close()
