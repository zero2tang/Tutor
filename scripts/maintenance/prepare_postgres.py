"""停机维护入口：建库、Alembic 升级与官方 Saver/Store setup。"""

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from alembic import command
from app.infrastructure.config import get_settings
from app.infrastructure.persistence.database import create_database
from app.infrastructure.persistence.schema import alembic_config
from app.infrastructure.persistence.memory_documents import setup_memory_document_storage
from app.application.learning.storage import setup_checkpoint_storage


def main():
    create_database()
    command.upgrade(alembic_config(), "head")
    dsn = get_settings().postgres_dsn()
    setup_memory_document_storage(dsn)
    # psycopg async 在 Windows 需要 Selector；只影响本维护命令的事件循环。
    asyncio.run(setup_checkpoint_storage(dsn), loop_factory=asyncio.SelectorEventLoop)
    print("PostgreSQL 业务 schema、LangGraph Saver 与 Store 已初始化。")


if __name__ == "__main__":
    main()
