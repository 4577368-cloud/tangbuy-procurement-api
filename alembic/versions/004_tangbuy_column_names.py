"""rename columns to Tangbuy field-catalog names

Revision ID: 004_tangbuy_cols
Revises: 003_ops
Create Date: 2026-07-12

"""

from typing import Sequence, Union

from alembic import op

revision: str = "004_tangbuy_cols"
down_revision: Union[str, None] = "003_ops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rename(table: str, old: str, new: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.alter_column(old, new_column_name=new)


def upgrade() -> None:
    _rename("procurement_pipeline_state", "blockers_json", "pipeline_blockers")
    _rename("procurement_pipeline_state", "updated_at", "upd_time")

    _rename("procurement_audit_log", "detail_json", "audit_detail")
    _rename("procurement_audit_log", "created_at", "crt_time")
    op.drop_index("ix_audit_ord_line_created", table_name="procurement_audit_log")
    op.create_index(
        "ix_audit_ord_line_created",
        "procurement_audit_log",
        ["ord_line_no", "crt_time"],
    )

    _rename("ord_line_snapshot", "queue", "proc_queue")
    _rename("ord_line_snapshot", "payload_json", "ord_line_row")
    _rename("ord_line_snapshot", "synced_at", "upd_time")
    op.drop_index("ix_snapshot_queue_stat", table_name="ord_line_snapshot")
    op.create_index(
        "ix_snapshot_queue_stat",
        "ord_line_snapshot",
        ["proc_queue", "ord_line_stat"],
    )

    op.drop_index("ix_product_source_id", table_name="product_record")
    _rename("product_record", "tangbuy_product_id", "item_id")
    _rename("product_record", "source_product_id", "splr_item_id")
    _rename("product_record", "product_name", "item_nm")
    _rename("product_record", "category_status", "ctgy_map_stat")
    _rename("product_record", "payload_json", "item_ext_json")
    _rename("product_record", "updated_at", "upd_time")
    op.create_index("ix_item_splr_id", "product_record", ["splr_item_id"])

    op.drop_index("ix_product_link_product", table_name="product_ord_line_link")
    _rename("product_ord_line_link", "tangbuy_product_id", "item_id")
    _rename("product_ord_line_link", "linked_at", "crt_time")
    op.create_index("ix_item_ord_line_link", "product_ord_line_link", ["item_id"])

    _rename("app_config_document", "payload_json", "doc_json")
    _rename("app_config_document", "updated_at", "upd_time")

    _rename("disposition_override", "payload_json", "override_json")
    _rename("disposition_override", "updated_at", "upd_time")

    _rename("agent_task_record", "payload_json", "task_json")
    _rename("agent_task_record", "created_at", "crt_time")
    _rename("agent_task_record", "updated_at", "upd_time")
    op.drop_index("ix_agent_task_updated", table_name="agent_task_record")
    op.create_index("ix_agent_task_updated", "agent_task_record", ["upd_time"])

    _rename("event_log", "payload_json", "event_json")
    _rename("event_log", "created_at", "crt_time")
    op.drop_index("ix_event_log_stream_created", table_name="event_log")
    op.create_index("ix_event_log_stream_created", "event_log", ["stream", "crt_time"])

    _rename("category_local_mapping", "payload_json", "mapping_json")
    _rename("category_local_mapping", "updated_at", "upd_time")


def downgrade() -> None:
    _rename("category_local_mapping", "upd_time", "updated_at")
    _rename("category_local_mapping", "mapping_json", "payload_json")

    op.drop_index("ix_event_log_stream_created", table_name="event_log")
    _rename("event_log", "crt_time", "created_at")
    _rename("event_log", "event_json", "payload_json")
    op.create_index("ix_event_log_stream_created", "event_log", ["stream", "created_at"])

    op.drop_index("ix_agent_task_updated", table_name="agent_task_record")
    _rename("agent_task_record", "upd_time", "updated_at")
    _rename("agent_task_record", "crt_time", "created_at")
    _rename("agent_task_record", "task_json", "payload_json")
    op.create_index("ix_agent_task_updated", "agent_task_record", ["updated_at"])

    _rename("disposition_override", "upd_time", "updated_at")
    _rename("disposition_override", "override_json", "payload_json")

    _rename("app_config_document", "upd_time", "updated_at")
    _rename("app_config_document", "doc_json", "payload_json")

    op.drop_index("ix_item_ord_line_link", table_name="product_ord_line_link")
    _rename("product_ord_line_link", "crt_time", "linked_at")
    _rename("product_ord_line_link", "item_id", "tangbuy_product_id")
    op.create_index("ix_product_link_product", "product_ord_line_link", ["tangbuy_product_id"])

    _rename("product_record", "upd_time", "updated_at")
    _rename("product_record", "item_ext_json", "payload_json")
    _rename("product_record", "ctgy_map_stat", "category_status")
    _rename("product_record", "item_nm", "product_name")
    _rename("product_record", "splr_item_id", "source_product_id")
    _rename("product_record", "item_id", "tangbuy_product_id")
    op.drop_index("ix_item_splr_id", table_name="product_record")
    op.create_index("ix_product_source_id", "product_record", ["source_product_id"])

    op.drop_index("ix_snapshot_queue_stat", table_name="ord_line_snapshot")
    _rename("ord_line_snapshot", "upd_time", "synced_at")
    _rename("ord_line_snapshot", "ord_line_row", "payload_json")
    _rename("ord_line_snapshot", "proc_queue", "queue")
    op.create_index("ix_snapshot_queue_stat", "ord_line_snapshot", ["queue", "ord_line_stat"])

    op.drop_index("ix_audit_ord_line_created", table_name="procurement_audit_log")
    _rename("procurement_audit_log", "crt_time", "created_at")
    _rename("procurement_audit_log", "audit_detail", "detail_json")
    op.create_index(
        "ix_audit_ord_line_created",
        "procurement_audit_log",
        ["ord_line_no", "created_at"],
    )

    _rename("procurement_pipeline_state", "upd_time", "updated_at")
    _rename("procurement_pipeline_state", "pipeline_blockers", "blockers_json")
