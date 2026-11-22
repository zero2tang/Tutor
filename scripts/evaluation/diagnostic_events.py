"""只读显式启用的临时指标日志；不再要求恢复已退役的 SQL 事件表。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace


def read_diagnostic_events(session_ids):
    if not session_ids:
        return []
    location = os.getenv("TUTOR_DIAGNOSTIC_LOG")
    if not location:
        raise RuntimeError("逐请求指标验收需让服务和验收进程使用相同的 TUTOR_DIAGNOSTIC_LOG；SQL 事件表已退役。")
    source = Path(location)
    items = []
    for path in (Path(str(source) + ".1"), source):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # 正在写入的末行下次重新读取。
            if value.get("session_id") in session_ids:
                items.append(SimpleNamespace(**value))
    return items
