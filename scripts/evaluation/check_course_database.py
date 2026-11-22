"""验收课程事实库、检索投影和只读接口；不发出新的模型请求。"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from request_protocol import request_body as native_request_body

_request_bindings = {}
from sqlalchemy import select

from app.infrastructure.clients.graph import GraphClient
from app.infrastructure.clients.search import SearchClient
from app.infrastructure.config import get_settings
from app.infrastructure.persistence.database import db_session
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.assessment.models import Attempt
from app.domains.courses.models import Concept, ConceptRelation, Material, Question
from app.domains.sessions.models import LearningSession
from app.domains.students.models import MemoryItem, StudentState
from app.domains.courses.catalog import public_question
from app.domains.practice.service import PracticeService

REPORT = ROOT / "data/reports/course_database_acceptance.json"
EXPECTED_QUESTIONS = {"math": 7652, "mechanics": 177}
EXPECTED_CONCEPTS = {"math": 1175, "mechanics": 198}
PRIVATE_FIELDS = {
    "answer",
    "answer_key",
    "explanation",
    "student_id",
    "student_answer",
    "student_state",
    "student_profile",
    "answer_text",
    "assessment",
    "mastery",
    "evidence_ids",
    "evidence_questions",
    "pending_context",
    "prediction",
    "session_id",
    "attempt_id",
    "learning_memory",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def private_fields(value, path="") -> list[str]:
    """检查结构字段，题干中的普通“答案”文字不作为泄漏判定。"""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            location = path + "." + key
            if key in PRIVATE_FIELDS:
                found.append(location)
            found.extend(private_fields(item, location))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(private_fields(item, f"{path}[{index}]"))
    return found


def local_evidence_fingerprint() -> dict:
    """仅计算摘要，不在验收报告中保存个人答案、状态或记忆正文。"""
    with db_session() as db:
        data = {
            "states": [
                [s.id, s.mastery, s.evidence_ids, s.evidence_questions, s.version]
                for s in db.scalars(
                    select(StudentState)
                    .where(StudentState.student_id == "local")
                    .order_by(StudentState.id)
                )
            ],
            "memories": [
                [m.id, m.course_id, m.status, m.text, m.evidence_ids]
                for m in db.scalars(
                    select(MemoryItem)
                    .where(MemoryItem.student_id == "local")
                    .order_by(MemoryItem.id)
                )
            ],
            "attempts": [
                a.id
                for a in db.scalars(
                    select(Attempt)
                    .join(LearningSession, Attempt.session_id == LearningSession.id)
                    .where(LearningSession.student_id == "local")
                    .order_by(Attempt.id)
                )
            ],
        }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "counts": {key: len(value) for key, value in data.items()},
    }


class CourseAcceptance:
    def __init__(self, base_url: str):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", timeout=60, trust_env=False
        )
        self.search, self.graph = SearchClient(), GraphClient()
        self.questions, self.concepts = {}, {}
        self.student_id = "evaluation_" + uuid4().hex[:12]
        self.report = {
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "base_url": base_url,
            "student_id": self.student_id,
            "scope": "课程入库、检索与隔离验收；不生成讲解、不提交学生答案、不发出新的模型请求",
            "limitations": [
                "PNG 文件可读取不代表图像教学语义正确",
                "当前模型接口不具备读图能力",
                "hybrid 检查仅复用已存在的本地向量缓存",
                "未验证所有数学等价表达式的确定性判分；无法精确匹配时仍需语义诊断",
            ],
            "checks": [],
            "complete": False,
            "initial_failure_report": (
                "data/reports/course_database_acceptance.initial.json"
                if (
                    ROOT / "data/reports/course_database_acceptance.initial.json"
                ).is_file()
                else None
            ),
        }

    def save(self):
        checks = self.report["checks"]
        passed = sum(item["status"] == "passed" for item in checks)
        self.report["summary"] = {
            "passed": passed,
            "failed": len(checks) - passed,
            "total": len(checks),
            "success_rate": passed / len(checks) if checks else 0,
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    async def check(self, identifier: str, title: str, operation):
        started = time.perf_counter()
        try:
            details = operation()
            if inspect.isawaitable(details):
                details = await details
            result = {
                "id": identifier,
                "title": title,
                "status": "passed",
                "details": details,
            }
        except Exception as exc:
            result = {
                "id": identifier,
                "title": title,
                "status": "failed",
                "error": type(exc).__name__,
                "message": str(exc)[:650],
            }
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self.report["checks"].append(result)
        self.save()
        print(f"{identifier}: {result['status']} - {title}", flush=True)

    async def request(self, method: str, path: str, **kwargs):
        if method.upper() == "POST":
            kwargs["json"] = await native_request_body(self.http, method, "/" + path.lstrip("/"), kwargs.get("json"), _request_bindings)
        response = await self.http.request(method, path.lstrip("/"), **kwargs)
        require(not response.is_error, f"{method} {path}: HTTP {response.status_code}")
        return response.json()

    def database_counts(self):
        with db_session() as db:
            for course in EXPECTED_QUESTIONS:
                self.questions[course] = {
                    q.id: q
                    for q in db.scalars(
                        select(Question).where(
                            Question.course_id == course, Question.generated == False
                        )
                    )
                }
                self.concepts[course] = {
                    c.id
                    for c in db.scalars(
                        select(Concept).where(Concept.course_id == course)
                    )
                }
            materials = Counter(
                m.course_id
                for m in db.scalars(
                    select(Material).where(Material.visibility == "public")
                )
            )
        questions = {key: len(value) for key, value in self.questions.items()}
        concepts = {key: len(value) for key, value in self.concepts.items()}
        require(questions == EXPECTED_QUESTIONS, f"原题数量不一致：{questions}")
        require(concepts == EXPECTED_CONCEPTS, f"知识节点数量不一致：{concepts}")
        require(
            materials["math"] == 1175 and materials["mechanics"] == 219,
            f"公开材料数量不一致：{dict(materials)}",
        )
        return {
            "original_questions": questions,
            "concepts": concepts,
            "public_materials": dict(materials),
        }

    def availability(self):
        result = {
            course: Counter(
                public_question(q)["availability"] for q in questions.values()
            )
            for course, questions in self.questions.items()
        }
        imported = Counter(
            q.validation.get("availability", "ready")
            for q in self.questions["math"].values()
        )
        require(
            imported
            == Counter(
                ready=7587,
                ambiguous_options=42,
                insufficient_options=18,
                duplicate_options=5,
            ),
            f"导入器原始异常分类不一致：{dict(imported)}",
        )
        # 缺图保护额外隔离25道提及图示、但版式尚未人工审核的题。
        # 首次验收把65道来源异常误当成全部不可练题，初次失败报告保留追溯。
        require(
            result["math"]
            == Counter(
                ready=7562,
                ambiguous_options=42,
                insufficient_options=18,
                duplicate_options=5,
                missing_figure=25,
            ),
            f"数学可用性数量不一致：{dict(result['math'])}",
        )
        require(
            result["mechanics"] == Counter(ready=174, missing_figure=3),
            f"力学可用性数量不一致：{dict(result['mechanics'])}",
        )
        return {
            "public_availability": {key: dict(value) for key, value in result.items()},
            "math_source_anomalies": dict(imported),
            "additional_conservative_layout_review_ids": [
                q.id
                for q in self.questions["math"].values()
                if public_question(q)["availability"] == "missing_figure"
            ],
            "initial_assertion_correction": "65为导入器识别的源数据异常，不包含沿用缺图策略另行隔离的25道版式待审题；没有放开任何隔离题。",
        }

    def ordered_blanks(self):
        source = ROOT / "data/raw/xes3g5m/metadata/questions.json"
        raw = json.loads(source.read_text(encoding="utf-8"))
        counts = Counter()
        for q in self.questions["math"].values():
            if q.question_type != "fill":
                continue
            original = raw[q.source_id]["answer"]
            if len(original) > 1:
                expected = {
                    str(index + 1): [value] for index, value in enumerate(original)
                }
                require(q.answer == expected, f"多空顺序或结构错误：{q.id}")
                require(
                    public_question(q).get("blank_count") == len(original),
                    f"多空公开数量错误：{q.id}",
                )
                counts["ordered_questions"] += 1
                counts["ordered_blanks"] += len(original)
            else:
                require(q.answer == original, f"单空原答案发生变化：{q.id}")
                counts["single_blank_questions"] += 1
        require(counts["ordered_questions"] > 0, "没有验证到多空题")
        return {
            **counts,
            "source_order_verified": True,
            "answer_values_in_report": False,
        }

    async def es_counts(self):
        output = {}
        for course, expected in EXPECTED_QUESTIONS.items():
            filters = [{"term": {"course_id": course}}, {"term": {"origin": "dataset"}}]
            result = await self.search.request(
                "POST",
                f"/{self.search.QUESTIONS}/_count",
                json={"query": {"bool": {"filter": filters}}},
            )
            require(
                result["count"] == expected, f"ES {course} 原题数量为 {result['count']}"
            )
            materials = await self.search.request(
                "POST",
                f"/{self.search.MATERIALS}/_count",
                json={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"course_id": course}},
                                {"term": {"visibility": "public"}},
                            ]
                        }
                    }
                },
            )
            require(
                materials["count"] == (1175 if course == "math" else 219),
                f"ES {course} 材料数量错误",
            )
            output[course] = {
                "dataset_questions": result["count"],
                "public_materials": materials["count"],
            }
        return output

    async def es_public_fields(self):
        totals = {}
        for index in (self.search.QUESTIONS, self.search.MATERIALS):
            scroll_id = None
            count = 0
            try:
                batch = await self.search.request(
                    "POST",
                    f"/{index}/_search?scroll=1m",
                    json={
                        "size": 1000,
                        "sort": ["_doc"],
                        "_source": {"excludes": ["embedding"]},
                        "query": {
                            "bool": {
                                "filter": [
                                    {"terms": {"course_id": ["math", "mechanics"]}},
                                    {"term": {"visibility": "public"}},
                                ]
                            }
                        },
                    },
                )
                while True:
                    scroll_id = batch.get("_scroll_id", scroll_id)
                    hits = batch["hits"]["hits"]
                    if not hits:
                        break
                    for hit in hits:
                        leaked = private_fields(hit["_source"])
                        require(
                            not leaked,
                            f"公开索引 {index} 文档 {hit['_id']} 有私有字段：{leaked[:3]}",
                        )
                        count += 1
                    batch = await self.search.request(
                        "POST",
                        "/_search/scroll",
                        json={"scroll": "1m", "scroll_id": scroll_id},
                    )
            finally:
                if scroll_id:
                    await self.search.request(
                        "DELETE", "/_search/scroll", json={"scroll_id": [scroll_id]}
                    )
            totals[index] = count
        require(totals[self.search.QUESTIONS] >= 7829, "未覆盖全部原题文档")
        return {
            "documents_scanned": totals,
            "private_field_leaks": 0,
            "vectors_not_dumped": True,
        }

    async def neo4j_counts(self):
        rows = await self.graph.query(
            "MATCH (n:StemTutorConcept) WHERE n.course_id IN $courses RETURN n.course_id AS course_id,count(n) AS count",
            courses=list(EXPECTED_CONCEPTS),
        )
        concepts = {row["course_id"]: row["count"] for row in rows}
        require(concepts == EXPECTED_CONCEPTS, f"Neo4j节点数量不一致：{concepts}")
        rows = await self.graph.query(
            "MATCH (q:StemTutorQuestion) WHERE q.course_id IN $courses RETURN q.course_id AS course_id,count(q) AS count",
            courses=list(EXPECTED_QUESTIONS),
        )
        questions = {row["course_id"]: row["count"] for row in rows}
        require(questions == EXPECTED_QUESTIONS, f"Neo4j题目数量不一致：{questions}")
        rows = await self.graph.query(
            "MATCH (a:StemTutorConcept)-[r]->(b:StemTutorConcept) WHERE a.course_id='math' AND b.course_id='math' RETURN type(r) AS kind,count(r) AS count"
        )
        relations = {row["kind"]: row["count"] for row in rows}
        require(
            relations == {"STEM_HAS_CHILD": 1314, "STEM_RELATED_TO": 764},
            f"数学图关系数量或语义不一致：{relations}",
        )
        cross = await self.graph.query(
            "MATCH (a:StemTutorConcept)-[r]->(b:StemTutorConcept) WHERE a.course_id IN $courses AND b.course_id IN $courses AND a.course_id<>b.course_id RETURN count(r) AS count",
            courses=list(EXPECTED_CONCEPTS),
        )
        require(cross[0]["count"] == 0, "图谱存在跨课程关系")
        return {
            "concepts": concepts,
            "questions": questions,
            "math_relations": relations,
            "cross_course_relations": 0,
        }

    async def course_directory(self):
        result = await self.request("GET", "courses")
        courses = {item["id"]: item for item in result["items"]}
        for course in EXPECTED_QUESTIONS:
            require(
                courses[course]["question_count"] == EXPECTED_QUESTIONS[course],
                f"HTTP {course} 题目数错误",
            )
            require(
                courses[course]["concept_count"] == EXPECTED_CONCEPTS[course],
                f"HTTP {course} 节点数错误",
            )
        return {key: courses[key] for key in EXPECTED_QUESTIONS}

    def validate_search(self, data: dict, course: str, expected_id: str | None = None):
        require(data["items"], f"{course} 查题结果为空")
        with db_session() as db:
            ids = [item["question"]["id"] for item in data["items"]]
            trusted_ids = set(
                db.scalars(
                    select(Question.id).where(
                        Question.id.in_(ids), Question.course_id == course
                    )
                )
            )
        ids = []
        for item in data["items"]:
            question = item["question"]
            require(
                question["course_id"] == course and question["id"] in trusted_ids,
                "查题返回跨课程或事实库不存在的题目",
            )
            require(not private_fields(question), "公开题目包含答案、解析或学生字段")
            ids.append(question["id"])
        if expected_id:
            require(expected_id in ids, f"未命中题号 {expected_id}")
        return ids

    async def keyword_search(self):
        output = {}
        for course, source_id in (
            ("math", "7649"),
            ("math", "1005"),
            ("math", "7644"),
            ("mechanics", "Pm_668268"),
        ):
            identifier = course + ":" + source_id
            for query in (source_id, identifier):
                data = await self.request(
                    "GET",
                    "search/questions",
                    params={
                        "course_id": course,
                        "q": query,
                        "mode": "keyword",
                        "limit": 10,
                    },
                )
                ids = self.validate_search(data, course, identifier)
                require(ids[0] == identifier, f"精确题号{query}没有排首：{ids[:3]}")
                require(
                    "keyword" in data["channels_used"] and not data["degraded"],
                    "关键字搜索未走正常ES路径",
                )
                output[query] = {
                    "course_id": course,
                    "ids": ids,
                    "timings": data["timings"],
                }
        return output

    def cached_query(self, course: str) -> tuple[str, str]:
        settings = get_settings()
        sample_id = "math:0" if course == "math" else "mechanics:Pm_668268"
        question = self.questions[course][sample_id]
        text = (
            question.stem
            + " "
            + " ".join(f"{key}. {value}" for key, value in question.options.items())
        )
        candidates = [
            question.stem[:100] + "\n" + text,
            text,
            question.stem + " " + " ".join(question.options.values()),
            question.stem,
        ]
        if course == "mechanics":
            fixed_tasks = ROOT / "data/evaluation/mechanics_20_tasks.json"
            raw = json.loads(fixed_tasks.read_text(encoding="utf-8"))
            tasks = raw.get("tasks", []) if isinstance(raw, dict) else raw
            candidates.extend(task["query"] for task in tasks if task.get("query"))
        for query in dict.fromkeys(candidates):
            key = hashlib.sha256(
                f"{settings.embedding_model}:{settings.embedding_dim}:v1:{query}".encode()
            ).hexdigest()
            path = ROOT / "data/cache/embeddings" / (key + ".json")
            if not path.is_file():
                continue
            vector = json.loads(path.read_text(encoding="utf-8"))
            if (
                len(vector) == settings.embedding_dim
                and all(math.isfinite(value) for value in vector)
                and any(vector)
            ):
                return query, key
        raise AssertionError(
            f"{course} 无可复用的完整query向量缓存；已跳过hybrid HTTP请求，避免调用模型"
        )

    async def hybrid_search(self):
        output = {}
        for course in EXPECTED_QUESTIONS:
            query, cache_key = self.cached_query(course)
            data = await self.request(
                "GET",
                "search/questions",
                params={"course_id": course, "q": query, "mode": "hybrid", "limit": 10},
            )
            ids = self.validate_search(data, course)
            require(
                set(data["channels_used"]) == {"keyword", "vector"}
                and not data["degraded"],
                f"{course} hybrid缺少真实双路检索",
            )
            output[course] = {
                "ids": ids,
                "embedding_cache_sha256": cache_key,
                "timings": data["timings"],
            }
        return output

    async def graph_semantics(self):
        with db_session() as db:
            edge = db.scalar(
                select(ConceptRelation)
                .where(
                    ConceptRelation.course_id == "math",
                    ConceptRelation.relation_type == "HAS_CHILD",
                )
                .order_by(ConceptRelation.id)
            )
            seed = edge.source_id
        mathematical = await self.request(
            "GET", "graph", params={"course_id": "math", "concept_ids": seed}
        )
        require(
            mathematical["edges"]
            and any(
                edge["relation_type"] == "HAS_CHILD" for edge in mathematical["edges"]
            ),
            "数学图未返回分类父子边",
        )
        require(
            all(node["id"] in self.concepts["math"] for node in mathematical["nodes"]),
            "数学图返回跨课程节点",
        )
        require(
            all(
                edge["relation_type"] in {"HAS_CHILD", "RELATED_TO"}
                for edge in mathematical["edges"]
            ),
            "数学图错误产生先修语义",
        )
        require(
            all(
                edge["origin"] == "dataset_taxonomy"
                for edge in mathematical["edges"]
                if edge["relation_type"] == "HAS_CHILD"
            ),
            "数学分类边来源错误",
        )
        mechanical = await self.request(
            "GET",
            "graph",
            params={
                "course_id": "mechanics",
                "concept_ids": "mechanics:c19,mechanics:c160",
            },
        )
        require(
            mechanical["edges"]
            and all(
                edge["relation_type"] == "RELATED_TO" for edge in mechanical["edges"]
            ),
            "旧力学关联图语义改变",
        )
        return {
            "math_seed": seed,
            "math_edges": len(mathematical["edges"]),
            "mechanics_edges": len(mechanical["edges"]),
            "taxonomy_is_not_prerequisite": True,
        }

    async def public_blank_and_png(self):
        sample = next(
            q
            for q in self.questions["math"].values()
            if isinstance(q.answer, dict)
            and public_question(q)["availability"] == "ready"
        )
        data = await self.request(
            "GET",
            "search/questions",
            params={
                "course_id": "math",
                "q": sample.id,
                "mode": "keyword",
                "limit": 20,
            },
        )
        ids = self.validate_search(data, "math", sample.id)
        require(ids[0] == sample.id, "公开多空题的完整题号没有排首")
        public = next(
            item["question"] for item in data["items"] if item["id"] == sample.id
        )
        require(
            public.get("blank_count") == len(sample.answer),
            "HTTP多空数量未公开或不匹配",
        )
        image_question = next(
            q
            for q in self.questions["math"].values()
            if public_question(q)["images"]
            and public_question(q)["availability"] == "ready"
        )
        image = public_question(image_question)["images"][0]
        response = await self.http.get(image["url"].removeprefix("/api/"))
        require(
            response.status_code == 200
            and response.content.startswith(b"\x89PNG\r\n\x1a\n"),
            "数学图片接口未返回PNG文件",
        )
        require(
            response.headers.get("content-type", "").startswith("image/png"),
            "图片媒体类型错误",
        )
        return {
            "ordered_blank_question": sample.id,
            "blank_count": public["blank_count"],
            "image_question": image_question.id,
            "image_url": image["url"],
            "image_bytes": len(response.content),
            "image_file_readable_only": True,
            "visual_teaching_correctness_verified": False,
        }

    async def cross_course_rejection(self):
        mathematical = next(iter(sorted(self.concepts["math"])))
        mechanical = "mechanics:c19"
        checks = [
            ("GET", "concepts/" + mechanical, {"params": {"course_id": "math"}}),
            (
                "GET",
                "search/questions",
                {"params": {"course_id": "math", "concept_ids": mechanical}},
            ),
            (
                "GET",
                "graph",
                {"params": {"course_id": "mechanics", "concept_ids": mathematical}},
            ),
            (
                "POST",
                "sessions",
                {
                    "json": {
                        "course_id": "math",
                        "concept_ids": [mechanical],
                        "goal": "课程范围验收",
                        "student_id": self.student_id,
                    }
                },
            ),
            (
                "POST",
                "sessions",
                {
                    "json": {
                        "course_id": "not_imported",
                        "concept_ids": [],
                        "goal": "未知课程验收",
                        "student_id": self.student_id,
                    }
                },
            ),
        ]
        for method, path, kwargs in checks:
            response = await self.http.request(method, path, **kwargs)
            require(
                response.status_code == 400,
                f"跨课或未知课程请求 {path} 未拒绝：HTTP{response.status_code}",
            )
        return {"requests_rejected": len(checks)}

    async def quarantined_practice(self):
        source = inspect.getsource(PracticeService.select)
        require(
            source.index('selected.get("availability")')
            < source.index("self.runner.run("),
            "不可练习题检查未位于模型调用之前；为避免模型调用，停止该检查",
        )
        output = {}
        for availability in (
            "ambiguous_options",
            "insufficient_options",
            "duplicate_options",
        ):
            question = next(
                q
                for q in self.questions["math"].values()
                if public_question(q)["availability"] == availability
            )
            session = await self.request(
                "POST",
                "sessions",
                json={
                    "course_id": "math",
                    "concept_ids": question.concept_ids[:6],
                    "goal": "数据隔离验收：" + availability,
                    "student_id": self.student_id,
                },
            )
            response = await self.http.post(
                "sessions/" + session["id"] + "/practice",
                params={"stream": "false"},
                json={"question_id": question.id, "cognitive_type": "recall"},
            )
            require(
                response.status_code == 400,
                f"隔离题 {question.id} 未被前置拒绝，HTTP{response.status_code}",
            )
            with db_session() as db:
                runs = list(
                    db.scalars(
                        select(AgentRun).where(AgentRun.session_id == session["id"])
                    )
                )
                persisted = db.get(LearningSession, session["id"])
                require(not runs, "隔离题触发了AgentRun，不能声称未调用模型")
                require(
                    persisted.student_id == self.student_id
                    and not persisted.pending_question_id,
                    "隔离题污染了待答状态或学生范围",
                )
            output[availability] = {
                "question_id": question.id,
                "session_id": session["id"],
                "http_status": response.status_code,
                "agent_runs": 0,
            }
        return output

    async def run(self):
        before = None
        try:
            before = local_evidence_fingerprint()
            self.report["local_evidence_before"] = before
            await self.check(
                "D01", "MySQL两课程原题、知识节点与公开材料数量", self.database_counts
            )
            await self.check(
                "D02",
                "数学65道来源异常与25道版式待审题及力学3道缺图题隔离",
                self.availability,
            )
            await self.check("D03", "数学多空结构和原始填写顺序", self.ordered_blanks)
            await self.check("D04", "ES课程范围内原题和材料数量", self.es_counts)
            await self.check(
                "D05", "全部公开ES文档无答案解析和学生私有字段", self.es_public_fields
            )
            await self.check(
                "D06", "Neo4j课程节点和分类关联关系数量", self.neo4j_counts
            )
            await self.check("D07", "HTTP课程目录与事实库一致", self.course_directory)
            await self.check(
                "D08", "两课程关键词题号检索及公开字段隔离", self.keyword_search
            )
            await self.check(
                "D09", "两课程hybrid只复用本地缓存并保持课程范围", self.hybrid_search
            )
            await self.check(
                "D10", "HTTP知识图保留分类与同题关联语义", self.graph_semantics
            )
            await self.check(
                "D11", "公开多空数量与数学PNG资源路由", self.public_blank_and_png
            )
            await self.check(
                "D12", "跨课程与未导入课程请求被拒绝", self.cross_course_rejection
            )
            await self.check(
                "D13", "三类隔离题练习前置拒绝且零AgentRun", self.quarantined_practice
            )
        finally:
            if before is not None:

                def untouched():
                    after = local_evidence_fingerprint()
                    require(
                        before == after,
                        "local档案在验收期间发生变化，请排查是否有并发使用；脚本仅使用evaluation_档案",
                    )
                    return {
                        "unchanged": True,
                        "sha256": after["sha256"],
                        "counts": after["counts"],
                    }

                await self.check("D14", "验收前后local学习证据和记忆未改变", untouched)
            await asyncio.gather(
                self.http.aclose(), self.search.close(), self.graph.close()
            )
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.report["complete"] = len(self.report["checks"]) == 14
            self.save()
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/api")
    args = parser.parse_args()
    result = asyncio.run(CourseAcceptance(args.base_url).run())
    print(json.dumps(result["summary"], ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["complete"] and not result["summary"]["failed"] else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
