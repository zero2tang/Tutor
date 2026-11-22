"""真实MySQL类型精度探针：只迁移新表白名单，测试行全部回滚，不算学习链路验收。"""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main(args):
    os.environ["TUTOR_DATABASE"] = "stem_tutor_business_memory_acceptance"
    from sqlalchemy import event, inspect, select, text
    from sqlalchemy.orm import Session
    from app.infrastructure.config import get_settings
    from app.infrastructure.persistence.database import engine
    from app.domains.memory.scopes.dependencies import allowed_sources
    from app.domains.memory.migrations import TIME_COLUMNS, ensure_business_memory_time_precision
    from app.domains.memory.scopes.models import ScopeMemory
    from app.domains.memory.models import MemoryFact

    assert get_settings().database_name == "stem_tutor_business_memory_acceptance"
    db_engine = engine()
    assert db_engine.dialect.name == "mysql"
    report = {"at": datetime.now(timezone.utc).isoformat(), "database": get_settings().database_name,
        "data_origin": "explicit_database_type_probe_not_learning_flow", "model_calls": 0,
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "complete": False}
    ddl_statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.upper().startswith("ALTER TABLE"):
            ddl_statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        report["first_migration"] = ensure_business_memory_time_precision(db_engine)
        first_count = len(ddl_statements)
        report["second_migration"] = ensure_business_memory_time_precision(db_engine)
        report["second_run_ddl_count"] = len(ddl_statements) - first_count
        assert report["second_run_ddl_count"] == 0
        inspector = inspect(db_engine)
        report["column_precision"] = [{"table": table, "column": name,
            "fsp": getattr(next(c for c in inspector.get_columns(table) if c["name"] == name)["type"], "fsp", None)}
            for table, names in TIME_COLUMNS.items() for name in names]
        assert len(report["column_precision"]) == 10 and all(c["fsp"] == 6 for c in report["column_precision"])
        with db_engine.connect() as connection:
            connection.execute(text("CREATE TEMPORARY TABLE `business_precision_type_probe` (`coarse` DATETIME(0), `precise` DATETIME(6))"))
            connection.execute(text("INSERT INTO `business_precision_type_probe` (`coarse`,`precise`) VALUES (:value,:value)"),
                               {"value": datetime(2026, 10, 21, 12, 0, 0, 800000)})
            coarse, precise = connection.execute(text("SELECT `coarse`,`precise` FROM `business_precision_type_probe`")).one()
            connection.execute(text("DROP TEMPORARY TABLE `business_precision_type_probe`"))
            connection.commit()
        report["native_mysql_rounding"] = {"input": "2026-10-21T12:00:00.800000",
            "datetime0": coarse.isoformat(), "datetime6": precise.isoformat()}
        assert coarse == datetime(2026, 10, 21, 12, 0, 1)
        assert precise == datetime(2026, 10, 21, 12, 0, 0, 800000)

        identifier = "precision_probe_" + uuid4().hex
        cutoff = datetime(2026, 10, 21, 12, 0, 0, 800000)
        before, after = cutoff - timedelta(microseconds=50000), cutoff + timedelta(microseconds=50000)
        with Session(db_engine) as db:
            db.add(ScopeMemory(scope_key=identifier, student_id="synthetic_precision", scope_kind="student", course_id="", concept_id="", cutoff_at=cutoff, updated_at=cutoff,
                generation=1, invalidation_version=1, enabled=True, blocked_source_refs=[], blocked_claim_ids=[]))
            for index, stamp in enumerate((before, after)):
                db.add(MemoryFact(id=identifier[-30:] + str(index), session_id=identifier[-36:], seq=index + 1,
                    source_ref=identifier + ":" + str(index), kind="type_probe",
                    payload={"_source_created_at": stamp.isoformat(), "synthetic_type_probe_only": True}))
            db.flush()
            db.expunge_all()
            state = db.get(ScopeMemory, identifier)
            assert state.cutoff_at == cutoff and state.updated_at == cutoff
            refs = [identifier + ":0", identifier + ":1"]
            admitted = allowed_sources(db, state, refs)
            assert admitted == {refs[1]}
            report["revocation_boundary"] = {"stored_cutoff": state.cutoff_at.isoformat(),
                "before_source": before.isoformat(), "after_source": after.isoformat(),
                "only_after_source_admitted": True}
            # recording恢复使用updated_at划定暂停区间，与同一类型精度权威比较。
            report["pause_boundary"] = {"stored_paused_at": state.updated_at.isoformat(),
                "before_not_inside_pause": before.isoformat() < state.updated_at.isoformat(),
                "after_inside_pause": after.isoformat() >= state.updated_at.isoformat()}
            assert report["pause_boundary"]["before_not_inside_pause"] and report["pause_boundary"]["after_inside_pause"]
            db.rollback()
        with Session(db_engine) as db:
            assert db.get(ScopeMemory, identifier) is None
            assert db.scalar(select(MemoryFact.id).where(MemoryFact.source_ref.in_(refs))) is None
        report.update(rolled_back_probe_rows_absent=True, schema_ddl_statements=ddl_statements,
                      original_learning_records_modified=0, complete=True)
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "data/reports/business_memory_precision_probe.json")
    main(parser.parse_args())
