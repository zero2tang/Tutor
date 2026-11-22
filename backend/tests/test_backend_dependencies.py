"""依赖方向是可执行约束，局部导入也不能绕过检查。"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "backend_dependencies", ROOT / "scripts/maintenance/audit_backend_dependencies.py"
)
dependencies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dependencies)


def test_backend_dependencies_are_acyclic_and_follow_boundaries():
    report = dependencies.audit(ROOT)
    assert report["violations"] == []
    assert report["module_cycles"] == []
    assert report["missing_imports"] == report["parse_errors"] == []
    assert not dependencies.has_errors(report)


def test_local_and_dynamic_imports_cannot_reverse_boundaries(tmp_path):
    app = tmp_path / "backend/app"
    for path, text in {
        "main.py": "from app.agent import example\n",
        "agent/example.py": "def read():\n    from app.domains import example\n",
        "domains/example.py": "import importlib\nimportlib.import_module('app.application.example')\n",
        "application/example.py": "",
    }.items():
        target = app / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    report = dependencies.audit(tmp_path)
    assert {(row["source"], row["target"]) for row in report["violations"]} >= {
        ("app.agent.example", "app.domains.example"),
        ("app.domains.example", "app.application.example"),
    }


def test_cycle_detection_finds_nested_and_disconnected_cycles():
    graph = {
        "a": {"b"},
        "b": {"c"},
        "c": {"a", "d"},
        "d": set(),
        "e": {"f"},
        "f": {"e"},
    }
    assert dependencies.cycles(graph) == [["a", "b", "c"], ["e", "f"]]


def test_unknown_application_dynamic_imports_require_explicit_resolution(tmp_path):
    app = tmp_path / "backend/app"
    app.mkdir(parents=True)
    (app / "main.py").write_text(
        "import importlib\nimportlib.import_module(selected_module)\n", encoding="utf-8"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "inspection.py").write_text(
        "import importlib\nimportlib.import_module(inspection_target)\n",
        encoding="utf-8",
    )
    report = dependencies.audit(tmp_path)
    assert report["unresolved_application_imports"] == [
        {"path": "backend/app/main.py", "line": 2}
    ]
    assert dependencies.has_errors(report)
    (app / "main.py").write_text("", encoding="utf-8")
    assert not dependencies.has_errors(dependencies.audit(tmp_path))
