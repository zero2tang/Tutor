"""检查后端源码依赖方向和全部模块环；包含函数内与条件导入。"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {
    "shared": set(),
    "infrastructure": {"shared"},
    "agent": {"shared"},
    "domains": {"agent", "infrastructure", "shared"},
    "application": {"domains", "agent", "infrastructure", "shared"},
    "api": {"application", "domains", "agent", "infrastructure", "shared"},
}


def component(module):
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ""


def cycles(graph):
    """Tarjan 强连通分量；两个以上模块互相可达才构成这里报告的环。"""
    indexes, lowlinks, pending, active, result = {}, {}, [], set(), []

    def visit(node):
        indexes[node] = lowlinks[node] = len(indexes)
        pending.append(node)
        active.add(node)
        for other in sorted(graph[node]):
            if other not in indexes:
                visit(other)
                lowlinks[node] = min(lowlinks[node], lowlinks[other])
            elif other in active:
                lowlinks[node] = min(lowlinks[node], indexes[other])
        if lowlinks[node] == indexes[node]:
            group = []
            while True:
                other = pending.pop()
                active.remove(other)
                group.append(other)
                if other == node:
                    break
            if len(group) > 1:
                result.append(sorted(group))

    for node in sorted(graph):
        if node not in indexes:
            visit(node)
    return sorted(result)


def analyze(layout):
    modules = {item["module"]: item for item in layout["modules"]}
    graph = {name: set() for name in modules}
    for target, record in modules.items():
        for source in record["app_importers"]:
            graph[source].add(target)
    violations, counts = [], {}
    for source, targets in sorted(graph.items()):
        owner = component(source)
        if owner not in ALLOWED:
            continue  # main/bootstrap 是明确的组合根。
        for target in sorted(targets):
            dependency = component(target)
            if not dependency or owner == dependency:
                continue
            key = f"{owner} -> {dependency}"
            counts[key] = counts.get(key, 0) + 1
            if dependency not in ALLOWED[owner]:
                violations.append(
                    {
                        "source": source,
                        "target": target,
                        "path": modules[source]["path"],
                    }
                )
    return {
        "module_count": len(modules),
        "edge_count": sum(len(edges) for edges in graph.values()),
        "allowed_dependencies": {
            key: sorted(values) for key, values in ALLOWED.items()
        },
        "cross_component_edges": counts,
        "violations": violations,
        "module_cycles": cycles(graph),
        "missing_imports": layout["missing_app_imports"],
        "parse_errors": layout["parse_errors"],
        "missing_entries": layout["missing_entries"],
        "layout_violations": layout["layout_violations"],
        "unresolved_application_imports": [
            item
            for item in layout["unresolved_dynamic_imports"]
            if item["path"].startswith("backend/app/")
        ],
        "limitation": "静态导入方向不等于运行时调用方向。Agent工具执行后回到调用方模型属于已授权能力回调；同层业务域仍有有界查询与ORM协作。",
    }


def audit(root=ROOT):
    path = ROOT / "scripts/maintenance/audit_backend_layout.py"
    spec = importlib.util.spec_from_file_location("dependency_layout", path)
    layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout)
    return analyze(layout.audit(root))


def has_errors(result):
    """应用内未知动态导入无法证明方向，必须显式处理后再通过。"""
    return any(
        result[key]
        for key in (
            "violations",
            "module_cycles",
            "missing_imports",
            "parse_errors",
            "missing_entries",
            "layout_violations",
            "unresolved_application_imports",
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = audit()
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return has_errors(result)


if __name__ == "__main__":
    raise SystemExit(main())
