"""workflow run record table

Revision ID: 005_workflow_run
Revises: 004_tangbuy_cols
Create Date: 2026-07-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_workflow_run"
down_revision: Union[str, None] = "004_tangbuy_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_run_record",
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("run_json", sa.JSON(), nullable=False),
        sa.Column("crt_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("upd_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ord_line_no"),
    )
    op.create_index("ix_workflow_run_updated", "workflow_run_record", ["upd_time"])


def downgrade() -> None:
    op.drop_index("ix_workflow_run_updated", table_name="workflow_run_record")
    op.drop_table("workflow_run_record")
