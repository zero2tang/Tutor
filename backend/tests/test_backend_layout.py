"""布局与静态引用回归；不导入业务代码，不访问真实数据库。"""

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "scripts/maintenance/audit_backend_layout.py"
if not AUDIT_PATH.exists():
    AUDIT_PATH = Path(__file__).with_name("audit_backend_layout.py")
spec = importlib.util.spec_from_file_location("backend_layout_audit", AUDIT_PATH)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def write(root, relative, text=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rows(report):
    return {item["module"]: item for item in report["modules"]}


def test_application_layout_and_import_paths():
    report = audit_module.audit(ROOT)
    assert report["layout_violations"] == []
    assert report["missing_entries"] == report["missing_app_imports"] == []
    assert report["parse_errors"] == []
    assert [
        item
        for item in report["unresolved_dynamic_imports"]
        if item["path"].startswith("backend/app/")
    ] == []
    app = ROOT / "backend/app"
    assert {
        path.name
        for path in app.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    } == set(audit_module.ROOT_ROLES)
    assert {
        path.name
        for path in (app / "domains").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    } == audit_module.DOMAINS
    registry = "app.application.model_registry"
    assert registry in rows(report)
    registry_tree = ast.parse(
        (app / "application/model_registry.py").read_text(encoding="utf-8-sig")
    )
    registry_refs, _, _ = audit_module.module_references(
        registry_tree, registry, False, rows(report)
    )
    model_modules = {
        item["module"]
        for item in report["modules"]
        if not item["is_package"]
        and (
            item["module"].endswith(
                (".models", ".execution_models", ".checkpoint_models")
            )
            or ".models." in item["module"]
        )
    }
    assert model_modules <= registry_refs
    assert all(
        module.startswith("app.domains.")
        or module.startswith(
            (
                "app.infrastructure.persistence.execution_models",
                "app.infrastructure.persistence.checkpoint_models",
            )
        )
        for module in model_modules
    )


def test_relative_imports_and_package_initialization_are_followed_without_execution(
    tmp_path,
):
    write(tmp_path, "backend/app/__init__.py")
    write(tmp_path, "backend/app/main.py", "from app.domains.courses import service\n")
    write(tmp_path, "backend/app/domains/courses/__init__.py", "from . import policy\n")
    write(
        tmp_path,
        "backend/app/domains/courses/service.py",
        "from .policy import decide\nraise RuntimeError('被审计模块不能执行')\n",
    )
    write(tmp_path, "backend/app/domains/courses/policy.py", "def decide(): pass\n")
    report = audit_module.audit(tmp_path)
    assert report["missing_app_imports"] == report["parse_errors"] == []
    assert all(item["reachable_from_entry"] for item in report["modules"])
    assert rows(report)["app.domains.courses.policy"]["import_chain"][0] == "app.main"
    assert sum(item["modules"] for item in report["roots"]) == report["app_modules"]


def test_cli_script_test_only_and_unreferenced_modules_are_distinct(tmp_path):
    write(tmp_path, "backend/app/main.py")
    write(
        tmp_path,
        "backend/app/application/commands/courses/import_course.py",
        "from . import prepare\nif __name__ == '__main__':\n    raise RuntimeError('不能执行命令')\n",
    )
    write(tmp_path, "backend/app/application/commands/courses/prepare.py")
    write(tmp_path, "backend/app/domains/courses/seed.py", "from . import seed_rules\n")
    write(tmp_path, "backend/app/domains/courses/seed_rules.py")
    write(tmp_path, "backend/app/domains/courses/test_helper.py")
    write(tmp_path, "backend/app/domains/courses/unused.py")
    write(
        tmp_path,
        "scripts/import_course.py",
        "from app.domains.courses.seed import run\n",
    )
    write(
        tmp_path,
        "backend/tests/test_example.py",
        "patch('app.domains.courses.test_helper.run')\n",
    )
    report = audit_module.audit(tmp_path)
    items = rows(report)
    assert report["command_roots"] == ["app.application.commands.courses.import_course"]
    assert (
        items["app.application.commands.courses.import_course"]["usage"]
        == "command_root"
    )
    assert items["app.application.commands.courses.prepare"]["usage"] == "command_only"
    assert items["app.domains.courses.seed_rules"]["usage"] == "script_only"
    assert items["app.domains.courses.test_helper"]["usage"] == "test_only"
    assert report["orphan_candidates"] == ["app.domains.courses.unused"]


def test_literal_dynamic_imports_and_unresolved_locations_are_reported(tmp_path):
    write(
        tmp_path,
        "backend/app/main.py",
        'import importlib\nimportlib.import_module("app.domains.courses.service")\nimportlib.import_module(".policy", "app.domains.courses")\nimportlib.import_module(selected_module)\n__import__("app.agent.missing")\n',
    )
    write(tmp_path, "backend/app/domains/courses/service.py")
    write(tmp_path, "backend/app/domains/courses/policy.py")
    report = audit_module.audit(tmp_path)
    assert rows(report)["app.domains.courses.service"]["reachable_from_entry"]
    assert rows(report)["app.domains.courses.policy"]["reachable_from_entry"]
    assert report["unresolved_dynamic_imports"] == [
        {"path": "backend/app/main.py", "line": 4}
    ]
    assert report["missing_app_imports"] == [
        {"path": "backend/app/main.py", "module": "app.agent.missing"}
    ]


def test_import_and_from_missing_modules_are_reported_and_symbols_are_not(tmp_path):
    write(
        tmp_path,
        "backend/app/main.py",
        "import app.domains.missing\nfrom app.domains.absent import Example\nfrom app.domains.courses.policy import decide\n",
    )
    write(tmp_path, "backend/app/domains/courses/policy.py", "decide = None\n")
    report = audit_module.audit(tmp_path)
    assert {item["module"] for item in report["missing_app_imports"]} == {
        "app.domains.missing",
        "app.domains.absent",
    }
    assert not report["unresolved_dynamic_imports"]


def test_disconnected_import_cycle_remains_a_candidate_for_review(tmp_path):
    write(tmp_path, "backend/app/main.py")
    write(tmp_path, "backend/app/domains/courses/first.py", "from . import second\n")
    write(tmp_path, "backend/app/domains/courses/second.py", "from . import first\n")
    report = audit_module.audit(tmp_path)
    assert report["orphan_candidates"] == []
    assert report["unreachable_candidates"] == [
        "app.domains.courses.first",
        "app.domains.courses.second",
    ]
    assert "不能据此自动删除" in report["limitation"]
