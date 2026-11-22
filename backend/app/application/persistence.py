"""应用启动仅检查业务 schema；建库、升级和种子导入属于维护命令。"""

from app.infrastructure.persistence.database import engine
from app.infrastructure.persistence.schema import assert_schema_current


def initialize_database() -> None:
    """不自动升级，也不在正式服务中隐式恢复历史数据。"""
    with engine().connect() as connection:
        assert_schema_current(connection)
