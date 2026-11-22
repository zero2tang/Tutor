"""规则解析在事务外执行，教材来源与只读目录投影一次提交。"""

import base64
import binascii
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from .teaching_reference import pack_views, make_reference
from hashlib import sha256
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.infrastructure.persistence.database import db_session
from app.domains.courses.models import Material, Question
from app.domains.materials.models import TextbookDocument, TextbookImportReceipt
from app.domains.materials.schemas import TextbookImportRequest
from app.domains.courses.catalog import CatalogService
from app.domains.materials.teaching_packs import payload_hash
from app.domains.materials.catalog_projection import project_catalog, verify_catalog_pack


def document_view(row, packs) -> dict:
    parsed = row.parsed
    return {
        "id": row.id,
        "course_id": row.course_id,
        "title": row.title,
        "edition": row.edition,
        "source_uri": row.source_uri,
        "source_kind": row.source_kind,
        "format": row.format,
        "content_hash": row.content_hash,
        "parser_version": row.parser_version,
        "status": row.status,
        "provenance": row.provenance,
        "chapters": parsed.get("chapters", []),
        "stats": parsed.get("stats", {}),
        "block_count": len(parsed.get("blocks", [])),
        "exercise_count": len(parsed.get("exercises", [])),
        "ready_pack_count": sum(pack.status == "ready" for pack in packs),
        "draft_pack_count": sum(pack.status == "draft" for pack in packs),
        "reference_pack_count": sum(pack.status == "reference_only" for pack in packs),
        "issues": [
            {
                key: issue.get(key)
                for key in ("code", "severity", "message", "page", "block_id")
            }
            for issue in parsed.get("issues", [])
        ],
        "fallback_regions": parsed.get("fallback_regions", []),
        "blocks": [
            {
                "id": block["id"],
                "kind": block["kind"],
                "chapter_id": block.get("chapter_id"),
                "ordinal": block.get("ordinal"),
                "source": block.get("source", {}),
            }
            for block in parsed.get("blocks", [])
        ],
    }


class TextbookService:
    def list(self, course_id="junior_algebra") -> dict:
        with db_session() as db:
            identifiers = list(
                db.scalars(
                    select(TextbookDocument.id)
                    .where(TextbookDocument.course_id == course_id)
                    .order_by(
                        TextbookDocument.created_at.desc(),
                        TextbookDocument.parser_version.desc(),
                    )
                )
            )
            # 原始教材与解析JSON不参与MySQL排序；先取得有序ID再读取不可变快照。
            rows = [db.get(TextbookDocument, identifier) for identifier in identifiers]
            packs = pack_views(db, course_id)
            # 默认目录展示同一来源的最新解析版本，旧快照仍可按ID恢复既有会话。
            latest, seen = [], set()
            for row in rows:
                key = (
                    row.title,
                    row.edition,
                    row.source_uri,
                    row.content_hash,
                    row.source_kind,
                )
                if key not in seen:
                    latest.append(row)
                    seen.add(key)
            return {
                "items": [
                    document_view(
                        row, [pack for pack in packs if pack.document_id == row.id]
                    )
                    for row in latest
                ]
            }

    def get(self, identifier, course_id="junior_algebra") -> dict:
        with db_session() as db:
            row = db.get(TextbookDocument, identifier)
            if row is None or row.course_id != course_id:
                raise ValueError("教材不存在或不属于当前课程。")
            packs = pack_views(db, course_id, identifier)
            return document_view(row, packs)

    def block(self, identifier, block_id, course_id="junior_algebra") -> dict:
        """正文公开阅读只开放定义和规则；题目解答通过教学发布流程提供。"""
        with db_session() as db:
            row = db.get(TextbookDocument, identifier)
            if row is None or row.course_id != course_id:
                raise ValueError("教材不存在或不属于当前课程。")
            block = next(
                (item for item in row.parsed["blocks"] if item["id"] == block_id), None
            )
            private_ids = {
                bid
                for ex in row.parsed.get("exercises", [])
                for bid in ex.get("source_block_ids", [])
                + ex.get("solution_block_ids", [])
            }
            if (
                not block
                or block["kind"]
                not in {
                    "heading",
                    "definition",
                    "rule",
                    "theorem",
                    "concept",
                    "technique",
                }
                or block_id in private_ids
            ):
                raise ValueError("该来源块需要通过习题教学流程阅读，不能直接公开答案。")
            return deepcopy(block)

    def import_document(self, request: TextbookImportRequest) -> dict:
        from app.domains.materials.parsing.parser import PARSER_VERSION
        from app.domains.materials.catalog_projection import VERSION as CATALOG_VERSION

        CatalogService().course(request.course_id)
        body = request.model_dump(exclude={"request_id"})
        digest = payload_hash(body)
        identifier = sha256(
            (request.course_id + digest + PARSER_VERSION + CATALOG_VERSION).encode()
        ).hexdigest()
        with db_session() as db:
            prior = db.scalar(
                select(TextbookImportReceipt).where(
                    TextbookImportReceipt.course_id == request.course_id,
                    TextbookImportReceipt.request_key == request.request_id,
                )
            )
            if prior and prior.request_hash != digest:
                raise ValueError("同一个导入请求标识不能对应不同教材。")
            exists = db.get(TextbookDocument, identifier) is not None
        if prior:
            return {
                "document": self.get(prior.document_id, request.course_id),
                "replayed": True,
            }
        if not exists:
            from app.domains.materials.parsing.parser import parse_document

            try:
                raw = (
                    base64.b64decode(request.content_base64, validate=True)
                    if request.content_base64 is not None
                    else request.content.encode("utf-8")
                )
            except (ValueError, binascii.Error) as exc:
                raise ValueError("教材二进制编码无效。") from exc
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("试点单次导入最多 8 MiB，请按章节拆分。")
            parsed = parse_document(
                raw if request.format == "pdf" else raw.decode("utf-8"),
                format=request.format,
                title=request.title,
                edition=request.edition,
                source_uri=request.source_uri,
                source_kind=request.source_kind,
                document_id=identifier,
            )
            packs = project_catalog(parsed)
            for pack in packs:
                verify_catalog_pack(pack, parsed)
        try:
            with db_session() as db:
                if not exists:
                    row = TextbookDocument(
                        id=identifier,
                        course_id=request.course_id,
                        title=request.title,
                        edition=request.edition,
                        source_uri=request.source_uri,
                        source_kind=request.source_kind,
                        format=request.format,
                        content_hash=sha256(raw).hexdigest(),
                        raw_content=raw,
                        parser_version=parsed["document"]["parser_version"],
                        status=(
                            "parsed_with_issues" if parsed.get("issues") else "parsed"
                        ),
                        parsed=parsed,
                        provenance={
                            "license": request.license,
                            "attribution": request.attribution,
                            "vision_calls": 0,
                            "rule_first": True,
                            "teacher_reviewed": False,
                            "mathematics_checked": False,
                            "catalog_version": CATALOG_VERSION,
                        },
                    )
                    db.add(row)
                    self._project(db, request.course_id, identifier, packs, parsed)
                db.add(
                    TextbookImportReceipt(
                        course_id=request.course_id,
                        request_key=request.request_id,
                        request_hash=digest,
                        document_id=identifier,
                    )
                )
        except IntegrityError:
            # 并发重放仅在完整同指纹来源已提交时复用；其他冲突保留错误。
            with db_session() as db:
                receipt = db.scalar(
                    select(TextbookImportReceipt).where(
                        TextbookImportReceipt.course_id == request.course_id,
                        TextbookImportReceipt.request_key == request.request_id,
                    )
                )
                if receipt is None or receipt.request_hash != digest:
                    raise ValueError("教材导入发生并发冲突，请使用原请求标识重试。")
        return {"document": self.get(identifier, request.course_id), "replayed": exists}

    def _project(self, db, course_id, document_id, packs, parsed):
        for pack in packs:
            qid = f"{course_id}:tb:{sha256((document_id + pack['question_id']).encode()).hexdigest()[:40]}"
            pid = sha256((course_id + document_id + pack["hash"]).encode()).hexdigest()
            reference = make_reference(SimpleNamespace(id=pid, document_id=document_id,
                source_question_id=pack["question_id"], status=pack["status"],
                payload_hash=payload_hash(pack), payload=pack, created_at=datetime.now()))
            source_answer = deepcopy(pack.get("source_answer", {}).get("value"))
            answer = (
                []
                if source_answer is None
                else (
                    source_answer
                    if isinstance(source_answer, (list, dict))
                    else [source_answer]
                )
            )
            available = pack["status"] != "draft"
            validation = {
                "teaching_pack_id": pid,
                "textbook_document_id": document_id,
                "guided_reference": True,
                "analysis_status": pack["status"],
                "data_origin": parsed["document"]["source_kind"],
                "availability": "ready" if available else "source_incomplete",
                "source_answer_available": source_answer is not None,
                "mathematics_checked": False,
                "availability_note": (
                    "教材参考资料；答案与解析来自原文，未作数学核验。"
                    if available
                    else "原文定位或来源结构不完整，需修复后再作教学参考。"
                ),
            }
            db.add(
                Question(
                    id=qid,
                    course_id=course_id,
                    source_id=str(pack["problem"].get("number") or "")[:60],
                    stem=pack["problem"]["stem"],
                    options={},
                    answer=answer,
                    question_type="open",
                    cognitive_type="unknown",
                    concept_ids=[],
                    explanation="\n".join(
                        item.get("text", "") for item in pack.get("source_solution", [])
                    ),
                    source_ref=(
                        pack.get("source_refs")
                        or [{"source_uri": parsed["document"]["source_uri"]}]
                    )[0].get("source_uri", "")
                    + " / "
                    + str(pack["problem"].get("number", "")),
                    generated=False,
                    validation=validation,
                    teaching_reference=reference,
                )
            )
        # 只将独立定义与规则作为公开材料，例题答案不进入通用公开RAG投影。
        private_ids = {
            bid
            for ex in parsed.get("exercises", [])
            for bid in ex.get("source_block_ids", []) + ex.get("solution_block_ids", [])
        }
        for block in parsed.get("blocks", []):
            if (
                block["kind"]
                in {"definition", "rule", "theorem", "concept", "technique"}
                and block["id"] not in private_ids
            ):
                mid = (
                    "textbook:"
                    + sha256((document_id + block["id"]).encode()).hexdigest()[:48]
                )
                db.add(
                    Material(
                        id=mid,
                        course_id=course_id,
                        title=block["kind"] + " · " + block["text"][:80],
                        text=block["text"],
                        concept_ids=[],
                        source_ref=block.get("source", {}).get(
                            "source_uri", parsed["document"]["source_uri"]
                        )
                        + "#"
                        + block.get("source", {})
                        .get("locator", block["id"])
                        .lstrip("#"),
                        origin="textbook_rule",
                        visibility="public",
                        version=1,
                    )
                )
