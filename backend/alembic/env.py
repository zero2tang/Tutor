"""业务结构迁移只管理 public；框架的两个 schema 由官方 setup 管理。"""

from alembic import context
from sqlalchemy import create_engine, pool

from app.application.model_registry import register_models
from app.infrastructure.config import get_settings

target_metadata = register_models()


def include_object(obj, name, kind, reflected, compare_to):
    return not (kind == "table" and getattr(obj, "schema", None) not in {None, "public"})


def migrate(connection):
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=get_settings().database_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    external = context.config.attributes.get("connection")
    if external is not None:
        migrate(external)
    else:
        engine = create_engine(get_settings().database_url(), poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()
