"""product / config / disposition tables

Revision ID: 002_catalog
Revises: 001_initial
Create Date: 2026-07-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_catalog"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_record",
        sa.Column("tangbuy_product_id", sa.String(length=64), nullable=False),
        sa.Column("source_product_id", sa.String(length=64), nullable=True),
        sa.Column("product_name", sa.Text(), nullable=True),
        sa.Column("category_status", sa.String(length=32), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tangbuy_product_id"),
    )
    op.create_index("ix_product_source_id", "product_record", ["source_product_id"])

    op.create_table(
        "product_ord_line_link",
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("tangbuy_product_id", sa.String(length=64), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ord_line_no"),
    )
    op.create_index("ix_product_link_product", "product_ord_line_link", ["tangbuy_product_id"])

    op.create_table(
        "app_config_document",
        sa.Column("doc_key", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("doc_key"),
    )

    op.create_table(
        "disposition_override",
        sa.Column("ord_line_no", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ord_line_no"),
    )


def downgrade() -> None:
    op.drop_table("disposition_override")
    op.drop_table("app_config_document")
    op.drop_index("ix_product_link_product", table_name="product_ord_line_link")
    op.drop_table("product_ord_line_link")
    op.drop_index("ix_product_source_id", table_name="product_record")
    op.drop_table("product_record")
