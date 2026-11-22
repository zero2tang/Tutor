"""静态退出门槛：旧路径必须消失，编排节点与模型消费者可核对。"""

import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.application.learning.graph import build_session_graph
from app.application.learning.subgraphs.practice import generation_graph, batch_generation_graph
from app.domains.memory.workflow import build_memory_graph
from app.domains.memory.scopes.models import ScopeMemory, SessionMemoryExtraction, SessionConceptObservation
from app.application.model_registry import register_models
from app.infrastructure.persistence.database import Base


def main():
    cost = json.loads((ROOT / "data/reports/native_remaining/source_cost.json").read_text(encoding="utf-8"))
    missing = [name for name in cost["deleted_files"] if (ROOT / name).exists()]
    forbidden = ("WireChatModel", "ContextController", "BusinessTools", "current_invocation", "graph_count",
                 "delegation_checkpoint", "context_checkpoints", "InMemorySaver", "tool_registry", "prepare_legacy_context")
    references, chat, tools = [], [], []
    for path in (ROOT / "backend/app").rglob("*.py"):
        source = path.read_text(encoding="utf-8-sig")
        name = path.relative_to(ROOT).as_posix()
        for index, line in enumerate(source.splitlines(), 1):
            references.extend({"path": name, "line": index, "symbol": value} for value in forbidden if value in line)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "chat":
                chat.append({"path": name, "line": node.lineno, "purpose": "offline textbook extraction" if "/textbooks/" in name else "independent visual question retrieval"})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(decorator, ast.Name) and decorator.id == "tool" or
                isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == "tool"
                for decorator in node.decorator_list):
                tools.append({"path": name, "function": node.name, "line": node.lineno})
    nodes = set(build_session_graph().get_graph().nodes) - {"__start__", "__end__"}
    expected = {"supervisor", "prepare_assessment", "commit_assessment", "guidance", "publish", "apply_action", "await_student"}
    assert nodes == expected
    assert set(generation_graph().get_graph().nodes) - {"__start__", "__end__"} == {"draft", "blind_solve", "review"}
    assert set(batch_generation_graph().get_graph().nodes) - {"__start__", "__end__"} == {"generate"}
    register_models()
    assert "context_checkpoints" not in Base.metadata.tables
    assert len(Base.metadata.tables) == 27
    memory_nodes = set(build_memory_graph(checkpointer=None).get_graph().nodes) - {"__start__", "__end__"}
    assert memory_nodes == {"freeze", "generate", "validate", "review", "stage", "publish", "followup"}
    for model in (ScopeMemory, SessionMemoryExtraction, SessionConceptObservation):
        assert "document_key" in model.__table__.c and "content" not in model.__table__.c
    assert not missing and not references, (missing, references)
    assert all("/textbooks/" in row["path"] or row["path"].endswith("question_retrieval/vision.py") for row in chat)
    result = {"passed": True, "deleted_file_count": len(cost["deleted_files"]), "deleted_files_still_present": missing,
        "forbidden_production_references": references, "session_graph_nodes": sorted(nodes),
        "postgresql_orm_tables": len(Base.metadata.tables), "memory_graph_nodes": sorted(memory_nodes),
        "remaining_ModelClient_chat_consumers": chat,
        "real_tool_declarations": tools, "single_input_entry": "app.application.learning.entry.execute",
        "native_agent_loop": "langchain.agents.create_agent", "checkpoint_owner": "official AsyncPostgresSaver",
        "memory_store_owner": "official PostgresStore / AsyncPostgresStore",
        "check_scope": "static source and compiled graph inspection; runtime fault/model evidence is separate"}
    target = ROOT / "data/reports/postgresql_memory_migration/architecture_audit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "real_tool_declarations"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
