"""离线记录并比较目录重构前后的 ORM、协议、API 和技能契约。

运行方式（从项目根目录执行，已有快照拒绝覆盖）：
  # 迁移前记录原始目录契约。
  backend/.venv/Scripts/python.exe scripts/maintenance/check_backend_contracts.py --snapshot tmp/app-layout/before_contracts.json
  # 迁移后通过显式清单比较原始基线。
  backend/.venv/Scripts/python.exe scripts/maintenance/check_backend_contracts.py --compare tmp/app-layout/before_contracts.json --manifest data/reports/backend_layout_manifest.json
  # 当前目录也可以记录新快照，并用相同清单再次比较。
  backend/.venv/Scripts/python.exe scripts/maintenance/check_backend_contracts.py --snapshot tmp/app-layout/current_contracts.json --manifest data/reports/backend_layout_manifest.json
  backend/.venv/Scripts/python.exe scripts/maintenance/check_backend_contracts.py --compare tmp/app-layout/current_contracts.json --manifest data/reports/backend_layout_manifest.json

manifest 的 symbols 字段接受 old.module.Symbol -> new.module.Symbol；模块映射可放
modules 字段。比较只按显式映射解析 Python 符号，JSON schema 和 OpenAPI 不做模糊归一化。
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MODULES = (
    "app.schemas.agents",
    "app.schemas.evidence",
    "app.schemas.session_goals",
    "app.schemas.textbooks",
    "app.guidance.schemas",
    "app.api.schemas",
    "app.api.memory",
    "app.memory.schemas",
    "app.memory.observations",
)
SYMBOLS = {
    "metadata_base": "app.core.database.Base",
    "project_root": "app.core.config.ROOT",
    "skills_library": "app.runtime.skills.SkillLibrary",
    "application": "app.main.app",
}


class SymbolResolver:
    def __init__(self, manifest):
        self.symbols = manifest.get("symbols", manifest.get("symbol_map", {}))
        if not self.symbols and all(isinstance(value, str) for value in manifest.values()):
            self.symbols = manifest
        self.modules = manifest.get("modules", manifest.get("module_map", {}))
        self.paths = manifest.get("paths", {})
        self.registration = manifest.get("model_registration")

    def mapped_name(self, name):
        if name in self.symbols:
            return self.symbols[name]
        # 优先选择最具体的模块映射，避免 app 的映射遮住子模块。
        for old in sorted(self.modules, key=len, reverse=True):
            if name == old or name.startswith(old + "."):
                return self.modules[old] + name[len(old):]
        return name

    def get(self, name):
        target = self.mapped_name(name)
        module, _, attribute = target.rpartition(".")
        return getattr(importlib.import_module(module), attribute)

    def baseline_name(self, name):
        for old, new in sorted(
            self.symbols.items(), key=lambda item: (len(item[1]), len(item[0])), reverse=True
        ):
            if name == new or name.startswith(new + "."):
                return old + name[len(new):]
        for old, new in sorted(self.modules.items(), key=lambda item: len(item[1]), reverse=True):
            if name == new or name.startswith(new + "."):
                return old + name[len(new):]
        return name

    def expected_path(self, path):
        resolved = Path(path).resolve()
        for old, new in sorted(self.paths.items(), key=lambda item: len(item[0]), reverse=True):
            try:
                relative = resolved.relative_to(Path(old).resolve())
            except ValueError:
                continue
            return str((Path(new).resolve() / relative).resolve())
        return str(resolved)

    def register_models(self, sources):
        if self.registration:
            entries = [self.registration] if isinstance(self.registration, str) else self.registration
            for entry in entries:
                self.get(entry)()
        else:
            for registry in sources:
                importlib.import_module(self.mapped_name(registry))


def forbidden_io(*args, **kwargs):
    raise RuntimeError("契约快照禁止创建数据库引擎或访问网络；请检查导入副作用。")


def callable_name(function, resolver):
    function = inspect.unwrap(function)
    name = f"{function.__module__}.{function.__qualname__}"
    return resolver.baseline_name(name)


def default_value(default, resolver):
    if default is None:
        return None
    arg = getattr(default, "arg", default)
    if callable(arg):
        return {"kind": "callable", "value": callable_name(arg, resolver)}
    if isinstance(arg, (str, int, float, bool, list, dict)) or arg is None:
        return {"kind": "literal", "value": arg}
    return {"kind": "sql", "value": str(arg)}


def metadata_contract(base, resolver):
    from sqlalchemy.dialects import mysql

    tables = {}
    for table in sorted(base.metadata.tables.values(), key=lambda item: item.fullname):
        columns = []
        for column in table.columns:
            columns.append({
                "name": column.name,
                "key": column.key,
                "type": str(column.type.compile(dialect=mysql.dialect())),
                "type_class": f"{type(column.type).__module__}.{type(column.type).__qualname__}",
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "autoincrement": column.autoincrement,
                "unique": column.unique,
                "index": column.index,
                "default": default_value(column.default, resolver),
                "onupdate": default_value(column.onupdate, resolver),
                "server_default": default_value(column.server_default, resolver),
                "server_onupdate": default_value(column.server_onupdate, resolver),
                "comment": column.comment,
                "foreign_keys": sorted(key.target_fullname for key in column.foreign_keys),
            })
        constraints = []
        for constraint in table.constraints:
            constraints.append({
                "kind": type(constraint).__name__,
                "name": constraint.name,
                "columns": [column.name for column in constraint.columns],
                "sqltext": str(constraint.sqltext) if hasattr(constraint, "sqltext") else None,
                "deferrable": getattr(constraint, "deferrable", None),
                "initially": getattr(constraint, "initially", None),
                "ondelete": getattr(constraint, "ondelete", None),
                "onupdate": getattr(constraint, "onupdate", None),
            })
        indexes = [{
            "name": index.name,
            "unique": index.unique,
            "expressions": [str(expression) for expression in index.expressions],
        } for index in table.indexes]
        tables[table.fullname] = {
            "columns": columns,
            "constraints": sorted(constraints, key=lambda item: json.dumps(item, sort_keys=True)),
            "indexes": sorted(indexes, key=lambda item: item["name"] or ""),
            "comment": table.comment,
        }
    return tables


def schema_symbols(resolver):
    from pydantic import BaseModel

    def is_model(value):
        return (inspect.isclass(value) and issubclass(value, BaseModel)
                and value is not BaseModel)

    result = set()
    # 模块映射负责未拆分的协议；保留旧标识作为快照键，实际加载交给 resolver。
    for source in SCHEMA_MODULES:
        target = resolver.mapped_name(source)
        module = importlib.import_module(target)
        for name, value in vars(module).items():
            if is_model(value) and value.__module__ == target:
                actual = f"{target}.{name}"
                aliases = [old for old, new in resolver.symbols.items()
                           if old.rpartition(".")[0] == source and new == actual]
                result.update(aliases or [f"{source}.{name}"])
    # 一个旧模块可能拆到多个业务域；共享基类也可能同时保留多个原始协议键。
    for old in resolver.symbols:
        if old.rpartition(".")[0] in SCHEMA_MODULES and is_model(resolver.get(old)):
            result.add(old)
    return sorted(result)


def effective_routes(routes):
    for route in routes:
        # FastAPI 的延迟 include 节点没有 path；读取最终上下文才含完整前缀和路由名。
        if hasattr(route, "effective_route_contexts"):
            yield from route.effective_route_contexts()
        elif hasattr(route, "routes"):
            yield from effective_routes(route.routes)
        else:
            yield route


def snapshot(resolver, baseline=None):
    import sqlalchemy

    with ExitStack() as guards:
        # 只导入应用对象，不执行 lifespan、端点、数据库建表或模型调用。
        guards.enter_context(patch.object(sqlalchemy, "create_engine", forbidden_io))
        guards.enter_context(patch.object(socket.socket, "connect", forbidden_io))
        guards.enter_context(patch.object(socket.socket, "connect_ex", forbidden_io))
        application = resolver.get(SYMBOLS["application"])
        registries = baseline["sources"]["model_registries"] if baseline else ["app.models"]
        resolver.register_models(registries)
        base = resolver.get(SYMBOLS["metadata_base"])
        classes = baseline["sources"]["schema_symbols"] if baseline else schema_symbols(resolver)
        schemas = {}
        for name in classes:
            schema_class = resolver.get(name)
            schemas[name] = {
                "validation": schema_class.model_json_schema(mode="validation"),
                "serialization": schema_class.model_json_schema(mode="serialization"),
            }
        routes = []
        for route in effective_routes(application.routes):
            path = getattr(route, "path", None)
            if path is None:
                continue
            routes.append({
                "path": path,
                "name": getattr(route, "name", None),
                "methods": sorted(getattr(route, "methods", None) or []),
                "include_in_schema": getattr(route, "include_in_schema", None),
            })
        library = resolver.get(SYMBOLS["skills_library"])()
        skills = {}
        for role in ("supervisor", "knowledge", "exercise", "assessment"):
            skills[role] = []
            for item in library.catalog(role):
                content = library.read(role, item["name"])["instructions"]
                skills[role].append({
                    **item,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                })
        return {
            "format_version": 1,
            "sources": {
                "schema_symbols": classes,
                "model_registries": ["app.models"],
                "symbols": SYMBOLS,
            },
            "orm_tables": metadata_contract(base, resolver),
            "schemas": schemas,
            "api_routes": sorted(routes, key=lambda item: (item["path"], item["methods"], item["name"] or "")),
            "openapi": application.openapi(),
            "resources": {
                "project_root": str(resolver.get(SYMBOLS["project_root"]).resolve()),
                "skills_root": str(library.root.resolve()),
                "skills_root_exists": library.root.is_dir(),
                "skills": skills,
            },
        }


def differences(before, after, path=""):
    if type(before) is not type(after):
        yield {"path": path or "/", "before": before, "after": after}
    elif isinstance(before, dict):
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in before:
                yield {"path": child, "added": after[key]}
            elif key not in after:
                yield {"path": child, "removed": before[key]}
            else:
                yield from differences(before[key], after[key], child)
    elif isinstance(before, list):
        if len(before) != len(after):
            yield {"path": path + "/length", "before": len(before), "after": len(after)}
        for index, (left, right) in enumerate(zip(before, after)):
            yield from differences(left, right, path + "/" + str(index))
    elif before != after:
        yield {"path": path or "/", "before": before, "after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", type=Path, help="记录当前目录的契约快照；已存在时拒绝覆盖")
    mode.add_argument("--compare", type=Path, help="与迁移前基线逐项比较")
    parser.add_argument("--manifest", type=Path, help="迁移符号和模块的显式映射 JSON")
    parser.add_argument("--report", type=Path, help="将完整差异另存 JSON，不改动基线")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "backend"))
    if args.snapshot and args.snapshot.exists():
        parser.error(f"基线已存在，拒绝覆盖：{args.snapshot}")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig")) if args.manifest else {}
    resolver = SymbolResolver(manifest)
    baseline = json.loads(args.compare.read_text(encoding="utf-8-sig")) if args.compare else None
    # SQLAlchemy 的自动命名字符串含 str 子类；按实际落盘 JSON 类型比较。
    try:
        current = json.loads(json.dumps(snapshot(resolver, baseline)))
    except (ModuleNotFoundError, AttributeError) as exc:
        hint = ("请检查 --manifest 中的模块和符号映射。" if args.manifest else
                "当前结构已迁移时，请加 --manifest data/reports/backend_layout_manifest.json。")
        parser.error(f"无法离线载入契约符号：{exc}。{hint}")
    summary = {
        "tables": len(current["orm_tables"]),
        "schema_classes": len(current["schemas"]),
        "routes": len(current["api_routes"]),
        "openapi_paths": len(current["openapi"]["paths"]),
        "skills": sum(map(len, current["resources"]["skills"].values())),
    }
    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"snapshot": str(args.snapshot), **summary}, ensure_ascii=False))
        return 0
    # 资源的物理迁移必须逐个在 manifest 声明；技能清单和内容不参与路径替换。
    expected = json.loads(json.dumps(baseline))
    resource_moves = []
    for key in ("project_root", "skills_root"):
        old = expected["resources"][key]
        target = resolver.expected_path(old)
        if target != old:
            resource_moves.append({"resource": key, "before": old, "expected": target})
        expected["resources"][key] = target
    changes = list(differences(expected, current))
    report = {"equal": not changes, "difference_count": len(changes), **summary,
              "declared_resource_moves": resource_moves, "differences": changes}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "differences": changes[:40]}, ensure_ascii=False, indent=2))
    if len(changes) > 40:
        print("仅在终端展示前 40 项差异；可用 --report 保存全部差异。")
    return 1 if changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
