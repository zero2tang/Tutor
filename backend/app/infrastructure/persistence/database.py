"""数据库连接与短事务边界；模型请求期间不占用写事务。"""

from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.infrastructure.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    return create_engine(get_settings().database_url(), pool_pre_ping=True, pool_recycle=1800)


@contextmanager
def db_session():
    factory = sessionmaker(engine(), expire_on_commit=False)
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def create_database() -> None:
    """按配置建立数据库；不导入业务表或执行业务迁移。"""
    settings = get_settings()
    # 数据库名来自本项目配置，并限制字符，避免拼接任意标识符。
    if not settings.database_name.replace("_", "").isalnum():
        raise ValueError("数据库名只能包含字母、数字和下划线")
    admin = create_engine(settings.database_url(database=False), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            if not connection.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": settings.database_name}):
                connection.exec_driver_sql('CREATE DATABASE "' + settings.database_name + '"')
    finally:
        admin.dispose()


def create_tables(metadata) -> None:
    """旧命令入口只检查结构；业务建表统一由 Alembic 完成。"""
    from app.infrastructure.persistence.schema import assert_schema_current

    with engine().connect() as connection:
        assert_schema_current(connection)
