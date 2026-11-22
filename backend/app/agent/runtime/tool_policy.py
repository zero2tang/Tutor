"""同一逻辑任务的工具故障熔断；业务拒绝和模型参数错误另行处理。"""

import asyncio
from contextlib import asynccontextmanager

from app.agent.tools.errors import ToolRejected
from app.shared.execution import LearningTurnLost

MAX_TOOL_CALLS = 24
SUPERVISOR_TOOL_ROUNDS = 12
SPECIALIST_TOOL_ROUNDS = 8
MAX_DELEGATIONS = 8


class ToolCircuitOpen(RuntimeError):
    """同名工具已经达到本任务的故障上限，不能再实际执行。"""


def is_execution_error(error):
    """保留批量包装的原始故障类别，不能靠异常文本猜测。"""
    if isinstance(error, (ToolRejected, ToolCircuitOpen, PermissionError)):
        return False
    if isinstance(error, BaseExceptionGroup):
        return any(is_execution_error(item) for item in error.exceptions)
    if isinstance(error, ValueError):
        return bool(error.__cause__ and is_execution_error(error.__cause__))
    return True


def raise_ownership_loss(error):
    """并发异常中的失权同样终止整个请求，不能转为可重试工具反馈。"""
    if isinstance(error, LearningTurnLost):
        raise error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            raise_ownership_loss(child)


class ToolFailurePolicy:
    limit = 3

    def __init__(self):
        self.failure_counts = {}
        self._conditions = {}
        self._in_flight = {}

    def is_blocked(self, name):
        return self.failure_counts.get(name, 0) >= self.limit

    @property
    def blocked_names(self):
        return {name for name in self.failure_counts if self.is_blocked(name)}

    def record_failure(self, name):
        self.failure_counts[name] = self.failure_counts.get(name, 0) + 1

    @asynccontextmanager
    async def execution_slot(self, name):
        # 失败数+同名在途数不超过3，防止同轮并发绕过故障熔断。
        condition = self._conditions.setdefault(name, asyncio.Condition())
        async with condition:
            await condition.wait_for(
                lambda: self.is_blocked(name)
                or self.failure_counts.get(name, 0) + self._in_flight.get(name, 0)
                < self.limit
            )
            if self.is_blocked(name):
                raise ToolCircuitOpen(
                    f"工具{name}在本任务中已执行失败3次，已禁用；请使用其他工具或说明缺口。"
                )
            self._in_flight[name] = self._in_flight.get(name, 0) + 1
        try:
            yield
        finally:
            async with condition:
                self._in_flight[name] -= 1
                condition.notify_all()
