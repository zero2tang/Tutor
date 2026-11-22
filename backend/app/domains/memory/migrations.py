"""只升级本版业务记忆表的时间精度，不回写学生事实或推测已丢失微秒。"""

from sqlalchemy import DateTime, inspect, text

TIME_COLUMNS = {
    "scope_memories": (
        "cutoff_at",
        "updated_at",
    ),
    "session_concept_observations": ("updated_at",),
    "session_memory_extractions": ("updated_at",),
    "memory_jobs": ("available_at", "lease_until", "created_at", "updated_at"),
    "evidence_concept_links": ("created_at", "updated_at"),
}


def ensure_business_memory_time_precision(engine):
    """建表后、worker启动前幂等执行；标识符只来自固定白名单。"""
    if engine.dialect.name != "mysql":
        return {"changed_columns": []}
    changed = []
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table, names in TIME_COLUMNS.items():
            if table not in tables:
                continue
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            for name in names:
                column = columns.get(name)
                if column is None:
                    continue
                if not isinstance(column["type"], DateTime):
                    raise RuntimeError("业务记忆时间列类型与迁移契约不符")
                if getattr(column["type"], "fsp", None) == 6:
                    continue
                # 本功能只使用Python侧默认值；遇到未知服务端默认不静默删改。
                if column.get("default") is not None:
                    raise RuntimeError("业务记忆时间列存在未知服务端默认值")
                nullable = "NULL" if column["nullable"] else "NOT NULL"
                connection.execute(text(f"ALTER TABLE `{table}` MODIFY COLUMN `{name}` DATETIME(6) {nullable}"))
                changed.append(table + "." + name)
    return {"changed_columns": changed}
