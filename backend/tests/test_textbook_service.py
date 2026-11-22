"""教材来源导入、目录投影、版本幂等与私有原解边界，不调用数学求解器。"""

from copy import deepcopy
from reference_helpers import get_pack, save_pack
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, func, select

from app.infrastructure.persistence import database
from app.infrastructure.persistence.database import Base, db_session
from app.domains.courses.models import Course, Material, Question
from app.domains.materials.models import TextbookDocument, TextbookImportReceipt
from app.domains.materials.schemas import TextbookImportRequest
from app.domains.materials.teaching_packs import TeachingPackService, payload_hash
from app.domains.materials.textbooks import TextbookService
from app.domains.materials.catalog_projection import catalog_hash

CHAPTER = """# 自编来源测试章

定义：等式表示左右两边相等。

性质：等式两边同时加减相同的量，等式仍然成立。

例1：解方程 $2(x+1)=8$。

解：
$2x+2=8$
$x=99$

例2：沿400米跑道走一圈，比较路程和位移。

答案：路程400米，位移大小为0。

例3：证明三角形全等，说明所用条件。
"""


@pytest.fixture
def textbooks(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "textbooks.db").as_posix())
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", lambda: engine)
    with db_session() as db:
        for course in ("junior_algebra", "mechanics"):
            db.add(
                Course(
                    id=course,
                    title=course,
                    description="明确自编隔离测试课程",
                    source_ref="fixture://" + course,
                )
            )
    yield TextbookService()
    engine.dispose()


def body(**changes):
    values = {
        "request_id": str(uuid4()),
        "title": "明确自编测试章",
        "edition": "test-v1",
        "source_uri": "fixture://source-chapter",
        "source_kind": "self_authored",
        "format": "markdown",
        "content": CHAPTER,
    }
    values.update(changes)
    return TextbookImportRequest(**values)


def test_import_preserves_original_answers_and_reference_only_arbitrary_problems(
    textbooks,
):
    document = textbooks.import_document(body())["document"]
    assert document["exercise_count"] == 3
    assert document["ready_pack_count"] == 2
    assert document["reference_pack_count"] == 1
    assert document["draft_pack_count"] == 0
    assert document["provenance"]["mathematics_checked"] is False
    assert document["provenance"]["vision_calls"] == 0
    with db_session() as db:
        questions = {row.source_id: row for row in db.scalars(select(Question))}
        assert questions["1"].answer == ["x=99"]
        assert questions["2"].answer == ["路程400米，位移大小为0。"]
        assert questions["3"].answer == []
        assert questions["3"].validation["availability"] == "ready"
        assert questions["3"].validation["analysis_status"] == "reference_only"
        assert "x=99" in questions["1"].explanation
        assert all(row.question_type == "open" for row in questions.values())
        assert all(not row.concept_ids for row in questions.values())
    references = TeachingPackService().list(document_id=document["id"])["items"]
    assert len(references) == 3
    assert all(
        not pack["solution_paths"] and not pack["concepts"] for pack in references
    )
    assert all(pack["verification"]["result"] == "not_checked" for pack in references)
    for pack in references:
        assert "source_answer" not in pack and "source_solution" not in pack
        assert "derived_answer" not in pack and "reference_solution" not in str(pack)
    reference_only = next(
        pack for pack in references if pack["status"] == "reference_only"
    )
    assert (
        TeachingPackService().internal(reference_only["question_id"])["problem"][
            "number"
        ]
        == "3"
    )


def test_receipt_source_kind_parser_and_catalog_version_are_idempotent(
    textbooks, monkeypatch
):
    request = body()
    first = textbooks.import_document(request)["document"]
    assert textbooks.import_document(request)["replayed"]
    second = textbooks.import_document(
        request.model_copy(update={"request_id": str(uuid4())})
    )
    assert second["document"]["id"] == first["id"]
    with pytest.raises(ValueError, match="标识"):
        textbooks.import_document(request.model_copy(update={"edition": "other"}))
    other_origin = textbooks.import_document(
        request.model_copy(
            update={"request_id": str(uuid4()), "source_kind": "uploaded"}
        )
    )["document"]
    assert other_origin["id"] != first["id"]
    monkeypatch.setattr("app.domains.materials.parsing.parser.PARSER_VERSION", "rules-next")
    new_parser = textbooks.import_document(
        request.model_copy(update={"request_id": str(uuid4())})
    )["document"]
    assert new_parser["id"] != first["id"]
    monkeypatch.setattr("app.domains.materials.catalog_projection.VERSION", "textbook-catalog-next")
    new_catalog = textbooks.import_document(
        request.model_copy(update={"request_id": str(uuid4())})
    )["document"]
    assert new_catalog["id"] != new_parser["id"]
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(TextbookDocument)) == 4
        assert db.scalar(select(func.count()).select_from(Question)) == 12
        assert db.scalar(select(func.count()).select_from(TextbookImportReceipt)) == 5


def test_public_rule_materials_do_not_include_private_original_solutions(textbooks):
    document = textbooks.import_document(body())["document"]
    rule = next(block for block in document["blocks"] if block["kind"] == "rule")
    assert "同时加减" in textbooks.block(document["id"], rule["id"])["text"]
    solution = next(
        block for block in document["blocks"] if block["kind"] == "solution"
    )
    with pytest.raises(ValueError, match="公开答案"):
        textbooks.block(document["id"], solution["id"])
    with db_session() as db:
        materials = list(db.scalars(select(Material)))
        assert materials
        assert all(
            "x=99" not in material.text and "路程400米" not in material.text
            for material in materials
        )


def test_cross_course_access_and_mutated_payload_are_rejected(textbooks):
    document = textbooks.import_document(body())["document"]
    pack = TeachingPackService().list(document_id=document["id"])["items"][0]
    with pytest.raises(ValueError, match="课程"):
        TeachingPackService().get(pack["id"], "mechanics")
    with pytest.raises(ValueError, match="课程"):
        TeachingPackService().internal(pack["question_id"], "mechanics")
    with db_session() as db:
        row = get_pack(db, pack["id"])
        changed = deepcopy(row.payload)
        changed["source_answer"]["value"] = "伪造后的来源答案"
        question = db.get(Question, row.question_id)
        question.teaching_reference = {**question.teaching_reference, "reference_hash": "corrupted"}
    with pytest.raises(ValueError, match="指纹"):
        TeachingPackService().get(pack["id"])


def test_mismatched_document_content_hash_is_rejected(textbooks):
    document = textbooks.import_document(body())["document"]
    pack = TeachingPackService().list(document_id=document["id"])["items"][0]
    with db_session() as db:
        db.get(TextbookDocument, document["id"]).content_hash = "corrupted"
    with pytest.raises(ValueError, match="来源内容指纹"):
        TeachingPackService().get(pack["id"])


def test_legacy_program_derived_archive_remains_readable_with_original_provenance(
    textbooks,
):
    document = textbooks.import_document(body())["document"]
    pack = TeachingPackService().list(document_id=document["id"])["items"][0]
    with db_session() as db:
        row = get_pack(db, pack["id"])
        value = deepcopy(row.payload)
        value["version"] = "teaching-pack-v2"
        value["provenance"]["origin"] = "rule_based_analysis"
        value["verification"] = {"result": "correct", "method": "legacy_math"}
        value["solution_paths"] = [
            {
                "id": "old",
                "label": "历史规则推导",
                "steps": [{"equation": "x=999"}],
                "requirements": {},
                "verification": {"result": "correct", "method": "legacy_math"},
            }
        ]
        value["hash"] = catalog_hash(
            {key: part for key, part in value.items() if key != "hash"}
        )
        row.payload, row.payload_hash = value, payload_hash(value)
        save_pack(db, row)
    public = TeachingPackService().get(pack["id"])
    assert public["provenance"]["origin"] == "rule_based_analysis"
    assert public["verification"]["result"] == "not_checked"
    assert public["verification"]["archive_verification"]["method"] == "legacy_math"
    assert "steps" not in public["solution_paths"][0]
    assert (
        TeachingPackService().internal(pack["question_id"])["solution_paths"][0][
            "steps"
        ][0]["equation"]
        == "x=999"
    )


def test_internal_rejects_source_blocking_issues_but_not_missing_answer(textbooks):
    document = textbooks.import_document(body())["document"]
    packs = TeachingPackService().list(document_id=document["id"])["items"]
    reference = next(pack for pack in packs if pack["status"] == "reference_only")
    assert (
        TeachingPackService().internal(reference["question_id"])["source_answer"][
            "value"
        ]
        is None
    )
    with db_session() as db:
        row = get_pack(db, reference["id"])
        value = deepcopy(row.payload)
        value["issues"].append(
            {
                "code": "unresolved_source",
                "severity": "blocking",
                "message": "原文块缺失",
            }
        )
        value["status"] = "draft"
        value["hash"] = catalog_hash(
            {key: part for key, part in value.items() if key != "hash"}
        )
        row.payload, row.payload_hash, row.status = value, payload_hash(value), "draft"
        save_pack(db, row)
    with pytest.raises(ValueError, match="来源结构不完整"):
        TeachingPackService().internal(reference["question_id"])


@pytest.mark.asyncio
async def test_http_import_public_catalog_and_extra_fields_boundary(textbooks):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from app.api.routes.textbooks import router

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(ValueError)
    async def handle_value_error(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = body()
        response = await client.post("/api/textbooks/import", json=request.model_dump())
        assert response.status_code == 200, response.text
        identifier = response.json()["document"]["id"]
        listed = await client.get(f"/api/teaching-packs?document_id={identifier}")
        public = listed.json()["items"]
        assert len(public) == 3
        assert all(
            "source_answer" not in item and "source_solution" not in item
            for item in public
        )
        assert (
            await client.get(
                f"/api/teaching-packs/{public[0]['id']}?course_id=mechanics"
            )
        ).status_code == 400
        assert (
            await client.post(
                "/api/textbooks/import",
                json={**request.model_dump(), "server_path": "C:/private"},
            )
        ).status_code == 422


def test_inline_solution_is_private_while_raw_source_and_problem_conditions_are_preserved(
    textbooks,
):
    original = "<div data-kind='exercise'><p>已知$x=4$，求$2x$。<span data-kind='solution'>答案：$2x=8$</span></p></div>"
    document = textbooks.import_document(body(format="html", content=original))[
        "document"
    ]
    pack = TeachingPackService().list(document_id=document["id"])["items"][0]
    assert pack["problem"]["stem"] == "已知$x=4$，求$2x$。"
    with db_session() as db:
        assert (
            db.get(TextbookDocument, document["id"]).raw_content.decode("utf-8")
            == original
        )
        question = db.get(Question, pack["question_id"])
        assert question.stem == pack["problem"]["stem"]
        assert "答案" not in question.stem and "2x=8" not in question.stem
        assert "2x=8" in question.explanation


def test_listing_sorts_only_lightweight_identifiers_instead_of_large_json_archives(
    textbooks,
):
    from sqlalchemy import event

    textbooks.import_document(body())
    statements = []
    engine = database.engine()

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if "ORDER BY" in statement and (
            Question.__tablename__ in statement
            or TextbookDocument.__tablename__ in statement
        ):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert len(textbooks.list()["items"]) == 1
        assert len(TeachingPackService().list()["items"]) == 3
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) >= 1
    assert all(
        ".payload" not in statement
        and ".parsed" not in statement
        and ".raw_content" not in statement
        for statement in statements
    )
