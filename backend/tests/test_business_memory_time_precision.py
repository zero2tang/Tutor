from app.domains.memory.models import MemoryJob
from app.domains.memory.scopes.models import SessionMemoryExtraction
from reference_helpers import put_scope
"""PostgreSQL微秒屏障类型，以及归档MySQL精度迁移的限定行为。"""

from contextlib import contextmanager

import pytest
from sqlalchemy import DateTime, create_engine
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.domains.memory.migrations import TIME_COLUMNS, ensure_business_memory_time_precision
from app.domains.memory.scopes.models import ScopeMemory
from app.domains.assessment.models import EvidenceConceptLink


def test_new_table_types_keep_postgres_microseconds_and_sqlite_compatibility():
    postgres_sql = str(CreateTable(ScopeMemory.__table__).compile(dialect=postgresql.dialect()))
    assert "cutoff_at TIMESTAMP WITHOUT TIME ZONE" in postgres_sql
    assert "updated_at TIMESTAMP WITHOUT TIME ZONE" in postgres_sql
    evidence_sql = str(CreateTable(EvidenceConceptLink.__table__).compile(dialect=postgresql.dialect()))
    assert "created_at TIMESTAMP WITHOUT TIME ZONE" in evidence_sql
    assert "updated_at TIMESTAMP WITHOUT TIME ZONE" in evidence_sql
    sqlite_sql = str(CreateTable(ScopeMemory.__table__).compile(dialect=sqlite.dialect()))
    assert "DATETIME(6)" not in sqlite_sql


def test_sqlite_migration_is_noop():
    engine = create_engine("sqlite://")
    assert ensure_business_memory_time_precision(engine) == {"changed_columns": []}
    engine.dispose()


class MigrationEngine:
    dialect = mysql.dialect()

    def __init__(self):
        self.statements = []

    @contextmanager
    def begin(self):
        yield self

    def execute(self, statement):
        self.statements.append(str(statement))


def test_only_whitelisted_time_columns_upgrade_and_second_run_has_no_ddl(monkeypatch):
    engine = MigrationEngine()

    class Inspector:
        precise = False

        def get_table_names(self):
            return ["scope_memories", "messages"]

        def get_columns(self, table):
            assert table == "scope_memories"
            return [{"name": name, "type": mysql.DATETIME(fsp=6 if self.precise else 0),
                     "nullable": name == "cutoff_at", "default": None}
                    for name in ("cutoff_at", "updated_at")]

    inspector = Inspector()
    monkeypatch.setattr("app.domains.memory.migrations.inspect", lambda _: inspector)
    first = ensure_business_memory_time_precision(engine)
    assert first["changed_columns"] == ["scope_memories.cutoff_at", "scope_memories.updated_at"]
    assert engine.statements == [
        "ALTER TABLE `scope_memories` MODIFY COLUMN `cutoff_at` DATETIME(6) NULL",
        "ALTER TABLE `scope_memories` MODIFY COLUMN `updated_at` DATETIME(6) NOT NULL"]
    inspector.precise = True
    assert ensure_business_memory_time_precision(engine) == {"changed_columns": []}
    assert len(engine.statements) == 2
    assert sum(map(len, TIME_COLUMNS.values())) == 10


def test_unexpected_database_default_is_not_silently_dropped(monkeypatch):
    engine = MigrationEngine()

    class Inspector:
        def get_table_names(self):
            return ["scope_memories"]

        def get_columns(self, _):
            return [{"name": "cutoff_at", "type": mysql.DATETIME(fsp=0),
                     "nullable": True, "default": "CURRENT_TIMESTAMP"}]

    monkeypatch.setattr("app.domains.memory.migrations.inspect", lambda _: Inspector())
    with pytest.raises(RuntimeError):
        ensure_business_memory_time_precision(engine)
    assert not engine.statements
