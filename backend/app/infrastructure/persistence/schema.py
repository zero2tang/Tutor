"""Alembic 版本检查与已有空数据库的严格接管；在线请求不执行 DDL。"""

import json
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.infrastructure.config import ROOT


def alembic_config() -> Config:
    return Config(str(ROOT / "backend" / "alembic.ini"))


def assert_schema_current(connection) -> None:
    """缺少或落后版本都明确失败，由维护命令完成升级。"""
    expected = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    actual = set(MigrationContext.configure(connection).get_current_heads())
    if actual != expected:
        raise RuntimeError("业务数据库结构未升级，请先执行 backend/.venv/Scripts/python -m alembic -c backend/alembic.ini upgrade head")


def schema_signature(connection, names) -> dict:
    """只比较关系结构，不记录数据、连接口令或框架独立 schema。"""
    inspector = inspect(connection)
    return {
        name: {
            "columns": [{"name": column["name"],
                         "type": str(column["type"].compile(dialect=connection.dialect)),
                         "nullable": column["nullable"], "default": column["default"]}
                        for column in inspector.get_columns(name)],
            "primary_key": inspector.get_pk_constraint(name)["constrained_columns"],
            "unique": sorted([{"name": item["name"], "columns": item["column_names"]}
                              for item in inspector.get_unique_constraints(name)], key=lambda x: x["name"]),
            "indexes": sorted([{"name": item["name"], "columns": item["column_names"], "unique": item["unique"]}
                               for item in inspector.get_indexes(name) if not item.get("duplicates_constraint")],
                              key=lambda x: x["name"]),
            "foreign_keys": inspector.get_foreign_keys(name),
        }
        for name in sorted(names)
    }


def adopt_empty_baseline(connection, manifest: Path) -> bool:
    """只接管完整匹配且全空的旧建表结果；不允许凭版本号跳过未知结构。"""
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    names = set(inspect(connection).get_table_names()) - {"alembic_version"}
    if not names:
        return False
    if names != set(expected) or schema_signature(connection, names) != expected:
        raise RuntimeError("已有数据库与冻结基线结构不一致，拒绝接管")
    for name in sorted(names):
        if connection.scalar(text('SELECT EXISTS (SELECT 1 FROM "' + name + '" LIMIT 1)')):
            raise RuntimeError("已有业务数据，拒绝接管空库基线：" + name)
    return True
