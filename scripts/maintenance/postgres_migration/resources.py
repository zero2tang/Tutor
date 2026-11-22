"""教学资源白名单、引用闭包和本地资产验证；学生历史留在完整备份。"""

from pathlib import Path

from .codec import digest, file_digest, normalize_row

RESOURCE_TABLES = frozenset({
    "courses", "concepts", "questions", "materials", "concept_relations",
    "textbook_documents", "textbook_import_receipts", "textbook_extraction_sections",
    "exercise_search_assets", "exercise_search_representations", "exercise_search_projections",
    "index_tasks", "model_calls",
})
RESET_TABLES = frozenset({
    "agent_runs", "attempts", "batch_submissions", "evidence_concept_links", "learning_sessions",
    "messages", "session_goal_states", "student_states", "memory_items", "memory_facts",
    "memory_jobs", "scope_memories", "session_concept_observations", "session_memory_extractions",
})


def keep_resource(table, row):
    if table == "index_tasks":
        return row["target"] in {"es", "neo4j", "question_search"}
    if table == "model_calls":
        return row["purpose"] == "textbook" or (row["purpose"] == "question_search" and row["stage"] == "extract")
    return table in RESOURCE_TABLES


def select_resources(blocks, metadata, *, check_assets=True):
    if set(blocks) != RESOURCE_TABLES | RESET_TABLES:
        raise ValueError("快照表清单不是已审核的完整 27 表；请先审查新增或缺失表")
    if not RESOURCE_TABLES <= set(metadata.tables):
        raise ValueError("目标模型缺少教学资源表")
    selected = {name: [normalize_row(metadata.tables[name], row) for row in blocks[name]
                       if keep_resource(name, row)] for name in sorted(RESOURCE_TABLES)}
    references = validate_references(selected)
    assets = validate_assets(selected) if check_assets else {"checked": False}
    return selected, {
        "tables": {name: {"source_rows": len(blocks[name]), "rows": len(rows),
                           "excluded_rows": len(blocks[name]) - len(rows), "sha256": digest(rows)}
                   for name, rows in selected.items()},
        "reset_tables": {name: len(blocks[name]) for name in sorted(RESET_TABLES)},
        "references": references, "assets": assets,
        "resource_rows": sum(map(len, selected.values())),
        "policy": "resources-only-v1", "generated_questions": "preserved_without_changing_access",
    }


def validate_references(data):
    ids = {name: {row["id"] for row in rows} for name, rows in data.items()}
    checks = 0

    def require(table, key, *, optional=False):
        nonlocal checks
        if optional and not key:
            return
        if key not in ids[table]:
            raise ValueError("资源引用不存在：" + table)
        checks += 1

    for name, rows in data.items():
        for row in rows:
            if "course_id" in row:
                require("courses", row["course_id"], optional=name == "index_tasks")
            if "question_id" in row:
                require("questions", row["question_id"], optional=name == "materials")
            for concept_id in row.get("concept_ids", []):
                require("concepts", concept_id)
            if name == "concept_relations":
                require("concepts", row["source_id"])
                require("concepts", row["target_id"])
            if name == "textbook_import_receipts":
                require("textbook_documents", row["document_id"])
            if name == "textbook_extraction_sections":
                require("textbook_documents", row["book_id"])
                if row["extraction_status"] in {"running", "pending"} or row["lease_until"]:
                    raise ValueError("教材提取未静止，拒绝迁移")
            if name == "index_tasks" and (row["status"] in {"running", "building", "pending"} or row["lease_until"]):
                raise ValueError("索引任务未静止，拒绝迁移")
            if name == "exercise_search_projections" and row["representation_id"] != "native":
                require("exercise_search_representations", row["representation_id"])
            for asset_id in row.get("asset_ids", row.get("payload", {}).get("asset_ids", [])):
                require("exercise_search_assets", asset_id)
    return {"checked": checks, "valid": True}


def validate_assets(data):
    checked = 0
    for row in data["exercise_search_assets"]:
        path = Path(row["location"])
        if not path.is_file() or file_digest(path) != row["content_hash"]:
            raise ValueError("题图缺失或哈希变化：" + row["id"])
        checked += 1
    for row in data["textbook_documents"]:
        if row["asset_root"] and not Path(row["asset_root"]).is_dir():
            raise ValueError("教材资产目录缺失：" + row["id"])
    return {"checked": True, "question_assets": checked, "textbooks": len(data["textbook_documents"])}
