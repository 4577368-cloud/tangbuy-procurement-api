"""agent tasks / event logs / category mappings

Revision ID: 003_ops
Revises: 002_catalog
Create Date: 2026-07-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_ops"
down_revision: Union[str, None] = "002_catalog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_task_record",
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_agent_task_updated", "agent_task_record", ["updated_at"])

    op.create_table(
        "event_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stream", sa.String(length=64), nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_log_stream_created", "event_log", ["stream", "created_at"])
    op.create_index("ix_event_log_stream_key", "event_log", ["stream", "event_key"])

    op.create_table(
        "category_local_mapping",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("splr_item_id", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_category_map_item", "category_local_mapping", ["item_id"])
    op.create_index("ix_category_map_splr", "category_local_mapping", ["splr_item_id"])


def downgrade() -> None:
    op.drop_index("ix_category_map_splr", table_name="category_local_mapping")
    op.drop_index("ix_category_map_item", table_name="category_local_mapping")
    op.drop_table("category_local_mapping")
    op.drop_index("ix_event_log_stream_key", table_name="event_log")
    op.drop_index("ix_event_log_stream_created", table_name="event_log")
    op.drop_table("event_log")
    op.drop_index("ix_agent_task_updated", table_name="agent_task_record")
    op.drop_table("agent_task_record")
