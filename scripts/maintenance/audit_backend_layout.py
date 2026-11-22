"""静态审计后端功能布局和导入来源；候选未使用不等于可以自动删除。"""

import argparse
import ast
from collections import deque
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOT_ROLES = {
    "api": "HTTP 路由、请求响应与传输适配",
    "application": "跨业务用例、Agent业务适配和启动编排",
    "domains": "学习、会话、练习、评估、课程、学生、材料、引导与记忆业务",
    "agent": "模型执行、上下文治理、工具协议和技能资源",
    "infrastructure": "配置、持久化和外部系统客户端",
    "shared": "不依赖具体业务的共用技术辅助",
}
DOMAINS = {
    "learning",
    "sessions",
    "practice",
    "assessment",
    "courses",
    "students",
    "materials",
    "guidance",
    "memory",
}
IGNORED = {"__pycache__", ".venv", "node_modules", ".pytest_cache"}


def python_paths(folder):
    return sorted(
        path for path in folder.rglob("*.py") if not IGNORED.intersection(path.parts)
    )


def module_name(path, backend):
    parts = list(path.relative_to(backend).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve_import(node, owner, is_package):
    if not node.level:
        return node.module or ""
    package = owner if is_package else owner.rpartition(".")[0]
    return importlib.util.resolve_name("." * node.level + (node.module or ""), package)


def parents(module):
    parts = module.split(".")
    return {".".join(parts[:index]) for index in range(1, len(parts))}


def module_references(tree, owner, is_package, known):
    """只解析源码，不执行业务导入；from 后的名称也可能是普通符号。"""
    imported, dynamic, missing = set(), [], set()
    available = set(known).union(*(parents(module) for module in known))
    for node in ast.walk(tree):
        targets, required = [], []
        if isinstance(node, ast.Import):
            targets = required = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            try:
                base = resolve_import(node, owner, is_package)
            except (ImportError, ValueError):
                missing.add("." * node.level + (node.module or ""))
                continue
            targets = [base] + [base + "." + alias.name for alias in node.names]
            required = [base]
        elif isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", "")
            )
            if name not in {"import_module", "__import__"}:
                continue
            if (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            ):
                dynamic.append(node.lineno)
                continue
            target = node.args[0].value
            if target.startswith("."):
                package = (
                    node.args[1]
                    if len(node.args) > 1
                    else next(
                        (item.value for item in node.keywords if item.arg == "package"),
                        None,
                    )
                )
                if not isinstance(package, ast.Constant) or not isinstance(
                    package.value, str
                ):
                    dynamic.append(node.lineno)
                    continue
                try:
                    target = importlib.util.resolve_name(target, package.value)
                except (ImportError, ValueError):
                    dynamic.append(node.lineno)
                    continue
            targets = required = [target]
        for target in required:
            if (
                target == "app" or target.startswith("app.")
            ) and target not in available:
                missing.add(target)
        for target in targets:
            imported.update(({target} | parents(target)).intersection(known))
    # 导入任意子模块也会执行其父包初始化；无 __init__ 的命名空间包不虚构文件。
    imported.update(parents(owner).intersection(known))
    return imported - {owner}, sorted(set(dynamic)), missing


def command_module(tree):
    """识别正反两种 __name__ == '__main__' 独立命令入口。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
            continue
        pair = [test.left, test.comparators[0]]
        if any(
            isinstance(value, ast.Name) and value.id == "__name__" for value in pair
        ) and any(
            isinstance(value, ast.Constant) and value.value == "__main__"
            for value in pair
        ):
            return True
    return False


def reachable(graph, roots):
    parent = {entry: None for entry in sorted(roots) if entry in graph}
    queue = deque(parent)
    while queue:
        current = queue.popleft()
        for target in sorted(graph[current]):
            if target not in parent:
                parent[target] = current
                queue.append(target)
    return parent


def import_chain(parent, module):
    chain = []
    if module in parent:
        while module is not None:
            chain.append(module)
            module = parent[module]
    return list(reversed(chain))


def layout_violations(app):
    violations = []
    for path in sorted(app.iterdir()) if app.exists() else []:
        if path.is_dir() and path.name not in ROOT_ROLES and path.name not in IGNORED:
            violations.append(path.name + "/: app 根目录未归入已登记职责")
        elif (
            path.is_file()
            and path.suffix == ".py"
            and path.name not in {"__init__.py", "main.py", "bootstrap.py"}
        ):
            violations.append(path.name + ": app 根目录只保留入口与装配")
    domain_root = app / "domains"
    for path in sorted(domain_root.iterdir()) if domain_root.exists() else []:
        if path.is_dir() and path.name not in DOMAINS and path.name not in IGNORED:
            violations.append("domains/" + path.name + "/: 未登记的业务域")
    return violations


def audit(root=ROOT, entries=("app.main",)):
    root = Path(root)
    backend, app = root / "backend", root / "backend/app"
    known = {module_name(path, backend): path for path in python_paths(app)}
    trees, graph, unresolved, missing, parse_errors = {}, {}, [], [], []

    def parse(path):
        try:
            return ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeError) as error:
            parse_errors.append(
                {"path": path.relative_to(root).as_posix(), "error": str(error)}
            )
            return ast.Module(body=[], type_ignores=[])

    def references(path, owner, is_package):
        tree = parse(path)
        refs, dynamic, absent = module_references(tree, owner, is_package, known)
        relative = path.relative_to(root).as_posix()
        unresolved.extend({"path": relative, "line": line} for line in dynamic)
        missing.extend(
            {"path": relative, "module": target} for target in sorted(absent)
        )
        return tree, refs

    for module, path in known.items():
        trees[module], graph[module] = references(
            path, module, path.name == "__init__.py"
        )
    external = {module: {"tests": set(), "scripts": set()} for module in known}
    external_roots = {"tests": set(), "scripts": set()}
    for label, folder in (("tests", backend / "tests"), ("scripts", root / "scripts")):
        for path in python_paths(folder):
            owner = ".".join(path.relative_to(root).with_suffix("").parts)
            tree, refs = references(path, owner, path.name == "__init__.py")
            # 完整 patch 字符串属于维护引用，不是线上业务入口。
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.startswith("app.")
                ):
                    target = node.value
                    while target and target not in known:
                        target = target.rpartition(".")[0]
                    if target:
                        refs.add(target)
            external_roots[label].update(refs)
            for target in refs:
                external[target][label].add(path.relative_to(root).as_posix())
    commands = {module for module, tree in trees.items() if command_module(tree)}
    online = reachable(graph, entries)
    command_paths = reachable(graph, commands)
    offline = reachable(graph, external_roots["scripts"])
    tests = reachable(graph, external_roots["tests"])
    incoming = {module: [] for module in known}
    for source, targets in graph.items():
        for target in targets:
            incoming[target].append(source)
    modules = []
    for module, path in sorted(known.items()):
        is_package = path.name == "__init__.py"
        if module in online:
            usage = "application"
        elif module in commands:
            usage = "command_root"
        elif module in command_paths:
            usage = "command_only"
        elif module in offline:
            usage = "script_only"
        elif module in tests:
            usage = "test_only"
        else:
            usage = (
                "unreachable_package"
                if is_package
                else (
                    "unreferenced_candidate"
                    if not incoming[module]
                    else "unreachable_candidate"
                )
            )
        relative = path.relative_to(app)
        bucket = relative.parts[0] if len(relative.parts) > 1 else "entrypoints"
        modules.append(
            {
                "module": module,
                "path": path.relative_to(root).as_posix(),
                "root": bucket,
                "lines": len(path.read_text(encoding="utf-8-sig").splitlines()),
                "is_package": is_package,
                "usage": usage,
                "reachable_from_entry": module in online,
                "import_chain": import_chain(online, module),
                "is_command_root": module in commands,
                "command_import_chain": import_chain(command_paths, module),
                "reachable_from_scripts": module in offline,
                "reachable_from_tests": module in tests,
                "app_importers": sorted(incoming[module]),
                "test_references": sorted(external[module]["tests"]),
                "script_references": sorted(external[module]["scripts"]),
                "orphan_candidate": usage == "unreferenced_candidate",
            }
        )
    root_roles = {**ROOT_ROLES, "entrypoints": "应用入口与依赖装配"}
    root_roles.update(
        {
            name: "尚未归入已登记职责"
            for name in sorted({item["root"] for item in modules} - set(root_roles))
        }
    )
    roots = [
        {
            "root": name,
            "responsibility": role,
            "modules": sum(item["root"] == name for item in modules),
            "lines": sum(item["lines"] for item in modules if item["root"] == name),
        }
        for name, role in root_roles.items()
    ]
    return {
        "entries": list(entries),
        "missing_entries": sorted(set(entries) - set(known)),
        "app_modules": len(modules),
        "reachable_modules": len(online),
        "roots": roots,
        "command_roots": sorted(commands),
        "script_only_modules": [
            item["module"] for item in modules if item["usage"] == "script_only"
        ],
        "command_only_modules": [
            item["module"]
            for item in modules
            if item["usage"] in {"command_root", "command_only"}
        ],
        "test_only_modules": [
            item["module"] for item in modules if item["usage"] == "test_only"
        ],
        "unreachable_modules": [
            item["module"] for item in modules if not item["reachable_from_entry"]
        ],
        "orphan_candidates": [
            item["module"] for item in modules if item["orphan_candidate"]
        ],
        "unreachable_candidates": [
            item["module"]
            for item in modules
            if item["usage"] == "unreachable_candidate"
        ],
        "large_modules": [item["path"] for item in modules if item["lines"] > 500],
        "largest_modules": sorted(
            modules, key=lambda item: (-item["lines"], item["module"])
        )[:10],
        "layout_violations": layout_violations(app),
        "missing_app_imports": missing,
        "unresolved_dynamic_imports": unresolved,
        "parse_errors": parse_errors,
        "modules": modules,
        "limitation": "这是包含条件导入的静态模块依赖图，可达不能证明函数实际执行。独立命令、离线脚本与测试分别计算；patch 字符串只算维护引用。候选需核查动态注册、部署入口和历史数据，不能据此自动删除。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--entry", action="append", default=None)
    arguments = parser.parse_args()
    result = audit(entries=tuple(arguments.entry or ["app.main"]))
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"modules", "largest_modules", "limitation"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return bool(
        result["missing_app_imports"]
        or result["missing_entries"]
        or result["parse_errors"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
