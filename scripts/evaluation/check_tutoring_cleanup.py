"""真实验证旧计算链退役、教材来源投影及通用讲题；学生输入为模拟数据。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import gzip
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from sqlalchemy import func, select
from app.infrastructure.persistence.database import db_session
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.sessions.models import LearningSession
from app.domains.courses.models import Question
from app.domains.materials.models import TextbookDocument
from app.domains.materials.teaching_reference import pack_views, get_pack
from check_guided_teaching import frames, source_digest


def archived_counts():
    path = ROOT / "data/archives/simplification_p0/20261023/retired.json.gz"
    tables = json.loads(gzip.decompress(path.read_bytes()))["tables"]
    return {name: len(tables[name]["rows"]) for name in ("tutoring_actions", "tutoring_evidence")}


def assert_public_references(value):
    """只检查敏感字段，来源复核提示中的字段名称并不代表答案泄露。"""
    if isinstance(value, dict):
        assert not {
            "source_answer",
            "source_solution",
            "source_solution_steps",
            "reference_solution",
        }.intersection(value), "公开响应包含内部答案资料"
        for child in value.values():
            assert_public_references(child)
    elif isinstance(value, list):
        for child in value:
            assert_public_references(child)


async def main(args):
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend_source_sha256": source_digest(),
        "student_id": "evaluation_" + uuid4().hex[:12],
        "checks": [],
        "requests": [],
        "sessions": [],
        "complete": False,
        "data_origin": {
            "chapter": "existing_OpenStax_curated_excerpt",
            "new_question": "self_authored_without_answer",
            "student_messages": "simulated",
            "legacy_session": "isolated_fixture_with_archival_shape",
        },
        "not_tested": ["真实学生学习增益", "全学科解题正确率", "图片识别质量"],
    }

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    with db_session() as db:
        assert db.get_bind().dialect.name == "mysql"
        history_counts = archived_counts()
        archive = None
        for row in pack_views(db)[:200]:
            if row.payload.get("provenance", {}).get("origin") == "rule_based_analysis":
                archive = {
                    "id": row.id,
                    "course_id": row.course_id,
                    "hash": row.payload_hash,
                    "payload": deepcopy(row.payload),
                }
                break
        assert archive, "需要既有旧分析档案验证保留边界"
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=240, trust_env=False
    ) as client:

        async def request(method, path, body=None, status=200):
            body = await native_request_body(client, method, path, body, _request_bindings)
            started = time.perf_counter()
            response = await client.request(method, path, json=body)
            report["requests"].append(
                {
                    "method": method,
                    "path": path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
            assert (
                response.status_code == status
            ), f"{path}: {response.status_code} {response.text[:800]}"
            return response.json()

        async def check(label, operation):
            try:
                details = await operation()
                report["checks"].append(
                    {"label": label, "status": "passed", "details": details}
                )
                print("PASS " + label, flush=True)
            except Exception as exc:
                report["checks"].append(
                    {"label": label, "status": "failed", "error": str(exc)[:1500]}
                )
                save()
                raise
            save()

        async def retirement():
            for path in (
                "backend/app/tutoring",
                "backend/app/teaching_analysis",
                "backend/app/api/tutoring.py",
                "frontend/src/components/EquationTutor.tsx",
            ):
                assert not (ROOT / path).exists(), path
            schema = await request("GET", "/../openapi.json")
            assert not any(path.endswith("/tutoring") for path in schema["paths"])
            await request(
                "POST",
                "/sessions/not-a-session/tutoring",
                {"action": "start"},
                status=404,
            )
            return {"old_endpoint": "removed", "old_math_sources": "removed"}

        await check("C01 旧计算模块及专用接口已退役", retirement)

        async def import_source():
            raw = (
                ROOT
                / "data/raw/textbooks/openstax_elementary_algebra_2e/linear_equations_curated_excerpt.html"
            ).read_text(encoding="utf-8")
            result = await request(
                "POST",
                "/textbooks/import",
                {
                    "request_id": str(uuid4()),
                    "course_id": "junior_algebra",
                    "title": "OpenStax Elementary Algebra 2e · 原文目录（清理验收）",
                    "source_uri": "https://openstax.org/books/elementary-algebra-2e/pages/2-3-solve-equations-with-variables-and-constants-on-both-sides",
                    "source_kind": "curated_excerpt",
                    "license": "CC-BY-NC-SA-4.0",
                    "attribution": "OpenStax, Rice University · Elementary Algebra 2e · 第二章2.3/2.4选段；依据下载时保存的来源清单。",
                    "format": "html",
                    "content": raw,
                },
            )
            docid = result["document"]["id"]
            with db_session() as db:
                assert db.get(TextbookDocument, docid).raw_content == raw.encode()
                packs = pack_views(db, document_id=docid)
                assert len(packs) >= 12
                for pack in packs:
                    assert (
                        pack.payload["verification"]["method"]
                        == "source_structure_integrity"
                    )
                    assert pack.payload["provenance"]["mathematics_checked"] is False
                    value = pack.payload["source_answer"]["value"]
                    expected = (
                        []
                        if value is None
                        else value if isinstance(value, (list, dict)) else [value]
                    )
                    assert db.get(Question, pack.question_id).answer == expected
            public = await request(
                "GET", f"/teaching-packs?course_id=junior_algebra&document_id={docid}"
            )
            assert public["channels_used"] == ["textbook_source_catalog"]
            assert_public_references(public)
            return {
                "document_id": docid,
                "pack_count": len(packs),
                "math_checks": False,
            }

        await check("C02 真实教材只投影来源，不计算或补造答案", import_source)

        async def archival_read():
            public = await request(
                "GET",
                f"/teaching-packs/{archive['id']}?course_id={archive['course_id']}",
            )
            assert public["hash"] == archive["hash"]
            assert public["verification"]["method"] == "source_structure_integrity"
            assert (
                public["verification"].get("archive_verification")
                == archive["payload"]["verification"]
            )
            with db_session() as db:
                assert get_pack(db, archive["id"]).payload == archive["payload"]
            return {"archive_id": archive["id"], "payload_unchanged": True}

        await check("C03 旧教材分析档案保留且未重新求解", archival_read)

        async def session(course):
            result = await request(
                "POST",
                "/sessions",
                {
                    "course_id": course,
                    "student_id": report["student_id"],
                    "concept_ids": [],
                    "goal": "通用讲题清理验收",
                },
            )
            report["sessions"].append(result["id"])
            return result["id"]

        async def non_equation():
            stem = "【自编清理验收题，无标准答案】有3件不同上衣和2条不同裤子，各选一件组成一套。甲认为有3+2=5种搭配。你如何判断这个说法？请先引导我分析。"
            result = await request(
                "POST",
                "/textbooks/import",
                {
                    "request_id": str(uuid4()),
                    "course_id": "math",
                    "title": "自编清理验收·分类计数（无答案）",
                    "source_uri": "fixture://self_authored/cleanup/counting",
                    "source_kind": "self_authored",
                    "format": "html",
                    "content": '<h1>分类计数</h1><div data-kind="exercise" data-number="1"><p>'
                    + stem
                    + "</p></div>",
                },
            )
            with db_session() as db:
                pack = pack_views(db, document_id=result["document"]["id"])[0]
                qid = pack.question_id
                assert (
                    pack.status == "reference_only"
                    and db.get(Question, qid).answer == []
                )
            sid = await session("math")
            response = await client.post(
                f"/sessions/{sid}/practice?stream=true",
                json={"question_id": qid, "cognitive_type": "understanding"},
            )
            assert response.status_code == 200
            events = frames(response.text)
            assert not any(name == "error" for name, _ in events), events
            assert any(name == "guidance" for name, _ in events)
            restored = await request("GET", f"/sessions/{sid}")
            assert restored["guidance"]["problem"]["stem"] == stem
            assert restored["guidance"]["pending_question"]
            with db_session() as db:
                assert db.get(Question, qid).answer == []
            return {
                "session_id": sid,
                "question_id": qid,
                "no_answer_required": True,
                "sse_events": sorted({name for name, _ in events}),
            }

        await check("C04 无标准答案的非方程教材题经真实Agent和SSE引导", non_equation)

        async def resume_legacy():
            sid = await session("junior_algebra")
            legacy = {
                "status": "working",
                "contract": {
                    "id": "legacy-cleanup-fixture",
                    "equation": "3x+6=42",
                    "answer": "PRIVATE_LEGACY_REFERENCE",
                },
                "accepted_steps": ["3x=36"],
                "current_equation": "3x=36",
                "assisted": True,
                "diagnoses": [],
            }
            with db_session() as db:
                db.get(LearningSession, sid).pending_context = {
                    "archived_problem": {"question_id": "legacy-cleanup-fixture", "stem": "3x+6=42", "status": "archived", "notice": "原题已归档"}
                }
            before = await request("GET", f"/sessions/{sid}")
            assert before["archived_problem"]["status"] == "archived"
            assert "PRIVATE_LEGACY_REFERENCE" not in json.dumps(before)
            result = await request(
                "POST",
                f"/sessions/{sid}/guidance?stream=false",
                {
                    "action": "start", "problem": {"stem": before["archived_problem"]["stem"]},
                    "text": "我之前得到3x=36，为什么两边能同时减6？请接着解释，不要另起一道题。",
                    "request_id": str(uuid4()),
                },
            )
            assert result["guidance"]["problem"]["stem"] == "3x+6=42"
            with db_session() as db:
                context = db.get(LearningSession, sid).pending_context
                assert "equation_tutoring" not in context
                assert "legacy_history" not in context["guided_teaching"]
            return {"session_id": sid, "old_snapshot_unchanged": True}

        await check("C05 旧会话保留历史并接续通用讲题", resume_legacy)

        async def audit():
            with db_session() as db:
                after = archived_counts()
                assert after == history_counts
                rows = list(
                    db.scalars(
                        select(AgentRun).where(
                            AgentRun.session_id.in_(report["sessions"])
                        )
                    )
                )
                teaching = [row for row in rows if row.skill == "guided_explanation"]
                assert len(teaching) >= 2 and all(
                    row.status == "completed" for row in teaching
                )
                report["metrics"] = {
                    "model_runs": len(teaching),
                    "failed_runs": sum(row.status != "completed" for row in teaching),
                    "prompt_tokens": sum(row.prompt_tokens for row in teaching),
                    "completion_tokens": sum(row.completion_tokens for row in teaching),
                    "model_duration_ms": round(
                        sum(row.duration_ms for row in teaching), 2
                    ),
                }
                report["actual_models"] = sorted({row.model for row in teaching})
            return {"legacy_archive_counts_unchanged": after, **report["metrics"]}

        await check("C06 MySQL历史账本及真实模型用量可核对", audit)
    report["complete"] = True
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps(report["metrics"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data/reports/tutoring_cleanup_acceptance.json",
    )
    asyncio.run(main(parser.parse_args()))
