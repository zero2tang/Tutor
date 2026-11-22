"""验证通用教学资料工具的真实数据库读取、来源、角色与待答隔离。"""

from copy import deepcopy
from types import SimpleNamespace
from datetime import datetime
from reference_helpers import get_pack, save_pack
from hashlib import sha256
import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.infrastructure.persistence import database
from app.infrastructure.persistence.database import Base, db_session
from app.domains.courses.models import Concept, Course, Material, Question
from app.domains.sessions.models import LearningSession
from app.domains.materials.models import TextbookDocument
from app.domains.materials.resources.service import TeachingResourceService
from app.agent.tools.contracts import ToolContext
from app.application.agent.tools import TeachingTools
from native_tools import invoke_tool


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@pytest.fixture
def resources(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "resources.db"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", lambda: engine)
    source = {
        "document_id": "document",
        "page": 8,
        "locator": "page:8/example:1",
        "source_uri": "fixture://chapter#page8",
    }
    parsed = {
        "document": {"id": "document", "content_hash": "content-hash"},
        "blocks": [
            {
                "id": "definition",
                "kind": "definition",
                "text": "分配律：a(b+c)=ab+ac。",
                "source": source,
            },
            {
                "id": "technique",
                "kind": "technique",
                "text": "整体处理：当同一子式重复出现时先看作整体。",
                "source": source,
            },
            {
                "id": "stem",
                "kind": "example",
                "text": "例1：2(x-3)+3(x-3)=20。",
                "source": source,
            },
            {
                "id": "solution",
                "kind": "solution",
                "text": "x=7，这是来源原解析。",
                "source": source,
                "images": [{"source_description": "UNTRUSTED ALT ANSWER"}],
            },
        ],
        "exercises": [
            {
                "id": "source-question",
                "source_block_ids": ["stem"],
                "solution_block_ids": ["solution"],
            }
        ],
    }
    payload = {
        "document_id": "document",
        "provenance": {"document_content_hash": "content-hash"},
        "source_refs": [
            {"block_id": "stem", **source},
            {"block_id": "solution", **source},
        ],
        "solution_paths": [
            {
                "id": "overall",
                "steps": [
                    {"equation": "x=7", "provenance": {"origin": "derived_by_rule"}}
                ],
            }
        ],
        "concepts": [{"id": "distribution", "name": "分配律"}],
        "techniques": [
            {
                "id": "whole",
                "name": "整体处理",
                "basis": "分配律反向使用",
                "conditions": ["相同子式重复"],
                "failure_conditions": ["子式不同"],
                "source_refs": [{"block_id": "stem", **source}],
                "provenance": {"origin": "model_induction"},
            }
        ],
        "verification": {"method": "historical_archive", "result": "not_rechecked"},
    }
    with db_session() as db:
        db.add_all(
            [
                Course(
                    id="course",
                    title="课程",
                    description="测试",
                    source_ref="fixture://course",
                ),
                Course(
                    id="other",
                    title="其他课程",
                    description="测试",
                    source_ref="fixture://other",
                ),
            ]
        )
        db.add(
            Concept(
                id="course:distribution",
                course_id="course",
                source_id="distribution",
                name="分配律",
                summary="去括号时使用分配律。",
                topic_type="concept",
                module="方程",
            )
        )
        db.add(
            Concept(
                id="other:concept",
                course_id="other",
                source_id="c",
                name="其他秘密",
                summary="CROSS COURSE",
                module="其他",
            )
        )
        for qid, course, validation in [
            ("course:example", "course", {"teaching_pack_id": "pack"}),
            ("course:pending", "course", {}),
            ("other:example", "other", {}),
        ]:
            db.add(
                Question(
                    id=qid,
                    course_id=course,
                    source_id=qid[-10:],
                    stem=(
                        "2(x-3)+3(x-3)=20" if qid == "course:example" else "分配律练习"
                    ),
                    answer=["SECRET PENDING" if "pending" in qid else "7"],
                    explanation="参考原解析",
                    options={},
                    question_type="fill",
                    concept_ids=["course:distribution"] if course == "course" else [],
                    source_ref="fixture://example",
                    generated=False,
                    validation=validation,
                )
            )
        db.add_all(
            [
                Material(
                    id="public",
                    course_id="course",
                    title="分配律说明",
                    text="公开教材知识正文",
                    concept_ids=["course:distribution"],
                    source_ref="fixture://material",
                    visibility="public",
                    origin="textbook_rule",
                ),
                Material(
                    id="private",
                    course_id="course",
                    title="分配律私有",
                    text="PRIVATE MATERIAL",
                    concept_ids=[],
                    source_ref="fixture://private",
                    visibility="private",
                ),
                Material(
                    id="linked",
                    course_id="course",
                    title="分配律题解",
                    text="PENDING EXPLANATION",
                    concept_ids=[],
                    source_ref="fixture://linked",
                    visibility="public",
                    question_id="course:pending",
                ),
            ]
        )
        db.add(
            TextbookDocument(
                id="document",
                course_id="course",
                title="真实来源测试章",
                edition="test",
                source_uri="fixture://chapter",
                source_kind="self_authored",
                format="markdown",
                content_hash="content-hash",
                raw_content=b"test",
                parser_version="test",
                status="parsed",
                parsed=parsed,
                provenance={},
            )
        )
        db.flush()
        q = db.get(Question, "course:example")
        payload["problem"] = {"stem":q.stem}
        payload["source_answer"] = {"value":q.answer}
        payload["source_solution"] = []
        save_pack(db, SimpleNamespace(id="pack", question_id=q.id, document_id="document",
            source_question_id="source-question", status="ready", payload_hash=digest(payload),
            payload=payload, created_at=datetime.now()))
        db.add(
            LearningSession(
                id="session",
                course_id="course",
                student_id="student",
                title="测试会话",
                goal="学习",
                concept_ids=[],
                pending_question_id="course:pending",
                pending_context={},
                agenda=[],
            )
        )
    yield TeachingResourceService()
    engine.dispose()


def test_search_reads_course_resources_without_elasticsearch_or_answer_disclosure(
    resources,
):
    result = resources.search("course", "分配律", limit=8)
    assert result["items"]
    assert any(item["kind"] == "example" for item in result["items"])
    assert any(item["kind"] == "concept" for item in result["items"])
    assert "PRIVATE MATERIAL" not in json.dumps(result)
    assert "PENDING EXPLANATION" not in json.dumps(result)
    assert "reference_answer" not in json.dumps(result)
    assert all(
        item["citation"]["course_id"] == "course" and item["source_refs"]
        for item in result["items"]
    )
    assert result["citations"] == [item["citation"] for item in result["items"]]
    assert all(
        item["citation"]["id"] == item["resource_id"] for item in result["items"]
    )
    assert (
        resources.search("course", "分配律")["items"]
        == resources.search("course", "分配律")["items"]
    )


def test_reference_example_returns_original_and_derived_materials_separately(resources):
    result = resources.read("course", "teach:question:course:example")
    assert result["content"]["reference_answer"] == ["7"]
    assert result["content"]["textbook_blocks"][-1]["text"].startswith("x=7")
    assert (
        result["content"]["textbook_blocks"][-1]["provenance"]
        == "textbook_transcription"
    )
    assert (
        result["content"]["solution_paths"][0]["steps"][0]["provenance"]["origin"]
        == "derived_by_rule"
    )
    assert "UNTRUSTED ALT ANSWER" not in json.dumps(result)
    assert result["usage"] == "internal_teaching_reference"
    assert result["citations"] == [result["citation"]]
    assert {ref["block_id"] for ref in result["source_refs"]} == {"stem", "solution"}


def test_strategy_keeps_model_induction_and_applicability_conditions(resources):
    found = resources.search("course", "整体处理", kind="strategies")
    assert len(found["items"]) >= 2
    result = resources.read("course", "teach:strategy:pack:whole")
    assert result["origin"] == "model_induction"
    assert result["content"]["conditions"] == ["相同子式重复"]
    assert result["content"]["failure_conditions"] == ["子式不同"]
    explicit = resources.read("course", "teach:block:document:technique")
    assert explicit["origin"] == "textbook_explicit"


@pytest.mark.parametrize(
    "identifier",
    [
        "teach:question:other:example",
        "teach:concept:other:concept",
        "teach:material:private",
        "teach:material:linked",
        "teach:block:document:solution",
        "teach:block:document:stem",
    ],
)
def test_cross_course_private_or_direct_solution_reads_are_denied(
    resources, identifier
):
    with pytest.raises((PermissionError, ValueError)):
        resources.read("course", identifier)


def test_explicit_exclusion_prevents_reference_answers_and_derived_strategies(
    resources,
):
    scope = {"excluded_question_ids": ["course:example"]}
    with pytest.raises(PermissionError):
        resources.read("course", "teach:question:course:example", **scope)
    with pytest.raises(PermissionError):
        resources.read("course", "teach:strategy:pack:whole", **scope)
    found = resources.search("course", "分配律", kind="examples", **scope)
    assert all(
        item["resource_id"] != "teach:question:course:example"
        for item in found["items"]
    )


def test_active_pending_is_excluded_even_if_call_context_forgets_it(resources):
    scope = {"session_id": "session", "student_id": "student"}
    with pytest.raises(PermissionError):
        resources.read("course", "teach:question:course:pending", **scope)
    found = resources.search("course", "分配律", kind="examples", **scope)
    assert all(
        item["resource_id"] != "teach:question:course:pending"
        for item in found["items"]
    )
    with pytest.raises(PermissionError):
        resources.search(
            "course", "分配律", session_id="session", student_id="someone-else"
        )


def test_archival_reads_check_source_integrity_without_invoking_math_verifier(
    resources, monkeypatch
):
    # 存档特意使用未经当前数学校验的标记；读取只检查来源和内容指纹。
    assert resources.read("course", "teach:question:course:example")["content"][
        "reference_answer"
    ] == ["7"]
    with db_session() as db:
        pack = get_pack(db, "pack")
        payload = deepcopy(pack.payload)
        payload["secret"] = "tampered"
        q = db.get(Question, pack.question_id)
        q.teaching_reference = {**q.teaching_reference, "reference_hash":"corrupted"}
    with pytest.raises(ValueError, match="指纹"):
        resources.read("course", "teach:question:course:example")


@pytest.mark.asyncio
async def test_tool_registration_execution_scope_and_roles(resources):
    tools = TeachingTools(None)
    context = ToolContext(
        course_id="course", student_id="student", session_id="session"
    )
    for role in ("knowledge", "exercise"):
        names = {item.name for item in tools.for_role(role)}
        assert {"search_teaching_resources", "read_teaching_resource"} <= names
        result = await invoke_tool(tools,
            role,
            "search_teaching_resources",
            {"query": "整体处理", "kind": "strategies"},
            context,
        )
        assert result["items"]
        result = await invoke_tool(tools,
            role,
            "read_teaching_resource",
            {"resource_id": "teach:question:course:example"},
            context,
        )
        assert result["content"]["reference_explanation"] == "参考原解析"
    with pytest.raises(PermissionError):
        await invoke_tool(tools,
            "assessment",
            "read_teaching_resource",
            {"resource_id": "teach:question:course:example"},
            context,
        )
    with pytest.raises(ValueError):
        await invoke_tool(tools,
            "exercise", "search_teaching_resources", {"query": "test", "limit": 9}, context
        )


def test_empty_search_is_not_a_claim_that_teaching_is_impossible(resources):
    result = resources.search("course", "NO-SUCH-RESOURCE", kind="examples")
    assert result["items"] == []
    assert "不表示无法讲解" in result["notice"]


@pytest.mark.parametrize(
    "pending",
    [
        {"exercise_batch": {"items": [{"contract": {"id": "course:example"}}]}},
    ],
)
def test_batch_answers_cannot_be_read_as_reference_examples(resources, pending):
    with db_session() as db:
        session = db.get(LearningSession, "session")
        session.pending_question_id = None
        session.pending_context = pending
    with pytest.raises(PermissionError):
        resources.read(
            "course",
            "teach:question:course:example",
            session_id="session",
            student_id="student",
        )
    with pytest.raises(PermissionError):
        resources.read(
            "course",
            "teach:strategy:pack:whole",
            session_id="session",
            student_id="student",
        )


def test_retired_specialized_state_does_not_occupy_current_reference_scope(resources):
    with db_session() as db:
        session = db.get(LearningSession, "session")
        session.pending_question_id = None
        session.pending_context = {
            "equation_tutoring": {
                "status": "working",
                "contract": {"id": "course:example"},
            }
        }
    result = resources.read(
        "course",
        "teach:question:course:example",
        session_id="session",
        student_id="student",
    )
    assert result["content"]["reference_answer"] == ["7"]
    assert result["usage"] == "internal_teaching_reference"
