"""真实HTTP→业务→MySQL→配置模型验收；全部学生输入明确为脚本模拟。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from business_memory_audit import DATABASE, digest, latency_breakdown, read_audit, seed_catalog, source_digest, usage_totals

FIRST_ANSWER = "这是脚本模拟作答。第1问：2(x+3)=2x+6，因为2同时乘x和3。第2问：我只在左边减4，右边仍是9，得到x=9。没有第3问，分数运算本次没有作答。"
COUNTER_ANSWER = "这是另一会话的脚本模拟作答。第1问：3(x+2)=3x+2，我只把3乘了x。第2问：等式两边都减5，所以x+5-5=11-5，得到x=6。没有第3问，分数运算本次没有作答。"
PREFERENCES_A = "这是合成工程验收的模拟学生原话。无论哪门课程，我长期希望先用简短中文解释，再给一个检查问题，不直接替我提交答案。在本代数课程，我长期希望先列符号式，再对照文字说明，不要优先画图。请只确认这些持续偏好，暂不讲课、不出新题。"
PREFERENCES_B = "这是合成工程验收的模拟学生原话。只在本物理课程，我长期希望先看示意图，再写公式；这项物理课偏好不替代我的代数课偏好。请只确认，不讲课、不出题。"
CORRECTION_A = "这是脚本模拟学生的明确纠正：仅在本代数课程，以后长期先画简图，再对照符号；不再把‘先列符号式、不优先画图’作为本代数课现行偏好。其它课程和跨课程通用偏好不变。"


class EvaluationPaused(RuntimeError):
    """人工暂停不是业务断言失败；当前请求结束后停止提交新请求。"""


class EvaluationCheckFailed(RuntimeError):
    """已保存首个真实失败，停止后续模型操作，等待修复后完整重跑。"""


def ensure(condition, message, detail=None):
    if not condition:
        suffix = "：" + json.dumps(detail, ensure_ascii=False, default=str)[:1800] if detail is not None else ""
        raise AssertionError(message + suffix)


def concept(snapshot, concept_id):
    return next((row for row in snapshot.get("concept_memories", []) if row.get("concept_id") == concept_id), None)


def semantic_scope(row):
    return {key: row.get(key) for key in ("scope_key", "content", "claims", "source_refs", "version", "status")} if row else None


class Harness:
    def __init__(self, args):
        self.args = args
        self.student = "evaluation_" + uuid4().hex[:12]
        self.report = {"started_at": datetime.now(timezone.utc).isoformat(), "student_id": self.student,
            "database": args.database, "base_url": args.base_url, "backend_source_sha256_before": source_digest(),
            "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "audit_helper_sha256": sha256(Path(__file__).with_name("business_memory_audit.py").read_bytes()).hexdigest(),
            "data_origin": "disclosed_synthetic_catalog_and_scripted_student_responses; no private answer read to generate student text",
            "execution": {"http_concurrency": 1, "http_timeout_seconds": 450, "http_mutation_retries": 0,
                "model_mocking": False, "direct_learning_record_writes": 0,
                "database_write_scope": "synthetic Course/Concept/Question only", "background_workers_may_overlap": True},
            "sessions": {}, "checks": [], "requests": [], "turns": [], "complete": False,
            "not_tested": ["真实学生学习增益", "评分准确率的人工标注评测", "长期保持", "容量与费用基准", "多worker吞吐"]}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        if args.report.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup = args.report.with_name(args.report.stem + "_previous_" + stamp + ".json")
            backup.write_bytes(args.report.read_bytes())
        self.client = httpx.AsyncClient(base_url=args.base_url.rstrip("/") + "/api", timeout=450, trust_env=False)

    def save(self):
        self.args.report.write_text(json.dumps(self.report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        lines = ["业务分层记忆真实后端验收", "学生输入和目录均为明确合成的工程夹具；教学、评估和记忆由真实配置模型执行。", ""]
        for turn in self.report["turns"]:
            lines.extend([turn["label"], "模拟学生：" + turn["student_text"], "后端结果：" + json.dumps(turn.get("result"), ensure_ascii=False), ""])
        self.args.report.with_name(self.args.report.stem + "_dialogue.txt").write_text("\n".join(lines), encoding="utf-8")

    async def request(self, method, path, body=None):
        body = await native_request_body(self.client, method, path, body, _request_bindings)
        if self.args.report.with_suffix(".pause").exists():
            raise EvaluationPaused("root_requested_pause_between_HTTP_requests")
        entry = {"method": method, "path": path, "attempt": 1, "body": body}
        started = time.perf_counter()
        try:
            response = await self.client.request(method, path, json=body)
            entry["status"] = response.status_code
            if response.status_code != 200:
                entry["failure_body"] = response.text[:1800]
                raise RuntimeError(f"真实HTTP {method} {path} 返回{response.status_code}：{response.text[:1000]}")
            result = response.json()
            if method != "GET":
                entry["result"] = result
            return result
        except Exception as exc:
            entry["error"] = type(exc).__name__
            entry["error_detail"] = str(exc)[:1800]
            raise
        finally:
            entry["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self.report["requests"].append(entry)
            self.save()

    async def audit(self):
        return await asyncio.to_thread(read_audit, self.student, list(self.report["sessions"].values()))

    async def check(self, label, operation):
        started = time.perf_counter()
        try:
            detail = await operation()
            self.report["checks"].append({"label": label, "status": "passed", "details": detail,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            print("PASS " + label, flush=True)
        except EvaluationPaused:
            raise
        except Exception as exc:
            self.report["checks"].append({"label": label, "status": "failed", "error": type(exc).__name__,
                "detail": str(exc)[:2400], "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            print("FAIL " + label + ": " + str(exc)[:300], flush=True)
            raise EvaluationCheckFailed(label) from exc
        finally:
            self.save()

    async def create(self, name, course, ids):
        data = await self.request("POST", "/sessions", {"student_id": self.student, "course_id": course,
            "concept_ids": ids, "goal": "脚本模拟学生的独立会话：按当前原答检查局部概念，完成后等待我下一次输入，不主动出题。"})
        self.report["sessions"][name] = data["id"]
        self.report.setdefault("initial_goal_states", {})[name] = data.get("session_goals")
        self.save()
        return data

    async def message(self, sid, text, ids=None):
        turn = {"label": "真实messages入口", "session_id": sid, "student_text": text}
        self.report["turns"].append(turn)
        data = await self.request("POST", f"/sessions/{sid}/messages?stream=false", {"content": text,
            "concept_ids": ids or [], "request_id": str(uuid4()), "intent": "auto"})
        turn["result"] = data
        return data

    async def practice_and_answer(self, sid, qid, text):
        snapshot = await self.request("GET", f"/sessions/{sid}")
        if snapshot.get("pending_question") or snapshot.get("pending_batch"):
            await self.request("POST", f"/sessions/{sid}/skip")
        await self.request("POST", f"/sessions/{sid}/practice?stream=false", {"question_id": qid, "cognitive_type": "understanding"})
        body = {"answer": text, "request_id": str(uuid4())}
        turn = {"label": "真实answers入口", "session_id": sid, "student_text": text}
        self.report["turns"].append(turn)
        data = await self.request("POST", f"/sessions/{sid}/answers?stream=false", body)
        turn["result"] = data
        return data, body

    async def settle(self, sid, *, refresh=True, ignore_failed_scope_job_ids=None):
        session = await self.request("GET", f"/sessions/{sid}")
        if session.get("pending_question") or session.get("pending_batch"):
            # 接续Agent可以提出后续题；本脚本只核验已提交题，跳过不产生新作答。
            self.report.setdefault("skipped_followup_questions", []).append({"session_id": sid,
                "question_id": (session.get("pending_question") or {}).get("id"),
                "batch_id": (session.get("pending_batch") or {}).get("id"),
                "reason": "inspect_committed_memory_after_skipping_unanswered_followup_via_HTTP"})
            await self.request("POST", f"/sessions/{sid}/skip")
        initial = await self.request("POST", f"/sessions/{sid}/memory/refresh?wait=true", {"close": False}) if refresh else {}
        target_id, target_seq = initial.get("job_id"), initial.get("target_seq", 0)
        deadline, stable, previous = time.monotonic() + 450, 0, None
        while True:
            snapshot = await self.request("GET", f"/sessions/{sid}/memory")
            jobs = snapshot.get("jobs", [])
            target = next((row for row in jobs if row["id"] == target_id), None)
            completed = any(row["status"] == "completed" and row.get("target_seq", 0) >= target_seq for row in jobs)
            if target and target["status"] == "failed":
                raise AssertionError("目标模型记忆任务失败：" + json.dumps(target, ensure_ascii=False))
            latest_scopes = {}
            for row in snapshot.get("scope_jobs", []):
                previous_job = latest_scopes.get(row["scope_key"])
                if previous_job is None or row.get("source_generation", 0) > previous_job.get("source_generation", 0):
                    latest_scopes[row["scope_key"]] = row
            failed_scopes = [row for row in latest_scopes.values() if row["status"] == "failed"
                and row["id"] not in set(ignore_failed_scope_job_ids or [])]
            if failed_scopes:
                raise AssertionError("知识点或上层聚合任务已达到失败终态，不继续无效轮询：" + json.dumps(failed_scopes, ensure_ascii=False)[:1800])
            active = [row for row in jobs + list(latest_scopes.values()) if row["status"] in {"pending", "retry", "running"}]
            signature = digest({key: snapshot.get(key) for key in ("global_memory", "course_memory", "concept_memories", "jobs", "scope_jobs")})
            covered = (snapshot.get("session_experience") or {}).get("covered_seq", 0) >= snapshot.get("fact_seq", 0)
            ready = not active and covered and (completed or not target_id)
            stable = stable + 1 if ready and signature == previous else 0
            if stable >= 2:
                return snapshot
            previous = signature
            if time.monotonic() >= deadline:
                raise TimeoutError("后台模型任务未收敛；没有重复触发提取：" + json.dumps({"target_job": target_id, "active": active}, ensure_ascii=False)[:1800])
            await asyncio.sleep(2)


async def main(args):
    parsed = urlparse(args.base_url)
    ensure(parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 8012,
        "验收只允许本机独立8012端口，拒绝对工作台执行合成学习")
    ensure(args.database == DATABASE, "验收只允许独立业务记忆数据库")
    os.environ["TUTOR_DATABASE"] = args.database
    harness = Harness(args)
    report = harness.report
    context = {}
    try:
        await harness.request("GET", "/health")
        fixture = await asyncio.to_thread(seed_catalog, uuid4().hex[:10], args.database)
        report["catalog_fixture"] = fixture
        ids, courses, questions = fixture["concepts"], fixture["courses"], fixture["questions"]
        first = await harness.create("first", courses["algebra"], [ids["distribution"], ids["equality"], ids["unobserved"]])
        sid = first["id"]
        await harness.message(sid, PREFERENCES_A)

        async def local_attribution():
            response, body = await harness.practice_and_answer(sid, questions["first"], FIRST_ANSWER)
            context.update(first_response=response, first_body=body)
            audit = await harness.audit()
            attempt = next((row for row in audit["attempts"] if row["id"] == response.get("attempt_id")), None)
            ensure(attempt is not None, "真实提交未落Attempt", response)
            links = [row for row in audit["links"] if row["source_ref"] == "attempt:" + attempt["id"]]
            by_concept = {row["concept_id"]: row for row in links}
            expected = {ids["distribution"]: "correct", ids["equality"]: "incorrect", ids["unobserved"]: "not_observed"}
            for cid, result in expected.items():
                ensure(by_concept.get(cid, {}).get("observed_result") == result, "逐知识点结果没有按局部原答分开", {"expected": expected, "actual_links": links})
            ensure(not by_concept[ids["unobserved"]]["eligible_for_mastery"], "未观察概念取得了正式更新资格", by_concept[ids["unobserved"]])
            ensure({row["concept_id"] for row in audit["states"]} == {ids["distribution"], ids["equality"]}, "未知概念被整题结果广播更新", audit["states"])
            ensure(attempt["grading_packet"] is not None, "未保存冻结GradingPacket")
            return {"attempt_id": attempt["id"], "overall_result": attempt["result"], "local_results": expected, "links": links}

        await harness.check("BM01 综合题局部正确与错误分开，第三概念保持未观察", local_attribution)

        async def replay():
            ensure("first_body" in context, "缺少首个真实提交，无法检查重放")
            try:
                context["first_memory"] = await harness.settle(sid)
            except EvaluationPaused:
                raise
            except Exception as exc:
                raise AssertionError("BM02后台记忆收敛前置失败，尚未发送重放HTTP；" + str(exc)) from exc
            before = await harness.audit()
            response = await harness.request("POST", f"/sessions/{sid}/answers?stream=false", context["first_body"])
            after = await harness.audit()
            ensure(response.get("replayed") is True, "相同提交没有返回幂等回放", response)
            ensure(before["counts"] == after["counts"], "重放新增了作答、归因、消息、事实或模型运行", {"before": before["counts"], "after": after["counts"]})
            return {"same_submission_new_records": 0, "before_after_counts": before["counts"]}

        await harness.check("BM02 提交重放不新增事实、证据或模型运行", replay)

        async def counter_session():
            second = await harness.create("counter", courses["algebra"], [ids["distribution"], ids["equality"]])
            context["second"] = second
            old, new = first.get("session_goals"), second.get("session_goals")
            ensure(old and new and old["root_goal_id"] != new["root_goal_id"], "新会话继承了旧执行根目标", {"first": old, "second": new})
            ensure(len(new["nodes"]) == 1 and not new["return_stack"], "新会话继承了旧子目标或返回栈", new)
            response, _ = await harness.practice_and_answer(second["id"], questions["counter"], COUNTER_ANSWER)
            context["counter_response"] = response
            audit = await harness.audit()
            links = [row for row in audit["links"] if row["source_ref"] == "attempt:" + response.get("attempt_id", "")]
            results = {row["concept_id"]: row["observed_result"] for row in links}
            ensure(results.get(ids["distribution"]) == "incorrect" and results.get(ids["equality"]) == "correct", "第二次局部反例未按原答归因", results)
            return {"new_root_goal_id": new["root_goal_id"], "attempt_id": response["attempt_id"], "local_results": results}

        await harness.check("BM03 新会话目标独立，第二道题产生方向相反的局部证据", counter_session)

        async def concept_memory():
            ensure("second" in context, "缺少第二会话，无法校验跨会话整合")
            snapshot = await harness.settle(context["second"]["id"])
            context["algebra_memory"] = snapshot
            audit = await harness.audit()
            ref_sessions = {row["source_ref"]: row["session_id"] for row in audit["facts"]}
            details = {}
            for cid in (ids["distribution"], ids["equality"]):
                row = concept(snapshot, cid)
                ensure(row and row.get("content"), "模型未形成该知识点跨会话记忆", {"concept_id": cid, "snapshot": snapshot})
                refs = {ref for claim in row.get("claims", []) for ref in claim.get("source_refs", []) + claim.get("counter_source_refs", [])}
                refs.update(row.get("source_refs", []))
                source_sessions = {ref_sessions.get(ref) for ref in refs} - {None}
                ensure({sid, context["second"]["id"]} <= source_sessions, "概念记忆未同时保留成功与反例的原始来源", {"memory": row, "source_sessions": sorted(source_sessions)})
                observations = [item for item in audit["observations"] if item["concept_id"] == cid and item["status"] == "active"]
                ensure({sid, context["second"]["id"]} <= {item["session_id"] for item in observations}, "会话概念观察未保留两次实际来源", observations)
                details[cid] = {"version": row["version"], "source_sessions": sorted(source_sessions), "claims": row["claims"]}
            ensure(audit["fact_sources_unique"], "重复事实引用破坏了水位幂等")
            return details

        await harness.check("BM04 模型知识点记忆保留两个会话的表现与反例来源", concept_memory)

        async def scoped_preferences():
            physical = await harness.create("physics", courses["physics"], [ids["motion"]])
            context["physics"] = physical
            await harness.message(physical["id"], PREFERENCES_B)
            snapshot_b = await harness.settle(physical["id"])
            snapshot_a = await harness.request("GET", f"/sessions/{sid}/memory")
            context.update(physics_memory=snapshot_b, algebra_memory=snapshot_a)
            overall, a, b = snapshot_a.get("global_memory"), snapshot_a.get("course_memory"), snapshot_b.get("course_memory")
            ensure(overall and overall.get("content"), "模型未提取明确跨课程持续偏好", overall)
            ensure("符号" not in overall["content"] and "示意图" not in overall["content"],
                "总体正文混入本夹具仅课程范围的符号或示意图偏好", overall)
            ensure(a and a.get("content") and b and b.get("content"), "课程持续偏好未形成两份独立模型记忆", {"algebra": a, "physics": b})
            ensure(a["course_id"] == courses["algebra"] and b["course_id"] == courses["physics"], "课程记忆归属串课", {"algebra": a, "physics": b})
            ensure("符号" in a["content"] and "图" in b["content"], "课程正文未保留相异偏好主要含义", {"algebra": a, "physics": b})
            audit = await harness.audit()
            ref_courses = {row["source_ref"]: courses["algebra"] if row["session_id"] in {sid, context["second"]["id"]} else courses["physics"] for row in audit["facts"]}
            for row in (a, b):
                ensure(all(ref_courses.get(ref) == row["course_id"] for ref in row["source_refs"]), "课程记忆引用了其它课程来源", row)
            ensure(any(claim.get("explicit_preference") for claim in overall.get("claims", [])), "总体记忆没有可核查的明确持续偏好条目", overall)
            return {"global": overall, "algebra": a, "physics": b}

        await harness.check("BM05 明确持续偏好形成总体与两门课记忆，课程来源不串课", scoped_preferences)

        async def purpose_receipts():
            ensure("second" in context, "缺少第二会话，无法校验三层输入")
            await harness.message(context["second"]["id"], "这是脚本模拟学生：请只简短确认本次分配律学习将怎样遵守我的当前偏好，不讲解、不出题。", [ids["distribution"]])
            audit = await harness.audit()
            purposes = [row["data"] for row in audit["events"] if row["event"] == "context_purpose_selected"]
            grading = [row for row in purposes if row.get("context_purpose") == "grading"]
            ensure(len(grading) >= 2, "没有两次真实评分用途回执", purposes)
            for row in grading:
                ensure(row.get("history_memory_allowed") is False and row.get("tools_enabled") is False, "评分未实际关闭历史记忆或工具", row)
                ensure(not any(event["event"] == "memory_context_loaded" and event["data"].get("run_id") == row["run_id"] for event in audit["events"]), "grading运行实际注入了记忆", row)
            loaded = [row["data"] for row in audit["events"] if row["event"] == "memory_context_loaded" and row["session_id"] == context["second"]["id"]]
            candidates = [row for row in loaded if {"student", "course", "concept"} <= {str(item.get("scope_kind") or item.get("scope") or item.get("scope_key", "").split(":")[0]) for item in row.get("scope_receipts", [])}]
            ensure(candidates, "真实教学模型输入未同时加载总体、当前课程及相关知识点三层记忆", loaded)
            return {"grading_receipts": grading, "three_scope_teaching_receipt": candidates[-1]}

        await harness.check("BM06 实际grading隔离及真实教学模型加载三层记忆", purpose_receipts)

        async def correct_course():
            ensure("physics" in context, "缺少另一课程，无法验证局部纠正")
            before = await harness.audit()
            b_before = await harness.request("GET", f"/sessions/{context['physics']['id']}/memory")
            result = await harness.request("POST", f"/sessions/{sid}/memory/correct?wait=true", {"kind": "course", "text": CORRECTION_A})
            corrected = await harness.settle(sid, refresh=False)
            a = corrected.get("course_memory")
            ensure(a and "图" in a.get("content", ""), "模型未按明确纠正重建课程偏好", {"result": result, "snapshot": corrected})
            old_a = next((row for row in before["scopes"] if row["scope_kind"] == "course" and row["course_id"] == courses["algebra"]), None)
            ensure(old_a and a["version"] > old_a["version"], "课程纠正没有推进正文版本", {"before": old_a, "after": a})
            ensure(any(ref.startswith("correction:") for ref in a["source_refs"]), "课程纠正正文没有指向新的原始纠正来源", a)
            b_after = await harness.request("GET", f"/sessions/{context['physics']['id']}/memory")
            ensure(semantic_scope(b_before.get("course_memory")) == semantic_scope(b_after.get("course_memory")), "代数纠正意外修改物理课程记忆", {"before": b_before.get("course_memory"), "after": b_after.get("course_memory")})
            after = await harness.audit()
            ensure(before["originals"] == after["originals"], "纠正摘要改写了原始作答、状态、正式归因或消息", {"before": before["originals"], "after": after["originals"]})
            context["corrected_memory"] = corrected
            return {"course_memory": a, "unrelated_course_preserved": True, "originals_unchanged": after["originals"]}

        await harness.check("BM07 课程偏好纠正经真实模型重建，原始评估与其它课程保持", correct_course)

        async def scoped_forget():
            ensure("second" in context and "physics" in context, "缺少已建立的多会话/多课程上下文")
            await harness.settle(context["second"]["id"])
            before = await harness.audit()
            memory_before = await harness.request("GET", f"/sessions/{sid}/memory")
            forgotten = concept(memory_before, ids["distribution"])
            ensure(forgotten and forgotten.get("content"), "目标概念没有可遗忘的模型正文", memory_before)
            unrelated = concept(memory_before, ids["equality"])
            b_before = await harness.request("GET", f"/sessions/{context['physics']['id']}/memory")
            await harness.request("DELETE", f"/sessions/{sid}/memory/concept?concept_id={ids['distribution']}")
            immediate = await harness.request("GET", f"/sessions/{sid}/memory")
            ensure(not (concept(immediate, ids["distribution"]) or {}).get("content"), "概念遗忘后仍返回旧正文", concept(immediate, ids["distribution"]))
            ensure(semantic_scope(unrelated) == semantic_scope(concept(immediate, ids["equality"])), "遗忘分配律意外清空等式性质记忆")
            b_after = await harness.request("GET", f"/sessions/{context['physics']['id']}/memory")
            ensure(semantic_scope(b_before.get("course_memory")) == semantic_scope(b_after.get("course_memory")), "概念遗忘意外清空另一门课")
            after = await harness.audit()
            ensure(before["originals"] == after["originals"], "遗忘摘要改写原始作答或正式StudentState", {"before": before["originals"], "after": after["originals"]})
            old_obs = [row for row in after["observations"] if row["concept_id"] == ids["distribution"]]
            ensure(old_obs and all(row["status"] != "active" for row in old_obs), "旧会话概念观察没有沿作用域屏障失效", old_obs)
            current_scopes = {row["scope_key"]: row for row in after["scopes"]}
            dependent_keys = {row["parent_scope_key"] for row in before["dependencies"] if row["child_kind"] == "scope" and row["child_key"] == forgotten["scope_key"] and current_scopes.get(row["parent_scope_key"], {}).get("version") == row["parent_version"]}
            ensure(all(current_scopes[key]["status"] == "stale" for key in dependent_keys), "实际依赖目标概念的上层摘要未被屏蔽", sorted(dependent_keys))
            refreshed = await harness.settle(sid)
            ensure(not (concept(refreshed, ids["distribution"]) or {}).get("content"), "旧原答在刷新后复活了已遗忘概念记忆", concept(refreshed, ids["distribution"]))
            final = await harness.audit()
            ensure(after["originals"] == final["originals"], "遗忘后刷新更改了正式证据或状态")
            return {"forgotten_scope_key": forgotten["scope_key"], "invalidated_observation_ids": [row["id"] for row in old_obs],
                "actual_dependent_parent_scopes": sorted(dependent_keys), "originals_unchanged": final["originals"], "unrelated_concept_and_course_preserved": True}

        await harness.check("BM08 概念遗忘传播失效且不复活，正式证据与无关作用域保持", scoped_forget)
        report["complete"] = len(report["checks"]) == 8 and all(row["status"] == "passed" for row in report["checks"])
    except EvaluationPaused as exc:
        report["interrupted"] = True
        report["interruption_reason"] = str(exc)
    except EvaluationCheckFailed as exc:
        report["stopped_on_failure"] = True
        report["failed_check"] = str(exc)
    except Exception as exc:
        report["fatal_error"] = {"type": type(exc).__name__, "message": str(exc)[:2400]}
    finally:
        try:
            report["audit"] = await harness.audit()
            report["usage_totals"] = usage_totals(report["audit"])
            report["latency_breakdown"] = latency_breakdown(report["audit"])
        except Exception as exc:
            report["final_audit_error"] = {"type": type(exc).__name__, "message": str(exc)[:1200]}
            report["complete"] = False
        report["backend_source_sha256_after"] = source_digest()
        report["source_unchanged_during_run"] = report["backend_source_sha256_before"] == report["backend_source_sha256_after"]
        report["complete"] = report["complete"] and report["source_unchanged_during_run"]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        harness.save()
        await harness.client.aclose()
    print(json.dumps({"complete": report["complete"], "report": str(args.report), "checks": [{"label": row["label"], "status": row["status"]} for row in report["checks"]]}, ensure_ascii=True), flush=True)
    return 2 if report.get("interrupted") else 0 if report["complete"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/business_memory_acceptance.json")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
