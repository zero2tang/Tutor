"""数据库时间精度迁移。"""

from sqlalchemy import inspect, text


def migrate_timestamps(engine) -> None:
    if engine.dialect.name != "mysql":
        return
    inspector = inspect(engine)
    for table in [
        "messages",
        "agent_runs",
        "learning_sessions",
        "attempts",
        "student_states",
        "memory_items",
        "index_tasks",
    ]:
        columns = inspector.get_columns(table)
        for column in columns:
            if column["name"] not in {"created_at", "updated_at", "next_review_at"}:
                continue
            if getattr(column["type"], "fsp", None) == 6:
                continue
            null = "NULL" if column["nullable"] else "NOT NULL"
            # 表与列只能来自固定的项目白名单和数据库自省结果。
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f"ALTER TABLE `{table}` MODIFY `{column['name']}` DATETIME(6) {null}"
                    )
                )
