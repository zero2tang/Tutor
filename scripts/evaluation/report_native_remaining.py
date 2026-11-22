"""对经 N1 清单核验的源码归档统计变更；净行数与结构退出证据分别报告。"""

import ast
from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "data/archives/native_remaining/20261023/n1_source.zip"
OUTPUT = ROOT / "data/reports/native_remaining/source_cost.json"


def diff_totals(before, after):
    counts, changed = Counter(), []
    for path in sorted(before.keys() | after.keys()):
        a, b = before.get(path, "").splitlines(), after.get(path, "").splitlines()
        if a == b:
            continue
        added = deleted = 0
        for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            added += l-k if tag in ("insert", "replace") else 0
            deleted += j-i if tag in ("delete", "replace") else 0
        counts.update(added=added, deleted=deleted)
        changed.append({"path": path, "added": added, "deleted": deleted, "net": added-deleted})
    return {**counts, "net": counts["added"]-counts["deleted"], "changed_files": len(changed), "files": changed}


def ancillary_cost():
    archive = ARCHIVE.with_name("ancillary_reference.zip")
    prefixes = ("backend/tests/", "scripts/", "docs/", "backend/app/README.md", "README.md")
    if not archive.exists():
        snapshot = ROOT / ".tmp/native_remaining_before"
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        values, origins = {}, {}
        with ZipFile(ROOT / "data/archives/native_n0_n1/20261023/source_before.zip") as prior:
            for name in prior.namelist():
                if name.startswith(prefixes) and Path(name).suffix in {".py", ".md", ".json", ".mjs"}:
                    values[name], origins[name] = prior.read(name), "N0 archive supplement"
        for name, entry in manifest.items():
            if name.startswith(prefixes) and Path(name).suffix in {".py", ".md", ".json", ".mjs"}:
                content = (snapshot / name).read_bytes()
                assert hashlib.sha256(content).hexdigest() == entry["sha256"], name
                values[name], origins[name] = content, "execution-start snapshot"
        with ZipFile(archive, "w") as target:
            for name, content in values.items():
                target.writestr(name, content)
            target.writestr("baseline_origins.json", json.dumps(origins))
    with ZipFile(archive) as source:
        before = {name: source.read(name).decode("utf-8-sig") for name in source.namelist() if name != "baseline_origins.json"}
        origins = json.loads(source.read("baseline_origins.json"))
    known_new = {"scripts/evaluation/" + name for name in ("request_protocol.py", "check_native_remaining.py",
        "check_native_compaction.py", "report_native_remaining.py", "check_native_architecture.py")}
    known_new.add("docs/stem_v1/30_native_remaining_implementation_and_acceptance.md")
    known_new.update("backend/tests/" + name for name in (
        "native_runtime_helpers.py", "native_scripted_model.py", "native_tools.py", "test_native_agent_runtime.py",
        "test_native_assessment_revocation.py", "test_native_child_restart.py", "test_native_guidance_context.py",
        "test_native_http_conflict.py", "test_native_memory_audit.py", "test_native_memory_models.py",
        "test_native_practice_graph.py", "test_native_recovery_guards.py", "test_native_request_protocol.py",
        "test_native_session_edge_cases.py", "test_native_session_graph.py", "test_native_storage.py"))
    groups = {}
    for group, prefix, suffix in (("tests", "backend/tests/", {".py"}),
                                  ("validation_scripts", "scripts/", {".py", ".mjs"}),
                                  ("documentation", "docs/", {".md", ".json"})):
        prior = {key: value for key, value in before.items() if key.startswith(prefix) and Path(key).suffix in suffix}
        current = {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8-sig")
                   for path in (ROOT / prefix).rglob("*") if path.is_file() and path.suffix in suffix}
        covered = {key: value for key, value in current.items() if key in before or key in known_new}
        groups[group] = {**diff_totals(prior, covered),
            "excluded_without_baseline": sorted(set(current) - set(covered)),
            "baseline_origins": {key: value for key, value in origins.items() if key in prior}}
    # 只计算唯一逻辑脚本，归档的演练/应用副本不重复计作新增生产能力。
    migration = ARCHIVE.with_name("migration_source_applied.py")
    groups["one_time_migration"] = {"lines": len(migration.read_text(encoding="utf-8").splitlines()),
        "active_entry_removed": not (ROOT / ".tmp/migrate_native_remaining.py").exists(),
        "scope": "archived conversion script; targeted guidance normalization executed as audited commands with independent before backups"}
    return {"baseline_kind": "execution-start snapshot plus explicitly marked N0 archival supplements; unrecorded existing files excluded; exact N1 SHA ledger applies only to production/frontend",
            "groups": groups}


def symbols(text):
    result = {}

    def visit(node, scope="module"):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope += "." + node.name
            for line in range(node.lineno, node.end_lineno + 1):
                result[line] = scope
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(text or ""))
    return result


def category(path, symbol, line):
    """混合文件逐函数/行归属；分类是职责核算，不是工作时长或性能度量。"""
    capacity = "new_durability"
    replacement = "replacement"
    if path.endswith(("learning/receipts.py", "learning/storage.py", "learning/protocol.py",
                      "sessions/interactions.py", "practice/operations.py", "shared/identity.py")):
        return capacity
    if path.endswith("learning/entry.py"):
        return replacement if any(marker in line for marker in ("claim(", "release(", "sessions.get(", "catalog.", "scope =", "failed =")) else capacity
    if path.endswith("learning/graph.py"):
        return replacement if any(marker in symbol for marker in ("supervisor", "guidance", "apply_action", ".route", ".stop", ".public_summary")) else capacity
    if path.endswith("learning/actions.py"):
        return capacity if any(marker in line for marker in ("complete(", "request_id", "operation_id", "owner(", "receipt")) else replacement
    if path.endswith("learning/publication.py"):
        return capacity if any(marker in line for marker in ("complete(", "request_id", "operation_id", "owner(", "receipt", "epoch", "identifier", "prior", "replayed", "db.get(", "if message is None")) else replacement
    if path.endswith("practice/service.py"):
        return capacity if any(marker in symbol for marker in ("existing_generation", "restore_generation_budget")) or any(marker in line for marker in ("operation", "stable_id", "input_hash", "session_version", "require_owner", "receipt", "replayed", "generation_graph is None")) else replacement
    if path.endswith("practice/batches.py"):
        return capacity if ".existing" in symbol or any(marker in line for marker in ("operation", "stable_id", "input_hash", "session_version", "require_owner", "receipt", "prior", "replayed", "first_message_id")) else replacement
    if path.endswith("memory/context.py") or path.endswith("shared/execution.py") or path.endswith("main.py"):
        return capacity
    if path.endswith("bootstrap.py"):
        return capacity if any(marker in line for marker in ("AsyncExitStack", "resources", "checkpointer", "saver", "storage", "cold_threads")) else replacement
    if path.endswith("learning/delegation.py"):
        return capacity if any(marker in symbol for marker in ("._reserve",)) or any(marker in line for marker in ("operation_id", "memory_epoch", "invalidation_epoch", "receipt", "owner(", "identifier", "db.add(", "db.get(Message", "budget", "reserved")) else replacement
    if path.endswith("application/guidance/service.py"):
        return capacity if any(marker in line for marker in ("epoch", "receipt", "operation_id", "_input_hash", "_native_request", "request_id", "replayed")) else replacement
    if "/api/" in path or path.endswith(("assessment/schemas.py", "guidance/schemas.py", "learning/schemas.py", "practice/schemas.py")):
        return capacity if any(marker in line for marker in ("wait_id", "request_id", "idempotency_key", "PracticeRequest", "SkipRequest", "LearningConflict", "status_code")) else replacement
    if "/assessment/" in path:
        return capacity if any(marker in line for marker in ("epoch", "request_id", "idempotency_key", "active_turn_id", "not turn_id")) else replacement
    if path.endswith("agent/tools/contracts.py"):
        return capacity if "memory_epoch" in line else replacement
    # 原生模型/真实工具/摘要/事件、旧底座删除、质量图替换和后台模型迁移。
    return replacement


def main():
    with ZipFile(ARCHIVE) as archive:
        expected = json.loads((ROOT / "data/reports/native_n0_n1/baseline_verification.json").read_text(encoding="utf-8"))["source_after"]
        for name, value in expected.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == value["sha256"], name
        before = {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()
                  if name.startswith("backend/app/") and name.endswith(".py")}
        old_frontend = {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist() if name.startswith("frontend/src/")}
    after = {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8-sig")
             for path in (ROOT / "backend/app").rglob("*.py")}
    entries, totals = [], Counter()
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, ""), after.get(path, "")
        a, b = old.splitlines(), new.splitlines()
        if a == b:
            continue
        old_symbols, new_symbols = symbols(old), symbols(new)
        counts = Counter()
        # 排除纯缩进变化和参数改名，不将排版作为删除收益。
        normalize = lambda lines: [line.strip().replace("tool_registry", "tool_source") for line in lines]
        matcher = difflib.SequenceMatcher(None, normalize(a), normalize(b), autojunk=False)
        for tag, i, j, k, l in matcher.get_opcodes():
            if tag in ("delete", "replace"):
                for index in range(i, j):
                    counts[(category(path, old_symbols.get(index + 1, "module"), a[index]), "deleted")] += 1
            if tag in ("insert", "replace"):
                for index in range(k, l):
                    counts[(category(path, new_symbols.get(index + 1, "module"), b[index]), "added")] += 1
        totals.update(counts)
        entries.append({"path": path, "status": "added" if path not in before else "deleted" if path not in after else "modified",
            "physical_before": len(a), "physical_after": len(b), "physical_net": len(b)-len(a),
            "cost": {group: {kind: counts[(group, kind)] for kind in ("added", "deleted")}
                     for group in ("replacement", "new_durability")},
            "sha256_before": hashlib.sha256(old.encode()).hexdigest() if old else None,
            "sha256_after": hashlib.sha256(new.encode()).hexdigest() if new else None})
    old_n, new_n = sum(len(v.splitlines()) for v in before.values()), sum(len(v.splitlines()) for v in after.values())
    core = lambda value: sum(len(text.splitlines()) for path, text in value.items() if path.startswith((
        "backend/app/agent/runtime/", "backend/app/agent/context/", "backend/app/application/agent/")))
    cost = {group: {"added": totals[(group, "added")], "deleted": totals[(group, "deleted")],
        "net": totals[(group, "added")]-totals[(group, "deleted")]} for group in ("replacement", "new_durability")}
    result = {"baseline": {"files": len(before), "lines": old_n, "core_lines": core(before)},
        "current": {"files": len(after), "lines": new_n, "core_lines": core(after)}, "physical_net": new_n-old_n,
        "production_cost": cost, "deleted_files": [entry["path"] for entry in entries if entry["status"] == "deleted"],
        "accounting": ["N1 archive checked against source_after SHA256 manifest", "physical lines include blank lines",
            "gross diff ignores indentation-only changes and tool_source parameter rename",
            "mixed files attributed by function/line rules in this script; not entire files assigned to new capability",
            "pure relocation is replacement cost and is not claimed as a simplification gain",
            "tests, scripts, migration archives and docs excluded from production totals; reported separately"],
        "frontend": diff_totals(old_frontend, {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8-sig")
                     for path in (ROOT / "frontend/src").rglob("*") if path.is_file()}),
        "ancillary": ancillary_cost(), "files": entries}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in {"files", "frontend", "ancillary"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
