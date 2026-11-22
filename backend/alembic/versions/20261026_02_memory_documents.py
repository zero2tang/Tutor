"""空记忆控制表改为官方 Store 文档指针；旧正文只保留在迁移备份。"""

from alembic import op, context
import sqlalchemy as sa

revision = "20261026_02"
down_revision = "20261026_01"
branch_labels = None
depends_on = None

REMOVED = {
    "scope_memories": ("content", "claims", "source_refs"),
    "session_concept_observations": ("content", "claims", "source_refs"),
    "session_memory_extractions": ("content", "concept_tags", "experience_source_refs", "preferences", "source_refs", "unassigned"),
}


def upgrade():
    if not context.is_offline_mode():
        for table in REMOVED:
            if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM "' + table + '" LIMIT 1)')):
                raise RuntimeError("旧记忆尚未归档并清空，拒绝删除正文列：" + table)
    for table, columns in REMOVED.items():
        op.add_column(table, sa.Column("document_key", sa.String(80), nullable=False))
        for column in columns:
            op.drop_column(table, column)


def downgrade():
    raise RuntimeError("旧记忆正文不能由指针无损还原，请从独立备份恢复")
