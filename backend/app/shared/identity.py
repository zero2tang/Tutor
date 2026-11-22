"""正式业务操作的稳定主键，不依赖一次执行的 run 或 turn 标识。"""

from uuid import NAMESPACE_URL, uuid5


def operation_id(session_id: str, request_id: str, kind: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"tutor:{session_id}:{request_id}:{kind}"))
