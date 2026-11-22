"""真实模型回归：公开数学元数据放入独立临时库，不读写个人学习档案。"""

import asyncio
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from app.infrastructure.clients.graph import GraphClient
from app.infrastructure.clients.llm import ModelClient
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.persistence.database import Base
from app.application.commands.courses.import_math import prepare_course
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.courses.models import Concept, ConceptRelation, Course, Material, Question
from app.domains.courses.retrieval.service import RetrievalService
from app.application.agent.runner import AgentRunner
from app.domains.learning.schemas import MessageRequest
from app.application.learning.service import LearningService
from app.domains.sessions.service import SessionService
from app.application.agent.tools import TeachingTools


@contextmanager
def public_math_database():
    bundle = prepare_course(ROOT / "data" / "raw" / "xes3g5m" / "metadata")
    temporary = TemporaryDirectory(prefix="stem-autonomy-")
    engine = create_engine("sqlite:///" + (Path(temporary.name) / "public.db").as_posix())
    from app.application.model_registry import register_models
    register_models()
    Base.metadata.create_all(engine)

    @contextmanager
    def isolated_session():
        with Session(engine, expire_on_commit=False) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    with isolated_session() as db:
        db.add(Course(**bundle["course"]))
        for model, key in (
            (Concept, "concepts"),
            (Question, "questions"),
            (Material, "materials"),
            (ConceptRelation, "relations"),
        ):
            db.execute(insert(model), bundle[key])
    try:
        with ExitStack() as stack:
            for module in (
                "app.domains.courses.catalog",
                "app.domains.sessions.service",
                "app.application.learning.service",
                "app.application.learning.planning",
                "app.domains.courses.queries",
                "app.domains.courses.difficulty.service",
                "app.domains.practice.service",
                "app.domains.assessment.service",
                "app.domains.students.service",
                "app.domains.practice.batches",
                "app.domains.assessment.batch",
            ):
                stack.enter_context(
                    patch(module + ".db_session", isolated_session)
                )
            stack.enter_context(patch("app.application.agent.runner.db_session", isolated_session))
            stack.enter_context(patch("app.application.agent.tools.db_session", isolated_session))
            yield bundle, isolated_session
    finally:
        engine.dispose()
        temporary.cleanup()


def source_fingerprint():
    sources = sorted((ROOT / "backend" / "app").rglob("*.py")) + sorted(
        (ROOT / "backend" / "app" / "agent" / "skills").rglob("*.md")
    )
    fingerprint = hashlib.sha256()
    for path in sources:
        fingerprint.update(str(path.relative_to(ROOT)).encode())
        fingerprint.update(path.read_bytes())
    return fingerprint.hexdigest()


def seed_decision_fixture(isolated_session):
    """让难度和先修位置都可查询且排序相反，仅用于验证决策，数值不冒充实测。"""
    topics = ["集合基本运算", "二量容斥", "三量容斥", "计数与容斥", "容斥与整除", "综合计数模型"]
    with isolated_session() as db:
        db.add(
            Course(
                id="autonomy_fixture",
                title="双指标决策验证课程",
                description="独立测试夹具，难度数值是人工测试输入，先修链仅用于验证系统行为。",
                source_ref="synthetic evaluation fixture",
            )
        )
        for index, (topic, value) in enumerate(zip(topics, [90, 85, 80, 35, 25, 20]), 1):
            cid = f"autonomy_fixture:c{index}"
            db.add(
                Concept(
                    id=cid,
                    source_id=str(index),
                    course_id="autonomy_fixture",
                    name=topic,
                    summary="独立测试概念",
                )
            )
            db.add(
                Question(
                    id=f"autonomy_fixture:q{index}",
                    source_id=str(index),
                    course_id="autonomy_fixture",
                    stem="仅用于测试排序行为的公开示例题",
                    options={},
                    answer=["1"],
                    explanation="测试输入",
                    question_type="fill",
                    cognitive_type="unknown",
                    concept_ids=[cid],
                    source_ref="synthetic evaluation fixture",
                    validation={
                        "difficulty_estimate": {
                            "value": value,
                            "scale_max": 100,
                            "higher_is_harder": True,
                            "method": "synthetic_fixture",
                            "reference_group": "test_only",
                            "version": "v1",
                        }
                    },
                )
            )
            if index > 1:
                db.add(
                    ConceptRelation(
                        id=f"fixture-edge-{index}",
                        course_id="autonomy_fixture",
                        source_id=f"autonomy_fixture:c{index-1}",
                        target_id=cid,
                        relation_type="PREREQUISITE_OF",
                        origin="synthetic evaluation fixture",
                        verified=True,
                    )
                )
    return topics


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    fingerprint = source_fingerprint()
    with public_math_database() as (bundle, isolated_session):
        models, search, graph = ModelClient(), SearchClient(), GraphClient()
        learning = LearningService(
            AgentRunner(models, TeachingTools(RetrievalService(models, search, graph)))
        )
        sessions = SessionService()
        results = []

        async def emit(event, data):
            if event == "tool_completed":
                print("工具", data["name"], data.get("status"), flush=True)

        async def run_case(name, session_id, content, check):
            turn_id = str(uuid4())
            print("开始", name, flush=True)
            try:
                result = await learning.turn(
                    session_id,
                    MessageRequest(content=content, request_id=str(uuid4())),
                    turn_id,
                    emit,
                )
                with isolated_session() as db:
                    runs = list(db.scalars(select(AgentRun).where(AgentRun.turn_id == turn_id)))
                    traces = [call for run in runs for call in run.tool_calls]
                    usage = {
                        key: sum(getattr(run, key) for run in runs)
                        for key in ("prompt_tokens", "completion_tokens")
                    }
                passed = bool(check(result, traces))
                row = {
                    "name": name,
                    "passed": passed,
                    "plan": result["plan"],
                    "message": result["message"]["content"],
                    "tool_calls": traces,
                    "usage": usage,
                    "agents": [run.agent for run in runs],
                }
            except Exception as exc:
                row = {
                    "name": name,
                    "passed": False,
                    "error": type(exc).__name__ + ": " + str(exc)[:400],
                }
            results.append(row)
            print("结果", name, "通过" if row["passed"] else "未通过", flush=True)
            return row

        try:
            broad = sessions.create("我想学会当前课程中最难的几个知识点", [], "math")
            await run_case(
                "实际课程缺少难度数据时先确认可行标准",
                broad["id"],
                broad["goal"],
                lambda result, trace: result["plan"]["action"] == "clarify"
                and not result["plan"]["concept_ids"]
                and "lesson" not in result
                and len((result["plan"].get("clarification") or {}).get("options", [])) >= 2
                and not any(t["name"] == "rag_retrieve" for t in trace),
            )
            await run_case(
                "短回答接续数值排名任务",
                broad["id"],
                "按难度数值最高的三个来",
                lambda result, trace: any(
                    t["name"] == "rank_learning_targets"
                    and t.get("arguments", {}).get("criterion") == "difficulty"
                    and t.get("status") == "completed"
                    for t in trace
                )
                and result["plan"]["action"] in {"clarify", "respond"}
                and "lesson" not in result,
            )
            chat = sessions.create("先了解学习助手", [], "math")
            await run_case(
                "普通沟通不强制课程检索或教学",
                chat["id"],
                "先别讲课，用一句话告诉我你能怎样帮助我学习",
                lambda result, trace: result["plan"]["action"] == "respond"
                and "lesson" not in result
                and all(t["name"] == "read_skill" for t in trace),
            )
            overview = sessions.create("了解当前课程", [], "math")
            await run_case(
                "课程总量来自概览工具",
                overview["id"],
                "当前课程一共有多少个知识分类节点？其中哪些可以作为具体学习目标？",
                lambda result, trace: any(
                    t["name"] == "get_course_overview" and t.get("status") == "completed"
                    for t in trace
                )
                and result["plan"]["action"] == "respond"
                and "1175" in result["message"]["content"].replace(",", ""),
            )
            target = next(c for c in bundle["concepts"] if c["name"] == "容斥原理")
            switch = sessions.create("学习加乘原理", ["math:c503"], "math")
            await run_case(
                "切换目标后进入正确教学",
                switch["id"],
                "换一个目标，我现在要学习容斥原理，请解释它为什么要减去重叠的部分",
                lambda result, trace: target["id"] in result["plan"]["concept_ids"]
                and "math:c503" not in result["plan"]["concept_ids"]
                and "lesson" in result,
            )
            topics = seed_decision_fixture(isolated_session)
            controlled = sessions.create("我想学当前课程最难的三个知识点", [], "autonomy_fixture")
            await run_case(
                "受控双指标课程澄清数值难度与先修位置",
                controlled["id"],
                controlled["goal"],
                lambda result, trace: result["plan"]["action"] == "clarify"
                and not result["plan"]["concept_ids"]
                and any(
                    "难度" in option or "数值" in option
                    for option in (result["plan"].get("clarification") or {}).get("options", [])
                )
                and any(
                    "先修" in option or "图谱" in option
                    for option in (result["plan"].get("clarification") or {}).get("options", [])
                ),
            )
            await run_case(
                "明确数值标准后推荐正确的前三项",
                controlled["id"],
                "我指的是难度数值最高的三个，先只给我推荐名单，不用开始讲解",
                lambda result, trace: result["plan"]["action"] == "respond"
                and "lesson" not in result
                and any(
                    t["name"] == "rank_learning_targets"
                    and t.get("arguments", {}).get("criterion") == "difficulty"
                    and t.get("status") == "completed"
                    for t in trace
                )
                and (
                    all(topic in result["message"]["content"] for topic in topics[:3])
                    or set(result["plan"]["concept_ids"])
                    == {f"autonomy_fixture:c{i}" for i in (1, 2, 3)}
                ),
            )
        finally:
            await asyncio.gather(models.close(), search.close(), graph.close())
        report = {
            "evaluation_version": "v4",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": models.settings.llm_model,
            "database": "isolated temporary SQLite; public XES3G5M metadata and explicitly synthetic decision fixture",
            "personal_records_read_or_written": False,
            "source_fingerprint": fingerprint,
            "source_unchanged_during_run": fingerprint == source_fingerprint(),
            "synthetic_fixture": "6 concepts with test-only scores and a prerequisite order intentionally different from difficulty order; not real difficulty calibration",
            "public_concepts": len(bundle["concepts"]),
            "public_questions": len(bundle["questions"]),
            "passed": sum(r["passed"] for r in results),
            "total": len(results),
            "cases": results,
        }
        path = ROOT / "data" / "reports" / "agent_autonomy_live.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"passed": report["passed"], "total": report["total"], "report": str(path)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
