"""导出、预检、单事务导入与只读验证；不允许向部分已有数据的目标补写。"""

from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
from io import StringIO
import re

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, MetaData, select, text
from sqlalchemy.engine import URL

from app.application.model_registry import register_models
from app.infrastructure.persistence.schema import assert_schema_current
from .codec import digest, file_digest, normalize_row, serialized
from .resources import RESOURCE_TABLES, RESET_TABLES, select_resources


def receipt_path(snapshot, action):
    return Path(snapshot).with_suffix("." + action + ".json")


def export_mysql(destination):
    """完整旧库只读快照独立保全；不会覆盖先前快照或回执。"""
    destination = Path(destination)
    report_path = receipt_path(destination, "export")
    if destination.exists() or report_path.exists():
        raise FileExistsError("备份路径已存在，请使用新的时间戳文件名")
    shared = Path(os.getenv("TUTOR_SHARED_ENV", r"E:\projects\contract\contract-compliance\backend\.env"))
    assignments = "\n".join(line for line in shared.read_text(encoding="utf-8-sig").splitlines()
                            if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=", line)) if shared.exists() else ""
    values = {**dotenv_values(stream=StringIO(assignments)), **os.environ}
    source = create_engine(URL.create("mysql+pymysql", username=values.get("MYSQL_USER", "root"),
        password=values.get("MYSQL_PASSWORD", ""), host=values.get("MYSQL_HOST", "127.0.0.1"),
        port=int(values.get("MYSQL_PORT", "3306")), database="stem_tutor", query={"charset": "utf8mb4"}))
    metadata = MetaData()
    tables = {}
    try:
        metadata.reflect(source)
        with source.connect().execution_options(isolation_level="REPEATABLE READ") as connection, connection.begin():
            checks = [
                "SELECT COUNT(*) FROM learning_sessions WHERE active_turn_id IS NOT NULL",
                "SELECT COUNT(*) FROM memory_jobs WHERE status IN ('pending','retry','running')",
                "SELECT COUNT(*) FROM index_tasks WHERE status IN ('pending','building','running') OR lease_until IS NOT NULL",
                "SELECT COUNT(*) FROM textbook_extraction_sections WHERE extraction_status IN ('pending','running') OR lease_until IS NOT NULL",
            ]
            if any(connection.scalar(text(statement)) for statement in checks):
                raise RuntimeError("仍有教学、记忆或资源构建任务，禁止快照切换")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(destination, "xt", encoding="utf-8") as stream:
                for table in metadata.sorted_tables:
                    rows = [dict(row) for row in connection.execute(select(table)).mappings()]
                    stream.write(serialized({"table": table.name, "rows": rows}) + "\n")
                    tables[table.name] = {"rows": len(rows), "sha256": digest(rows)}
    finally:
        source.dispose()
    report = {"source": "mysql:stem_tutor", "created_at": datetime.now(timezone.utc).isoformat(),
              "snapshot_sha256": file_digest(destination), "tables": tables}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def load_snapshot(snapshot):
    snapshot = Path(snapshot)
    report = json.loads(receipt_path(snapshot, "export").read_text(encoding="utf-8"))
    checksum = file_digest(snapshot)
    if report.get("snapshot_sha256", checksum) != checksum:
        raise ValueError("快照文件哈希不一致")
    blocks = {}
    with gzip.open(snapshot, "rt", encoding="utf-8") as stream:
        for line in stream:
            block = json.loads(line)
            if set(block) != {"table", "rows"} or block["table"] in blocks or not isinstance(block["rows"], list):
                raise ValueError("快照块格式错误或表重复")
            blocks[block["table"]] = block["rows"]
    if set(blocks) != set(report["tables"]):
        raise ValueError("快照表与导出清单不一致")
    for name, rows in blocks.items():
        expected = report["tables"][name]
        if len(rows) != expected["rows"] or digest(rows) != expected["sha256"]:
            raise ValueError("快照原始内容哈希不一致：" + name)
    return blocks, checksum


def preflight(snapshot, *, metadata=None, check_assets=True):
    metadata = metadata if metadata is not None else register_models()
    blocks, checksum = load_snapshot(snapshot)
    selected, report = select_resources(blocks, metadata, check_assets=check_assets)
    report.update(snapshot_sha256=checksum, source_rows=sum(map(len, blocks.values())))
    return selected, report


def _validate_target(connection, metadata):
    if connection.dialect.name != "postgresql":
        raise ValueError("目标必须是 PostgreSQL")
    assert_schema_current(connection)
    actual = set(inspect(connection).get_table_names()) - {"alembic_version"}
    if actual != set(metadata.tables):
        raise RuntimeError("目标业务表清单与当前模型不一致，停止迁移")


def _verify(connection, selected, metadata):
    tables = {}
    for name in sorted(RESOURCE_TABLES):
        table = metadata.tables[name]
        existing = [normalize_row(table, dict(row)) for row in connection.execute(select(table)).mappings()]
        expected, actual = digest(selected[name]), digest(existing)
        if expected != actual:
            raise RuntimeError("资源数据校验失败：" + name)
        tables[name] = {"rows": len(existing), "sha256": actual, "verified": True}
    for name in sorted(set(metadata.tables) - RESOURCE_TABLES):
        if connection.execute(select(metadata.tables[name]).limit(1)).first() is not None:
            raise RuntimeError("应为空的学生或运行表已有数据：" + name)
    return tables


def import_snapshot(snapshot, target, *, metadata=None, check_assets=True):
    metadata = metadata if metadata is not None else register_models()
    selected, report = preflight(snapshot, metadata=metadata, check_assets=check_assets)
    # 所有文件、筛选和资产预检都在目标连接及写事务之前完成。
    with target.begin() as connection:
        _validate_target(connection, metadata)
        names = sorted(metadata.tables)
        connection.execute(text('LOCK TABLE ' + ','.join('"' + name + '"' for name in names) + ' IN ACCESS EXCLUSIVE MODE'))
        for name in names:
            if connection.execute(select(metadata.tables[name]).limit(1)).first() is not None:
                raise RuntimeError("目标业务数据非空，只允许 verify，禁止自动补写：" + name)
        for table in metadata.sorted_tables:
            rows = selected.get(table.name, [])
            for offset in range(0, len(rows), 250):
                connection.execute(table.insert(), rows[offset:offset + 250])
        report["verified_tables"] = _verify(connection, selected, metadata)
    return {**report, "target": target.url.database, "status": "verified", "transaction": "single"}


def verify_snapshot(snapshot, target, *, metadata=None, check_assets=True):
    metadata = metadata if metadata is not None else register_models()
    selected, report = preflight(snapshot, metadata=metadata, check_assets=check_assets)
    with target.connect().execution_options(isolation_level="REPEATABLE READ") as connection, connection.begin():
        connection.execute(text("SET TRANSACTION READ ONLY"))
        _validate_target(connection, metadata)
        report["verified_tables"] = _verify(connection, selected, metadata)
    return {**report, "target": target.url.database, "status": "verified", "read_only": True}
