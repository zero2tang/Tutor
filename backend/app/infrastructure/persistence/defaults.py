"""数据库模型共用的 UTC 时间和标识符默认值，不依赖业务域。"""

from datetime import datetime, timezone
from uuid import uuid4


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def uid() -> str:
    return str(uuid4())
