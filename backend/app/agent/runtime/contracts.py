"""执行内核接收的运行准备结果；不携带业务服务或数据库类型。"""

from dataclasses import dataclass, field
from typing import Callable, Awaitable, Protocol, Any
import asyncio

from app.agent.runtime.tool_policy import ToolFailurePolicy


@dataclass
class PreparedRun:
    payload: dict
    instruction: str
    purpose: str
    use_tools: bool
    display_name: str
    public_fields: frozenset[str] = frozenset()
    citations: list[dict] = field(default_factory=list)
    memory_validator: Callable[[], None] | None = None
    image_context: object | None = None
    preloaded_skills: frozenset[str] = frozenset()


@dataclass
class RunBudget:
    """父子 Agent 共用的调用额度；身份只由服务端显式传递。"""

    calls_used: int = 0
    max_calls: int = 24
    failures: ToolFailurePolicy = field(default_factory=ToolFailurePolicy)


@dataclass
class RuntimeContext:
    """不进入 checkpoint 的可信运行依赖，每次执行由请求入口重新注入。"""

    scope: Any
    role: str
    run_id: str
    root_run_id: str
    parent_run_id: str | None = None
    depth: int = 0
    budget: RunBudget = field(default_factory=RunBudget)
    tools: Any = None
    delegation_handler: Any = None
    emit: Any = None
    image_context: Any = None
    memory_validator: Any = None
    trace: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    loaded_skills: set = field(default_factory=set)
    instruction: str = ""
    task_id: str = ""
    validate_output: Any = None
    observer: Any = None
    events: Any = None
    rounds: int = 0
    repairs: int = 0
    skill_locks: dict = field(default_factory=dict)
    tool_slots: Any = None
    # 一次调用中的写工具顺序执行，子调用持有自己的锁，避免父等子死锁。
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def tool_policy(self):
        return self.budget.failures


EventSink = Callable[[str, dict], Awaitable[None]]


class AgentRunner(Protocol):
    """领域依赖的异步运行端口，具体执行图由应用层装配。"""

    async def run(
        self, role, skill, payload, schema, context, turn_id, **options
    ) -> dict: ...
