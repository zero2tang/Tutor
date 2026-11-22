"""真实摘要模型连续两轮压缩并重开 SQLite；合成历史明确作为验收夹具。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Annotated, TypedDict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Overwrite
from app.application.learning.storage import checkpoint_storage
from app.application.agent.runner import AgentRunner
from app.application.learning.storage import checkpoint_serde
from app.agent.runtime.contracts import PreparedRun
from app.agent.tools.contracts import ToolContext
from app.domains.learning.schemas import Plan
from app.infrastructure.clients.llm import ModelClient


class State(TypedDict, total=False):
    payload: dict
    messages: Annotated[list, add_messages]
    output: dict


class EmptyTools:
    def for_role(self, role, **kwargs):
        return []


class Recorder:
    def __init__(self):
        self.runs = []

    def start(self, identifier, *args):
        self.runs.append({"id": identifier})

    def complete(self, identifier, duration, usage, trace, output):
        self.runs[-1].update(usage=usage, duration_ms=duration, status="completed")

    def fail(self, identifier, duration, usage, trace, error):
        self.runs[-1].update(usage=usage, duration_ms=duration, status="failed", error_type=type(error).__name__)


async def prepare(role, skill, payload, schema, context, identifier, emit, use_tools, max_tokens, **kwargs):
    return PreparedRun(payload=payload, purpose="teaching", use_tools=True, display_name="摘要验收",
        instruction="这是隔离的运行框架验收，历史是合成夹具。不要推断掌握。依据当前原题和原答，简短确认已完整读取，action=respond，concept_ids和steps为空。不要调用其他业务工具。",
        public_fields=frozenset({"message"}))


def summaries(messages):
    return [message for message in messages if message.additional_kwargs.get("lc_source") == "summarization"]


async def main():
    target = ROOT / "data/reports/postgresql_memory_migration/real_compaction.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    events, record = [], Recorder()
    models = ModelClient()
    # 仅本验收进程降低软水位，以较少的合成历史触发两轮；正式部署配置不变。
    models.settings = replace(models.settings, context_soft_ratio=.08, context_keep_recent_turns=1,
                              context_summary_timeout_seconds=90)
    runner = AgentRunner(models, EmptyTools(), prepare_run=prepare, recorder=record)
    config = {"configurable": {"thread_id": "native-compaction-fixture"}, "metadata": {"tutor_durable": True}}
    report = {"fixture_history": True, "production_threshold_changed": False, "rounds": []}

    async def emit(event, data):
        events.append({"event": event, "data": data})

    async def activity(state, config):
        result = await runner.run("supervisor", "auto", state["payload"], Plan,
            ToolContext(course_id="fixture", student_id="evaluation_compaction", session_id="fixture",
                concept_ids=[], request_id="round-" + str(state["payload"]["round"])), "fixture-turn",
            emit=emit, messages=state.get("messages"), config=config)
        return {"messages": Overwrite(result["messages"]), "output": result["output"]}

    builder = StateGraph(State)
    builder.add_node("supervisor", activity)
    builder.add_edge(START, "supervisor")
    builder.add_edge("supervisor", END)
    original = "原题：同一个月，甲体重增加2千克、乙减少1千克，写出增长量。"
    raw = "  我的原答是：甲 +2 kg，乙 -1 kg。\n减少可以看成负的增长。  "
    # 每条内容不同且有稳定来源；不依赖重复字符串压缩得到虚假的节省。
    history = [{"role": "user" if index % 2 == 0 else "assistant",
        "content": f"夹具记录{index}：" + "学生解释增长方向、单位和基准；这一段仅说明讨论过程，不提供掌握证据。" * 20}
        for index in range(12)]
    try:
        for round_number in (1, 2):
            async with checkpoint_storage() as saver:
                graph = builder.compile(checkpointer=saver)
                if round_number == 1:
                    await saver.adelete_thread(config["configurable"]["thread_id"])
                payload = {"round": round_number, "question": original, "student_answer": raw,
                    "user_message": "请确认完整读取本轮原题原答。",
                    "help_snapshot": {"assisted": False, "source": "fixture"}}
                if round_number == 1:
                    payload["recent_messages"] = history
                else:
                    restored = await graph.aget_state(config)
                    assert summaries(restored.values["messages"]), "重开 saver 后必须已有真实摘要"
                result = await graph.ainvoke({"payload": payload}, config=config, durability="sync")
                compressed = summaries(result["messages"])
                assert len(compressed) == 1, "每轮只应保留当前摘要前缀"
                assert not any(message.id and message.id.startswith("recent_messages:") for message in result["messages"])
                fixed = [json.loads(message.content) for message in result["messages"]
                    if message.additional_kwargs.get("tutor_current_run")]
                assert fixed[-1]["question"] == original and fixed[-1]["student_answer"] == raw
                report["rounds"].append({"round": round_number,
                    "summary_source_refs": compressed[0].additional_kwargs["tutor_source_refs"],
                    "summary": compressed[0].content, "state_message_count": len(result["messages"]),
                    "old_prefix_absent": True, "current_raw_answer_exact": True,
                    "compression_events": sum(e["event"] == "context_budget" and e["data"].get("status") == "compressed" for e in events)})
        assert report["rounds"][1]["compression_events"] >= 2
        assert set(report["rounds"][0]["summary_source_refs"]).issubset(report["rounds"][1]["summary_source_refs"])
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:600])
        raise
    finally:
        report.update(runs=record.runs, model=models.settings.llm_model,
                      compressed_events=[e for e in events if e["event"] == "context_budget"])
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await models.close()
        print(json.dumps({"passed": report.get("passed"), "rounds": len(report["rounds"]), "report": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    from app.infrastructure.persistence.async_runtime import selector_loop
    with asyncio.Runner(loop_factory=selector_loop) as event_runner:
        event_runner.run(main())
