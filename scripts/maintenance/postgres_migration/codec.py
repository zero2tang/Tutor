"""保留原始快照指纹，并按目标列类型生成跨数据库可比较的值。"""

import base64
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json

from sqlalchemy import Boolean, DateTime, Date, LargeBinary, JSON


def encode(value):
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, (bytes, memoryview)):
        return {"$bytes": base64.b64encode(value).decode()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    raise TypeError(type(value).__name__)


def serialized(value):
    return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(rows):
    return hashlib.sha256("\n".join(sorted(serialized(row) for row in rows)).encode()).hexdigest()


def file_digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def normalize_row(table, row):
    if set(row) != set(table.columns.keys()):
        raise ValueError("快照列与目标结构不同：" + table.name)
    result = {}
    for column in table.columns:
        value = row[column.name]
        if value is None:
            if not column.nullable and not isinstance(column.type, JSON):
                raise ValueError("必填列为空：" + table.name + "." + column.name)
        elif isinstance(column.type, Boolean):
            if type(value) not in {bool, int} or value not in (0, 1):
                raise ValueError("Boolean 仅接受 true/false 或 0/1")
            value = bool(value)
        elif isinstance(column.type, DateTime):
            if isinstance(value, dict) and set(value) == {"$datetime"}:
                value = datetime.fromisoformat(value["$datetime"])
            if not isinstance(value, datetime):
                raise ValueError("时间列类型错误：" + column.name)
            # 本轮保持既有 UTC naive 契约和完整微秒。
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
        elif isinstance(column.type, Date):
            if isinstance(value, dict) and set(value) in ({"$date"}, {"$datetime"}):
                value = date.fromisoformat(next(iter(value.values())))
        elif isinstance(column.type, LargeBinary):
            if isinstance(value, dict) and set(value) == {"$bytes"}:
                value = base64.b64decode(value["$bytes"], validate=True)
            if not isinstance(value, (bytes, memoryview)):
                raise ValueError("二进制列类型错误：" + column.name)
            value = bytes(value)
        elif not isinstance(column.type, JSON) and isinstance(value, dict) and set(value) == {"$decimal"}:
            value = Decimal(value["$decimal"])
        result[column.name] = value
    serialized(result)
    return result
