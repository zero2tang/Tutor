"""只供服务端维护的生成诊断寻址，禁止进入公开响应。"""

from functools import lru_cache
import gzip
import json
from pathlib import Path
from app.infrastructure.persistence.execution_models import AgentRun


@lru_cache(maxsize=1)
def _archive():
    root = Path(__file__).resolve().parents[4]
    return json.loads(gzip.decompress((root / "data/archives/simplification_p1/20261023/generation_attempts.json.gz").read_bytes()))


def generation_diagnostic(db, audit_id):
    run = db.get(AgentRun, audit_id)
    if run and (value := (run.private_diagnostics or {}).get("generation_attempt")):
        return value
    return _archive().get(audit_id)
