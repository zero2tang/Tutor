"""迁移边界测试：类型规范化、损坏文件、空库拒绝补写和真实 PG 事务回滚。"""

from datetime import datetime
import gzip
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import Boolean, Column, DateTime, JSON, LargeBinary, MetaData, Table, create_engine, select, text

from app.application.model_registry import register_models
from app.infrastructure.config import get_settings
from app.infrastructure.persistence.schema import alembic_config, assert_schema_current
from scripts.maintenance.postgres_migration.codec import digest, file_digest, normalize_row, serialized
from scripts.maintenance.postgres_migration.resources import RESOURCE_TABLES, RESET_TABLES
from scripts.maintenance.postgres_migration import transfer


def test_boolean_time_binary_and_nested_json_normalization():
    table = Table("sample", MetaData(), Column("flag", Boolean), Column("at", DateTime),
                  Column("blob", LargeBinary), Column("payload", JSON))
    source = {"flag": 1, "at": {"$datetime": "2026-10-26T08:00:00.123456"},
              "blob": {"$bytes": "YWJj"}, "payload": {"$datetime": "ordinary-json-value", "中文": [1, False]}}
    target = {"flag": True, "at": datetime(2026, 10, 26, 8, 0, 0, 123456),
              "blob": b"abc", "payload": source["payload"]}
    assert digest([normalize_row(table, source)]) == digest([normalize_row(table, target)])
    with pytest.raises(ValueError, match="Boolean"):
        normalize_row(table, {**source, "flag": 2})


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "snapshot.jsonl.gz"
    blocks = {name: [] for name in RESOURCE_TABLES | RESET_TABLES}
    blocks["courses"] = [{"id": "test-course", "title": "测试", "description": "资源", "source_ref": "fixture", "version": 1}]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for name, rows in sorted(blocks.items()):
            stream.write(serialized({"table": name, "rows": rows}) + "\n")
    receipt = {"snapshot_sha256": file_digest(path),
               "tables": {name: {"rows": len(rows), "sha256": digest(rows)} for name, rows in blocks.items()}}
    transfer.receipt_path(path, "export").write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_preflight_validates_complete_snapshot_and_selects_only_resources(snapshot):
    selected, report = transfer.preflight(snapshot)
    assert set(selected) == RESOURCE_TABLES
    assert report["resource_rows"] == 1
    assert set(report["reset_tables"]) == RESET_TABLES
    assert report["references"]["valid"]


def test_corrupt_snapshot_is_rejected_before_target_access(snapshot):
    with snapshot.open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="文件哈希"):
        transfer.preflight(snapshot)


@pytest.fixture
def postgres_target():
    if os.getenv("TUTOR_TEST_POSTGRES") != "1":
        pytest.skip("设置 TUTOR_TEST_POSTGRES=1 运行独立 PostgreSQL 数据库事务测试")
    settings = get_settings()
    name = "tutor_test_migration_" + uuid4().hex
    admin = create_engine(settings.database_url(database=False), isolation_level="AUTOCOMMIT")
    target = None
    try:
        with admin.connect() as connection:
            connection.execute(text('CREATE DATABASE "' + name + '"'))
        target = create_engine(settings.database_url().set(database=name))
        config = alembic_config()
        with target.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            assert_schema_current(connection)
        yield target
    finally:
        if target is not None:
            target.dispose()
        assert name.startswith("tutor_test_migration_") and name.isidentifier()
        with admin.connect() as connection:
            connection.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
        admin.dispose()


def test_import_is_single_transaction_and_repeat_is_verify_only(snapshot, postgres_target):
    result = transfer.import_snapshot(snapshot, postgres_target)
    assert result["status"] == "verified"
    assert result["resource_rows"] == 1
    with pytest.raises(RuntimeError, match="非空"):
        transfer.import_snapshot(snapshot, postgres_target)
    assert transfer.verify_snapshot(snapshot, postgres_target)["read_only"]


def test_post_insert_verification_failure_rolls_back_every_row(snapshot, postgres_target, monkeypatch):
    def fail(*args):
        raise RuntimeError("故障注入：最后校验失败")

    monkeypatch.setattr(transfer, "_verify", fail)
    with pytest.raises(RuntimeError, match="故障注入"):
        transfer.import_snapshot(snapshot, postgres_target)
    with postgres_target.connect() as connection:
        for table in register_models().tables.values():
            assert connection.execute(select(table).limit(1)).first() is None


def test_unknown_target_write_is_not_overwritten(snapshot, postgres_target):
    table = register_models().tables["courses"]
    with postgres_target.begin() as connection:
        connection.execute(table.insert(), {"id": "new-data", "title": "新写入", "description": "保留", "source_ref": "fixture", "version": 1})
    with pytest.raises(RuntimeError, match="非空"):
        transfer.import_snapshot(snapshot, postgres_target)
    with postgres_target.connect() as connection:
        assert connection.execute(select(table.c.id)).scalars().all() == ["new-data"]


def test_postgres_keyword_search_preserves_mysql_case_insensitivity(postgres_target, monkeypatch):
    """验证实际服务筛选，保留原名称/题干列的大小写与通配符语义。"""
    from app.infrastructure.persistence import database
    from app.domains.courses.models import Concept, Course, Question
    from app.domains.courses.catalog import CatalogService
    from app.domains.courses.difficulty.service import DifficultyQueryService
    from app.domains.courses.difficulty.metadata import fingerprint

    monkeypatch.setattr(database, "engine", lambda: postgres_target)
    with database.db_session() as db:
        db.add(Course(id="case-course", title="自编课程", description="筛选验收", source_ref="fixture://case"))
        db.add(Concept(id="case-x", course_id="case-course", source_id="x", name="Sine x"))
        db.add(Concept(id="case-other", course_id="case-course", source_id="other", name="Other"))
        question = Question(id="case-question", course_id="case-course", source_id="q",
            stem="sin(x) and Newton", question_type="fill", options={}, answer=["fixture"],
            concept_ids=["case-x"], source_ref="fixture://case")
        db.add(question)
        db.flush()
        question.difficulty_estimate = {"value": 20, "status": "estimated", "scale_max": 100,
            "method": "fixture", "version": "v1", "reference_group": "fixture",
            "source_hash": "fixture", "higher_is_harder": True, "question_fingerprint": fingerprint(question)}
    catalog = CatalogService()
    assert [row["id"] for row in catalog.concepts("case-course", "X")] == ["case-x"]
    assert len(catalog.concepts("case-course", "%")) == 2
    service = DifficultyQueryService()
    for query in ("SIN", "newton", "x", "%"):
        assert [row["id"] for row in service.query("case-course", query=query)["items"]] == ["case-question"]
