"""履约作业态 ORM 模型（列名对齐 ads_ops_ord_line_rel_td / field-catalog）。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.types import JSON

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProcurementPipelineState(Base):
    __tablename__ = "procurement_pipeline_state"

    ord_line_no = Column(String(64), primary_key=True)
    pipeline_step = Column(String(32), nullable=False, default="prepare")
    ord_line_stat = Column(Integer, nullable=True)
    pipeline_blockers = Column(JSON, nullable=False, default=list)
    last_error = Column(Text, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class ProcurementBlockerAck(Base):
    __tablename__ = "procurement_blocker_ack"
    __table_args__ = (UniqueConstraint("ord_line_no", "blocker_key", name="uq_blocker_ack"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ord_line_no = Column(String(64), nullable=False, index=True)
    blocker_key = Column(String(64), nullable=False)
    operator = Column(String(128), nullable=True)
    acked_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ProcurementAuditLog(Base):
    __tablename__ = "procurement_audit_log"
    __table_args__ = (Index("ix_audit_ord_line_created", "ord_line_no", "crt_time"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ord_line_no = Column(String(64), nullable=False, index=True)
    ord_no = Column(String(64), nullable=True)
    action_key = Column(String(64), nullable=False, index=True)
    action_label = Column(String(128), nullable=True)
    trigger = Column(String(32), nullable=True)
    operator = Column(String(128), nullable=True)
    admin_write = Column(String(16), nullable=True)
    ord_line_stat_before = Column(Integer, nullable=True)
    ord_line_stat_after = Column(Integer, nullable=True)
    audit_detail = Column(JSON, nullable=True)
    crt_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class OrdLineSnapshot(Base):
    __tablename__ = "ord_line_snapshot"
    __table_args__ = (
        Index("ix_snapshot_ord_no", "ord_no"),
        Index("ix_snapshot_queue_stat", "proc_queue", "ord_line_stat"),
        Index("ix_snapshot_pay_time", "pay_time"),
    )

    ord_line_no = Column(String(64), primary_key=True)
    ord_no = Column(String(64), nullable=False)
    ord_line_stat = Column(Integer, nullable=True)
    pay_time = Column(DateTime(timezone=True), nullable=True)
    item_id = Column(String(64), nullable=True)
    splr_item_id = Column(String(64), nullable=True)
    item_nm = Column(Text, nullable=True)
    proc_queue = Column(String(32), nullable=True)
    admin_fingerprint = Column(String(512), nullable=True)
    ord_line_row = Column(JSON, nullable=False)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SyncCursor(Base):
    __tablename__ = "sync_cursor"

    queue_name = Column(String(64), primary_key=True)
    backfill_page = Column(Integer, nullable=False, default=1)
    backfill_complete = Column(Boolean, nullable=False, default=False)
    last_incremental_at = Column(DateTime(timezone=True), nullable=True)
    last_backfill_at = Column(DateTime(timezone=True), nullable=True)


class ProductRecord(Base):
    __tablename__ = "product_record"
    __table_args__ = (Index("ix_item_splr_id", "splr_item_id"),)

    item_id = Column(String(64), primary_key=True)
    splr_item_id = Column(String(64), nullable=True)
    item_nm = Column(Text, nullable=True)
    ctgy_map_stat = Column(String(32), nullable=True)
    item_ext_json = Column(JSON, nullable=False)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class ProductOrdLineLink(Base):
    __tablename__ = "product_ord_line_link"
    __table_args__ = (Index("ix_item_ord_line_link", "item_id"),)

    ord_line_no = Column(String(64), primary_key=True)
    item_id = Column(String(64), nullable=False, index=True)
    crt_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AppConfigDocument(Base):
    """配置中心单文档存储（business / matrix / userRoles）。"""

    __tablename__ = "app_config_document"

    doc_key = Column(String(64), primary_key=True)
    doc_json = Column(JSON, nullable=False)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class DispositionOverride(Base):
    __tablename__ = "disposition_override"

    ord_line_no = Column(String(64), primary_key=True)
    override_json = Column(JSON, nullable=False)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class AgentTaskRecord(Base):
    __tablename__ = "agent_task_record"
    __table_args__ = (Index("ix_agent_task_updated", "upd_time"),)

    task_id = Column(String(128), primary_key=True)
    task_json = Column(JSON, nullable=False)
    crt_time = Column(DateTime(timezone=True), nullable=True)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class EventLog(Base):
    __tablename__ = "event_log"
    __table_args__ = (
        Index("ix_event_log_stream_created", "stream", "crt_time"),
        Index("ix_event_log_stream_key", "stream", "event_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    stream = Column(String(64), nullable=False)
    event_key = Column(String(128), nullable=True)
    event_json = Column(JSON, nullable=False)
    crt_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CategoryLocalMapping(Base):
    __tablename__ = "category_local_mapping"
    __table_args__ = (
        Index("ix_category_map_item", "item_id"),
        Index("ix_category_map_splr", "splr_item_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String(64), nullable=True)
    splr_item_id = Column(String(64), nullable=True)
    mapping_json = Column(JSON, nullable=False)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class WorkflowRunRecord(Base):
    """采购履约 WorkflowRun（ord_line_no 粒度端到端 trace）。"""

    __tablename__ = "workflow_run_record"
    __table_args__ = (Index("ix_workflow_run_updated", "upd_time"),)

    ord_line_no = Column(String(64), primary_key=True)
    run_json = Column(JSON, nullable=False)
    crt_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    upd_time = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
