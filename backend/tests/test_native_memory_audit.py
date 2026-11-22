"""审计只能读取真实原生摘要及其前缀变化，空检查点不能冒充压缩证据。"""

import importlib.util
import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("native_memory_audit", ROOT / "scripts/evaluation/memory_context_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def save(saver, namespace, identifier, messages):
    checkpoint = empty_checkpoint()
    checkpoint["id"] = identifier
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": identifier}
    saver.put({"configurable": {"thread_id": "learning:session", "checkpoint_ns": namespace}},
              checkpoint, {"step": int(identifier)}, {})


def seed(path, refs):
    original = HumanMessage(
        id="original", content="这是需要完整保留来源的学生原始解释。" * 200,
        additional_kwargs={"tutor_source_refs": ["formal:1"]},
    )
    value = {"confirmed_findings": ["学生解释了自己的操作。"], "source_refs": refs}
    summary = HumanMessage(
        id="summary", content="已闭合历史摘要（不能代替正式业务状态）：\n" + json.dumps(value, ensure_ascii=False),
        additional_kwargs={"lc_source": "summarization", "tutor_source_refs": refs},
    )
    with SqliteSaver.from_conn_string(str(path)) as saver:
        save(saver, "agent", "001", [original])
        save(saver, "agent", "002", [summary])
        save(saver, "", "003", [summary])


def test_missing_native_database_is_not_created_or_counted(tmp_path):
    path = tmp_path / "missing.sqlite"
    result = audit.read_native_checkpoints(["session"], path)
    assert not result["available"] and result["summaries"] == []
    assert not path.exists()


def test_native_summary_is_deduplicated_verified_and_read_only(tmp_path):
    path = tmp_path / "native.sqlite"
    seed(path, ["formal:1"])
    before = path.read_bytes()
    result = audit.read_native_checkpoints(["session"], path)
    assert result["available"] and result["checkpoint_rows_inspected"] == 3
    assert len(result["summaries"]) == 1
    record = result["summaries"][0]
    assert record["source_coverage_valid"] and record["compression_reduced"]
    assert record["source_message_tokens"] > record["summary_message_tokens"]
    assert path.read_bytes() == before


def test_unknown_summary_source_cannot_pass_coverage_audit(tmp_path):
    path = tmp_path / "native.sqlite"
    seed(path, ["fabricated:source"])
    result = audit.read_native_checkpoints(["session"], path)
    assert result["available"] and len(result["summaries"]) == 1
    assert not result["summaries"][0]["source_coverage_valid"]
