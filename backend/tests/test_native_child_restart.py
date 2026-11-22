"""新 Python 进程重建实际 create_agent，验证父子执行栈与正式工具回执。"""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import aiosqlite
import pytest
from langchain.tools import tool, ToolRuntime
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import StateGraph, MessagesState, START, END
from pydantic import ConfigDict

from app.agent.runtime.contracts import PreparedRun, RuntimeContext
from app.agent.tools.contracts import ToolContext
from app.application.agent.runner import AgentRunner
from app.domains.learning.schemas import Plan, Lesson
from app.shared.execution import LearningTurnLost


def formal(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS counters (kind TEXT, role TEXT);
        CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS flags (id TEXT PRIMARY KEY);
    """)
    return connection


def count(path, kind, role):
    with formal(path) as database:
        database.execute("INSERT INTO counters VALUES (?, ?)", (kind, role))


def once(path, marker):
    with formal(path) as database:
        return database.execute("INSERT OR IGNORE INTO flags VALUES (?)", (marker,)).rowcount == 1


class RecordingModel(BaseChatModel):
    """只提供标准模型协议，真实工具调度与消息回填仍由 create_agent 完成。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    path: str

    @property
    def _llm_type(self):
        return "native-restart-test"

    def bind_tools(self, tools, **options):
        return self.bind(tools=[convert_to_openai_tool(value) for value in tools])

    def _generate(self, messages, **options):
        raise NotImplementedError("验收仅使用异步模型端口")

    async def _agenerate(self, messages, **options):
        role = messages[0].content
        count(self.path, "model", role)
        names = [message.name for message in messages if message.type == "tool"]
        if role == "knowledge":
            name, args = ("Lesson", {"title": "知识点讲解", "content": "根据已经提交的真实工具回执解释知识点，保留正确步骤和未知条件，不把讲解完成等同于掌握。",
                "follow_up": "请说明下一步。", "next_action": "等待学生回答"}) if "commit_fact" in names else ("commit_fact", {})
        else:
            name, args = ("Plan", {"action": "respond", "concept_ids": [], "message": "已读取知识点子 Agent 的真实回执。",
                "steps": [], "rationale": "依据已执行结果继续。"}) if "call_knowledge_agent" in names else ("call_knowledge_agent", {"task": "解释当前知识点"})
        message = AIMessage(content="", tool_calls=[{"id": role + "-" + name, "name": name, "args": args, "type": "tool_call"}],
            usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            response_metadata={"finish_reason": "tool_calls"})
        return ChatResult(generations=[ChatGeneration(message=message)])


class Recorder:
    def start(self, *args): pass
    def complete(self, *args): pass
    def fail(self, *args): pass


async def prepare(role, skill, payload, schema, scope, run_id, emit, use_tools, max_tokens, **options):
    return PreparedRun(payload=payload, instruction=role, purpose="teaching", use_tools=True, display_name=role)


async def run_phase(directory, scenario, phase):
    from app.application.agent import graph as agent_graph
    from app.application.agent import summary

    path = str(Path(directory) / "formal.sqlite")
    models = SimpleNamespace(settings=SimpleNamespace(llm_model="native-test"), limit=asyncio.Semaphore(3))
    agent_graph.create_chat_model = lambda models, **options: RecordingModel(path=path)
    summary.create_chat_model = agent_graph.create_chat_model

    @tool
    async def commit_fact(runtime: ToolRuntime[RuntimeContext]):
        """提交一个稳定标识的正式事实，并回放已经完成的正式回执。"""
        count(path, "tool", "commit_fact")
        identifier = "fact:" + runtime.context.scope.request_id
        with formal(path) as database:
            prior = database.execute("SELECT value FROM facts WHERE id=?", (identifier,)).fetchone()
            if prior:
                return {**json.loads(prior[0]), "replayed": True}
            result = {"id": identifier, "value": 7}
            database.execute("INSERT INTO facts VALUES (?, ?)", (identifier, json.dumps(result)))
        if scenario == "tool_commit" and once(path, "effect-return-failure"):
            raise LearningTurnLost("模拟效果已提交但ToolMessage尚未写入checkpoint")
        return result

    @tool
    async def call_knowledge_agent(task: str, runtime: ToolRuntime[RuntimeContext]):
        """真实调用知识点子 Agent，执行结果返回本次主模型。"""
        count(path, "tool", "call_knowledge_agent")
        result = await runner.run("knowledge", "auto", {"task": task}, Lesson,
            runtime.context.scope, "turn", runtime_context=runtime.context, config=runtime.config)
        return {"lesson": result["output"], "child_run_ids": [result["run_id"]]}

    class Provider:
        def for_role(self, role, *, handler=None):
            return [call_knowledge_agent] if role == "supervisor" else [commit_fact]

    runner = AgentRunner(models, Provider(), prepare_run=prepare, recorder=Recorder())

    async def supervisor(state, config: RunnableConfig):
        result = await runner.run("supervisor", "auto", {"task": "学习当前知识点"}, Plan,
            ToolContext(course_id="math", session_id="session", request_id="stable-request"), "turn", config=config)
        if scenario == "parent_return" and once(path, "parent-return-failure"):
            raise RuntimeError("模拟子Agent完成后父节点尚未提交")
        return {"messages": result["messages"]}

    # 明确允许原始领域Schema，禁止pickle和不受限模块反序列化。
    serde = JsonPlusSerializer(allowed_msgpack_modules=[Plan, Lesson])
    async with aiosqlite.connect(str(Path(directory) / "graph.sqlite")) as connection:
        saver = AsyncSqliteSaver(connection, serde=serde)
        await saver.setup()
        builder = StateGraph(MessagesState)
        builder.add_node("supervisor", supervisor)
        builder.add_edge(START, "supervisor")
        builder.add_edge("supervisor", END)
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "restart"}, "metadata": {"tutor_durable": True}}
        if phase == "first":
            try:
                await graph.ainvoke({"messages": []}, config, durability="sync")
            except (RuntimeError, LearningTurnLost):
                pass
            else:
                raise AssertionError("故障注入没有发生")
            assert (await graph.aget_state(config)).next == ("supervisor",)
        else:
            await graph.ainvoke(None, config, durability="sync")
            assert not (await graph.aget_state(config)).next
        restored_types = set()
        async for checkpoint in saver.alist(None):
            value = checkpoint.checkpoint["channel_values"].get("structured_response")
            if value is not None:
                assert isinstance(value, (Plan, Lesson)), type(value)
                restored_types.add(type(value).__name__)
    with formal(path) as database:
        counts = {kind + ":" + role: amount for kind, role, amount in database.execute("SELECT kind, role, COUNT(*) FROM counters GROUP BY kind, role")}
        facts = database.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    return {"counts": counts, "facts": facts, "types": sorted(restored_types)}


@pytest.mark.parametrize("scenario", ["parent_return", "tool_commit"])
def test_native_nested_agent_resume_in_new_process(tmp_path, scenario):
    script = Path(__file__).resolve()
    environment = {**os.environ, "PYTHONPATH": str(script.parents[1]), "PYTHONIOENCODING": "utf-8"}
    def phase(name):
        completed = subprocess.run([sys.executable, str(script), str(tmp_path), scenario, name],
            cwd=script.parents[1], env=environment, capture_output=True, encoding="utf-8", timeout=40)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout.strip())
    before, after = phase("first"), phase("second")
    assert before["facts"] == after["facts"] == 1
    assert after["types"] == ["Lesson", "Plan"]
    if scenario == "parent_return":
        assert before["counts"] == after["counts"] == {"model:knowledge": 2, "model:supervisor": 2,
            "tool:call_knowledge_agent": 1, "tool:commit_fact": 1}
    else:
        assert before["counts"]["model:knowledge"] == before["counts"]["model:supervisor"] == 1
        assert after["counts"]["model:knowledge"] == after["counts"]["model:supervisor"] == 2
        assert after["counts"]["tool:commit_fact"] == 2


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_phase(sys.argv[1], sys.argv[2], sys.argv[3])), ensure_ascii=False))
