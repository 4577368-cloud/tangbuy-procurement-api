"""initial procurement tables

Revision ID: 001_initial
Revises:
Create Date: 2026-07-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "procurement_pipeline_state",
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("pipeline_step", sa.String(length=32), nullable=False),
        sa.Column("ord_line_stat", sa.Integer(), nullable=True),
        sa.Column("blockers_json", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ord_line_no"),
    )
    op.create_table(
        "procurement_blocker_ack",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("blocker_key", sa.String(length=64), nullable=False),
        sa.Column("operator", sa.String(length=128), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ord_line_no", "blocker_key", name="uq_blocker_ack"),
    )
    op.create_index("ix_procurement_blocker_ack_ord_line_no", "procurement_blocker_ack", ["ord_line_no"])

    op.create_table(
        "procurement_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("ord_no", sa.String(length=64), nullable=True),
        sa.Column("action_key", sa.String(length=64), nullable=False),
        sa.Column("action_label", sa.String(length=128), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=True),
        sa.Column("operator", sa.String(length=128), nullable=True),
        sa.Column("admin_write", sa.String(length=16), nullable=True),
        sa.Column("ord_line_stat_before", sa.Integer(), nullable=True),
        sa.Column("ord_line_stat_after", sa.Integer(), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_procurement_audit_log_ord_line_no", "procurement_audit_log", ["ord_line_no"])
    op.create_index("ix_procurement_audit_log_action_key", "procurement_audit_log", ["action_key"])
    op.create_index("ix_audit_ord_line_created", "procurement_audit_log", ["ord_line_no", "created_at"])

    op.create_table(
        "ord_line_snapshot",
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("ord_no", sa.String(length=64), nullable=False),
        sa.Column("ord_line_stat", sa.Integer(), nullable=True),
        sa.Column("pay_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("splr_item_id", sa.String(length=64), nullable=True),
        sa.Column("item_nm", sa.Text(), nullable=True),
        sa.Column("queue", sa.String(length=32), nullable=True),
        sa.Column("admin_fingerprint", sa.String(length=512), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ord_line_no"),
    )
    op.create_index("ix_snapshot_ord_no", "ord_line_snapshot", ["ord_no"])
    op.create_index("ix_snapshot_queue_stat", "ord_line_snapshot", ["queue", "ord_line_stat"])
    op.create_index("ix_snapshot_pay_time", "ord_line_snapshot", ["pay_time"])

    op.create_table(
        "sync_cursor",
        sa.Column("queue_name", sa.String(length=64), nullable=False),
        sa.Column("backfill_page", sa.Integer(), nullable=False),
        sa.Column("backfill_complete", sa.Boolean(), nullable=False),
        sa.Column("last_incremental_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_backfill_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("queue_name"),
    )


def downgrade() -> None:
    op.drop_table("sync_cursor")
    op.drop_table("ord_line_snapshot")
    op.drop_index("ix_audit_ord_line_created", table_name="procurement_audit_log")
    op.drop_index("ix_procurement_audit_log_action_key", table_name="procurement_audit_log")
    op.drop_index("ix_procurement_audit_log_ord_line_no", table_name="procurement_audit_log")
    op.drop_table("procurement_audit_log")
    op.drop_index("ix_procurement_blocker_ack_ord_line_no", table_name="procurement_blocker_ack")
    op.drop_table("procurement_blocker_ack")
    op.drop_table("procurement_pipeline_state")
