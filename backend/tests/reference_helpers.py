from types import SimpleNamespace
from datetime import datetime
from sqlalchemy import select
from app.domains.courses.models import Question
from app.domains.materials.teaching_reference import get_pack, make_reference
from app.infrastructure.persistence.execution_models import AgentRun
from app.domains.practice.diagnostics import generation_diagnostic


def save_pack(db, pack):
    q = db.get(Question, pack.question_id)
    q.stem = pack.payload.get("problem", {}).get("stem", q.stem)
    q.teaching_reference = make_reference(pack)


def audits(db):
    return sorted([SimpleNamespace(**r.private_diagnostics["generation_attempt"])
        for r in db.scalars(select(AgentRun)) if r.private_diagnostics and "generation_attempt" in r.private_diagnostics], key=lambda r:r.attempt_number)


def audit(db, identifier):
    return SimpleNamespace(**generation_diagnostic(db, identifier))


def put_scope(db, **values):
    from app.domains.memory.scopes.models import ScopeMemory
    row = db.get(ScopeMemory,values["scope_key"])
    if row is None:
        row = ScopeMemory(**values)
        db.add(row)
    else:
        for key,value in values.items():
            setattr(row,key,value)
    return row
