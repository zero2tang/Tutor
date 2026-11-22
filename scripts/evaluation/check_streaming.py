"""通过 Vite 代理和真实模型验证公开 SSE、持久化与会话释放。"""

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select

from app.infrastructure.persistence.database import db_session
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.assessment.models import Attempt, BatchSubmission
from app.domains.sessions.models import LearningSession, Message

REPORT = ROOT / "data/reports/streaming_acceptance.json"
PUBLIC_FIELDS = {
    "supervisor": {"message"},
    "knowledge": {"title", "content", "follow_up"},
    "exercise": {"content"},
    "assessment": {"feedback", "summary"},
}
INTERNAL_JSON = re.compile(
    r'"(?:action|rationale|reasoning_content|tool_calls|exercise_batch|predicted_correct|suggested_delta)"\s*:'
)
PRIVATE_QUESTION_FIELDS = {
    "answer", "explanation", "solution", "contract", "prediction", "_exercise_contract"
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def source_fingerprint():
    """记录验收时源码版本，避免把旧进程的结果误认为新实现结果。"""
    digest = hashlib.sha256()
    for directory, suffixes in ((ROOT / "backend/app", {".py", ".md"}), (ROOT / "frontend/src", {".ts", ".tsx", ".css"})):
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in suffixes:
                digest.update(path.relative_to(ROOT).as_posix().encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def assert_public_questions(value):
    """递归检查公开题目与消息元数据，不依赖短答案字符串的偶然出现。"""
    if isinstance(value, dict):
        assert not PRIVATE_QUESTION_FIELDS.intersection(value), "公开数据包含私有题目字段"
        assert not any(key.startswith("_") for key in value), "公开数据包含服务端私有快照"
        for item in value.values():
            assert_public_questions(item)
    elif isinstance(value, list):
        for item in value:
            assert_public_questions(item)


class StreamingAcceptance:
    def __init__(self, base_url, include_batch, resume_batch=False):
        self.base_url = base_url.rstrip("/")
        self.include_batch = include_batch
        self.resume_batch = resume_batch
        self.student_id = "evaluation_" + uuid4().hex[:12]
        self.report = {
            "evaluation_version": "v1",
            "started_at": utc_now(),
            "base_url": self.base_url,
            "transport": "real HTTP POST SSE via Vite /api proxy; real configured model",
            "student_id": self.student_id,
            "scope": "Only newly created evaluation student sessions receive mutations; local student is not used.",
            "source_sha256_before": source_fingerprint(),
            "cases": [],
            "complete": False,
        }
        if resume_batch:
            previous = json.loads(REPORT.read_text(encoding="utf-8"))
            assert previous["base_url"] == self.base_url and include_batch
            assert previous["source_sha256_before"] == source_fingerprint(), "源码已改变，不能复用旧验收"
            assert re.fullmatch(r"evaluation_[a-f0-9]{12}", previous["student_id"])
            completed = [case for case in previous["cases"] if case["id"] in {"S01", "S02", "S03"}]
            assert len(completed) == 3 and all(case["status"] == "passed" for case in completed)
            # 复用通过项之前保留原报告，修正验收任务时不覆盖失败事实。
            archive = REPORT.with_name("streaming_acceptance_before_batch_resume_" + uuid4().hex[:8] + ".json")
            archive.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.report = previous
            self.report.update(cases=completed, complete=False, resumed_at=utc_now(),
                               previous_batch_attempt_report=archive.relative_to(ROOT).as_posix())
            self.student_id = previous["student_id"]
        self.client = httpx.AsyncClient(
            base_url=self.base_url + "/api", timeout=httpx.Timeout(660, connect=10), trust_env=False
        )

    def save(self):
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def request(self, method, path, body=None):
        response = await self.client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()

    async def create_session(self, purpose):
        session = await self.request("POST", "/sessions", {
            "course_id": "math", "student_id": self.student_id, "concept_ids": ["math:c0"],
            "goal": "学习加法原理与乘法原理。" + purpose + "。作答后只给出反馈和下一步建议，等待我回复，不自动出新题。",
        })
        with db_session() as db:
            assert db.get(LearningSession, session["id"]).student_id == self.student_id
        return session["id"]

    async def read_stream(self, case, path, body):
        body = await native_request_body(self.client, "POST", path, body, _request_bindings)
        """按网络实际到达时间记录每帧；空行才提交完整 SSE 事件。"""
        started = time.perf_counter()
        frames, event, data, identifier = [], "message", [], None
        async with self.client.stream("POST", path + "?stream=true", json=body) as response:
            response.raise_for_status()
            assert response.headers.get("content-type", "").startswith("text/event-stream")
            assert response.headers.get("x-accel-buffering") == "no"
            case["response_headers"] = {key: response.headers[key] for key in ("content-type", "cache-control", "x-accel-buffering")}
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        frames.append({
                            "id": identifier, "event": event, "data": json.loads("\n".join(data)),
                            "elapsed_seconds": round(time.perf_counter() - started, 6), "received_at": utc_now(),
                        })
                        if event in {"agent_started", "agent_completed", "question", "exercise_batch", "done", "error"}:
                            print(case["id"], event, round(time.perf_counter() - started, 2), flush=True)
                    event, data, identifier = "message", [], None
                elif line.startswith(":"):
                    case["heartbeat_count"] = case.get("heartbeat_count", 0) + 1
                else:
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event = value
                    elif field == "id":
                        identifier = value
                    elif field == "data":
                        data.append(value)
        assert not data, "连接在完整 SSE 帧结束前关闭"
        case["duration_seconds"] = round(time.perf_counter() - started, 3)
        case["event_counts"] = dict(Counter(frame["event"] for frame in frames))
        case["timeline"] = [{
            "event": frame["event"], "id": frame["id"], "elapsed_seconds": frame["elapsed_seconds"],
            "received_at": frame["received_at"],
            **({key: frame["data"][key] for key in ("stream_id", "agent", "field", "delta") if key in frame["data"]}),
        } for frame in frames]
        return frames

    async def verify_case(self, case, sid, frames, require_deltas, final_event):
        errors = [frame["data"] for frame in frames if frame["event"] == "error"]
        assert not errors, "服务返回 SSE error: " + str(errors)[:400]
        assert frames and frames[-1]["event"] == "done", "最后一帧不是 done"
        assert frames[-1]["data"]["status"] == "completed"
        assert any(frame["event"] == final_event for frame in frames), "缺少最终业务事件 " + final_event
        turn_id = frames[-1]["data"]["turn_id"]
        case["turn_id"] = turn_id
        deltas = [frame for frame in frames if frame["event"] == "text_delta"]
        final = [frame for frame in frames if frame["event"] == final_event][-1]
        case["timing"] = {
            "first_public_text_seconds": deltas[0]["elapsed_seconds"] if deltas else None,
            "last_increment_seconds": deltas[-1]["elapsed_seconds"] if deltas else None,
            "final_event_seconds": final["elapsed_seconds"],
            "done_seconds": frames[-1]["elapsed_seconds"],
            "public_increment_span_seconds": round(deltas[-1]["elapsed_seconds"] - deltas[0]["elapsed_seconds"], 6) if deltas else None,
        }
        case["increment_count"] = len(deltas)
        case["increment_characters"] = sum(len(frame["data"]["delta"]) for frame in deltas)
        case["increment_roles"] = dict(Counter(frame["data"]["agent"] for frame in deltas))
        grouped, active = defaultdict(list), {}
        for frame in frames:
            data = frame["data"]
            if frame["event"] == "text_reset":
                active.pop(data["stream_id"], None)
            elif frame["event"] == "text_delta":
                assert set(data) == {"stream_id", "agent", "field", "delta"}
                assert data["field"] in PUBLIC_FIELDS[data["agent"]]
                assert isinstance(data["delta"], str) and data["delta"]
                assert frame["elapsed_seconds"] < frames[-1]["elapsed_seconds"]
                key = (data["stream_id"], data["agent"], data["field"])
                grouped[key].append(data["delta"])
                active.setdefault(data["stream_id"], {})[key] = grouped[key]
        for parts in grouped.values():
            assert not INTERNAL_JSON.search("".join(parts)), "公开正文出现内部 JSON 字段"
        if require_deltas:
            assert len(deltas) >= 2, "未多次收到公开文本增量"
            assert len({frame["elapsed_seconds"] for frame in deltas}) >= 2
            assert deltas[0]["elapsed_seconds"] < final["elapsed_seconds"], "正文直到最终事件才抵达"
            assert case["timing"]["public_increment_span_seconds"] >= 0.05, "公开正文抵达间隔不足50毫秒，无法排除整包缓冲"
            expected_roles = case.get("expected_stream_roles") or {
                "S01": {"knowledge", "supervisor"}, "S03": {"assessment", "supervisor"},
                "S04": {"supervisor"}, "S05": {"assessment", "supervisor"},
            }.get(case["id"], set())
            assert set(expected_roles).issubset(case["increment_roles"]), "缺少预期业务角色的公开正文"
        else:
            assert not deltas, "待答预测或选题的私有输出不应产生正文草稿"

        snapshot = await self.request("GET", f"/sessions/{sid}")
        assert snapshot["active_turn_id"] is None, "任务完成后会话仍被占用"
        public_messages = [frame["data"] for frame in frames if frame["event"] == "message"]
        stored = {message["id"]: message for message in snapshot["messages"]}
        for message in public_messages:
            assert stored[message["id"]] == message, "正式消息与刷新后的接口状态不同"
        final_text = "\n".join(message["content"] for message in public_messages if message["role"] == "assistant")
        for fields in active.values():
            for parts in fields.values():
                assert "".join(parts) in final_text, "未被撤销的完整草稿不在正式消息中"
        with db_session() as db:
            row = db.get(LearningSession, sid)
            assert row.student_id == self.student_id and row.active_turn_id is None
            for message in public_messages:
                actual = db.get(Message, message["id"])
                assert actual.session_id == sid and actual.content == message["content"]
            assert len({frame["id"] for frame in frames}) == len(frames)
            runs = list(db.scalars(select(AgentRun).where(AgentRun.turn_id == turn_id)))
            assert runs and all(run.status == "completed" for run in runs)
            assert sum(run.completion_tokens for run in runs) > 0, "真实模型用量未被记录"
            case["model_usage"] = {
                "models": sorted({run.model for run in runs}),
                "prompt_tokens": sum(run.prompt_tokens for run in runs),
                "completion_tokens": sum(run.completion_tokens for run in runs),
                "role_run_counts": dict(Counter(run.agent for run in runs)),
                "runs": [{"id": run.id, "agent": run.agent, "skill": run.skill, "duration_ms": round(run.duration_ms, 2),
                          "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens,
                          "tool_call_count": len(run.tool_calls)} for run in runs],
                "note": "Role runs may each include several model/tool rounds; token counts are provider reported.",
            }
        case["final_messages"] = [message for message in public_messages if message["role"] == "assistant"]
        case["persistence"] = {"durable_event_count": 0, "formal_messages_checked": len(public_messages),
                               "session_unlocked": True, "student_id_matches": True, "snapshot_phase": snapshot["phase"]}
        case["assertions"] = {"sse_finished": True, "business_event_received": True, "public_fields_only": True,
                              "internal_json_absent_in_text": True, "text_increments_before_final": bool(deltas),
                               "active_drafts_match_formal_messages": True, "formal_messages_persisted": True}
        return snapshot

    async def run_case(self, identifier, title, sid, operation, body, require_deltas, final_event, after=None):
        case = {"id": identifier, "title": title, "session_id": sid, "operation": operation, "started_at": utc_now(), "status": "running"}
        self.report["cases"].append(case)
        self.save()
        print(identifier, "开始", title, flush=True)
        try:
            frames = await self.read_stream(case, f"/sessions/{sid}/{operation}", body)
            snapshot = await self.verify_case(case, sid, frames, require_deltas, final_event)
            if after:
                after(case, snapshot, frames)
            case["status"] = "passed"
            print(identifier, "通过", case["increment_count"], "正文增量", flush=True)
            return snapshot
        except Exception as exc:
            case["status"] = "failed"
            case["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
            raise
        finally:
            case["finished_at"] = utc_now()
            self.save()

    async def single_cases(self):
        sid = await self.create_session("先讲解，然后按我指定的题目练习")
        await self.run_case("S01", "知识讲解在最终消息前持续抵达", sid, "messages", {
            "content": "先由知识点老师讲解加法原理和乘法原理的区别，用两段简短的例子说明，本轮不出题。讲完交回主老师，等我回复。",
            "intent": "teach", "request_id": str(uuid4()),
        }, True, "message")

        def check_practice(case, snapshot, frames):
            question = snapshot["pending_question"]
            assert question["id"] == "math:0"
            assert_public_questions(question)
            assert_public_questions(snapshot["messages"])
            for frame in frames:
                assert_public_questions(frame["data"])
            assert snapshot["phase"] == "awaiting_answer"
            case["assertions"]["unanswered_question_private_fields_absent"] = True
            case["public_text_expectation"] = "No public output: selection and pre-answer prediction remain private."

        await self.run_case("S02", "指定题目出题保留答案边界", sid, "practice", {
            "question_id": "math:0", "cognitive_type": "understanding",
        }, False, "question", check_practice)

        def check_single(case, snapshot, frames):
            result = next(frame["data"] for frame in frames if frame["event"] == "assessment_completed")
            assert result["result"] == "correct"
            assert not snapshot["pending_question"] and not snapshot["pending_batch"]
            with db_session() as db:
                attempt = db.get(Attempt, result["attempt_id"])
                assert attempt.session_id == sid and attempt.answer_text == "24" and attempt.result == "correct"
            case["assessment"] = {key: result[key] for key in ("attempt_id", "result", "assisted", "score")}
            case["assertions"]["attempt_persisted_and_pending_cleared"] = True

        await self.run_case("S03", "单题评估正文与主 Agent 接续均流式返回", sid, "answers", {
            "answer": "24", "request_id": str(uuid4()),
        }, True, "message", check_single)


    async def batch_cases(self):
        batch_sid = await self.create_session("使用一组两道原创选择题检验乘法原理")

        def check_batch(case, snapshot, frames):
            assert snapshot["pending_batch"] and len(snapshot["pending_batch"]["questions"]) == 2
            assert_public_questions(snapshot["pending_batch"])
            assert_public_questions(snapshot["messages"])
            for frame in frames:
                assert_public_questions(frame["data"])
            assert all(frame["data"].get("field") not in {"answer", "explanation"} for frame in frames if frame["event"] == "text_delta")
            assert set(case["increment_roles"]) == {"supervisor"}, "原创题生成、独立求解和复核不应发送私有正文"
            case["assertions"]["unanswered_batch_private_fields_absent"] = True

        snapshot = await self.run_case("S04", "原创练习组生成隐藏答案并公开正文", batch_sid, "messages", {
            "content": "请直接通过习题老师生成一组2道乘法原理选择题，不要讲解、不展示答案和解析。我独立答完整组后等我决定下一步。",
            "intent": "practice", "generate": True, "exercise_count": 2, "request_id": str(uuid4()),
        }, True, "message", check_batch)
        batch = snapshot["pending_batch"]
        answers = [{"question_id": question["id"], "answer": next(iter(question["options"]))} for question in batch["questions"]]

        def check_batch_answer(case, state, frames):
            result = next(frame["data"] for frame in frames if frame["event"] == "batch_assessment_completed")
            assert result["total"] == 2 and result["batch_id"] == batch["id"]
            assert not state["pending_question"] and not state["pending_batch"]
            with db_session() as db:
                saved = db.scalar(select(BatchSubmission).where(BatchSubmission.session_id == batch_sid))
                assert saved and saved.response == result
                assert all(db.get(Attempt, item["attempt_id"]).session_id == batch_sid for item in result["items"])
            case["assessment"] = {key: result[key] for key in ("batch_id", "total", "correct_count", "score")}
            case["submitted_answer_policy"] = "First displayed option; no private answer read for the generated question."
            case["assertions"]["batch_submission_and_attempt_persisted"] = True

        await self.run_case("S05", "整组提交汇总与主 Agent 接续流式返回", batch_sid, "batch-answers", {
            "batch_id": batch["id"], "answers": answers,
            "request_id": str(uuid4()),
        }, True, "message", check_batch_answer)

    async def main(self):
        try:
            health = await self.request("GET", "/health")
            assert health["application"] == "stem-tutor" and health["status"] == "ready"
            self.report["health"] = health
            if not self.resume_batch:
                await self.single_cases()
            if self.include_batch:
                await self.batch_cases()
            self.report["complete"] = True
        finally:
            await self.client.aclose()
            self.report["finished_at"] = utc_now()
            self.report["source_sha256_after"] = source_fingerprint()
            self.report["source_unchanged_during_run"] = self.report["source_sha256_before"] == self.report["source_sha256_after"]
            self.report["summary"] = {"passed": sum(case["status"] == "passed" for case in self.report["cases"]),
                                      "executed": len(self.report["cases"]), "planned": 5 if self.include_batch else 3,
                                      "prompt_tokens": sum(case.get("model_usage", {}).get("prompt_tokens", 0) for case in self.report["cases"]),
                                      "completion_tokens": sum(case.get("model_usage", {}).get("completion_tokens", 0) for case in self.report["cases"])}
            self.save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--skip-batch", action="store_true", help="仅执行知识讲解与指定单题链路")
    parser.add_argument("--resume-batch", action="store_true", help="源码相同时复用当前报告已通过的前三项，仅重跑批量链路")
    arguments = parser.parse_args()
    asyncio.run(StreamingAcceptance(arguments.base_url, not arguments.skip_batch, arguments.resume_batch).main())
