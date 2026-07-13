"""商品中心 / 配置中心 / 处置覆盖 仓储。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import AppConfigDocument, DispositionOverride, ProductOrdLineLink, ProductRecord


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _product_item_id(product: dict[str, Any]) -> str:
    return str(product.get("tangbuy_product_id") or product.get("item_id") or "").strip()


def _product_to_dict(row: ProductRecord, links: list[str]) -> dict[str, Any]:
    """DB 宽表列名 → 商品中心 UI 字段（field-catalog productCenterPath）。"""
    payload = dict(row.item_ext_json) if isinstance(row.item_ext_json, dict) else {}
    payload["tangbuy_product_id"] = row.item_id
    if row.splr_item_id:
        payload["source_product_id"] = row.splr_item_id
    if row.item_nm:
        payload.setdefault("product_name", row.item_nm)
    if row.ctgy_map_stat:
        payload.setdefault("category_status", row.ctgy_map_stat)
    payload["linked_ord_lines"] = links
    return payload


class ProductRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _dedupe_products(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for product in items:
            if not isinstance(product, dict):
                continue
            pid = _product_item_id(product)
            if not pid:
                continue
            if pid in by_id:
                prev = by_id[pid]
                lines = list(
                    dict.fromkeys(
                        [str(x).strip() for x in (prev.get("linked_ord_lines") or []) if str(x).strip()]
                        + [str(x).strip() for x in (product.get("linked_ord_lines") or []) if str(x).strip()]
                    )
                )
                by_id[pid] = {**prev, **product, "linked_ord_lines": lines}
            else:
                by_id[pid] = dict(product)
        return list(by_id.values())

    def _links_for(self, item_id: str) -> list[str]:
        rows = self.session.scalars(
            select(ProductOrdLineLink.ord_line_no).where(ProductOrdLineLink.item_id == item_id)
        ).all()
        return [str(r) for r in rows if r]

    def load_all(self) -> list[dict[str, Any]]:
        products = self.session.scalars(select(ProductRecord)).all()
        out: list[dict[str, Any]] = []
        for row in products:
            if row.item_id:
                out.append(_product_to_dict(row, self._links_for(row.item_id)))
        return out

    def count(self) -> int:
        return len(self.session.scalars(select(ProductRecord.item_id)).all())

    def get_by_id(self, tangbuy_id: str) -> Optional[dict[str, Any]]:
        key = tangbuy_id.strip()
        if not key:
            return None
        row = self.session.get(ProductRecord, key)
        if not row:
            return None
        return _product_to_dict(row, self._links_for(key))

    def find_by_source_product_id(self, source_id: str) -> Optional[dict[str, Any]]:
        sid = source_id.strip()
        if not sid:
            return None
        row = self.session.scalar(
            select(ProductRecord).where(ProductRecord.splr_item_id == sid).limit(1)
        )
        if not row:
            return None
        return _product_to_dict(row, self._links_for(row.item_id))

    def find_by_ord_line(self, ord_line_no: str) -> Optional[dict[str, Any]]:
        key = ord_line_no.strip()
        if not key:
            return None
        link = self.session.get(ProductOrdLineLink, key)
        if not link:
            return None
        return self.get_by_id(link.item_id)

    def _upsert_one(self, product: dict[str, Any]) -> None:
        pid = _product_item_id(product)
        if not pid:
            return
        now = _utcnow()
        row = self.session.get(ProductRecord, pid)
        payload = dict(product)
        links = [str(x).strip() for x in (product.get("linked_ord_lines") or []) if str(x).strip()]
        fields = {
            "splr_item_id": str(product.get("source_product_id") or product.get("splr_item_id") or "") or None,
            "item_nm": product.get("product_name") or product.get("item_nm"),
            "ctgy_map_stat": product.get("category_status") or product.get("ctgy_map_stat"),
            "item_ext_json": payload,
            "upd_time": now,
        }
        if row is None:
            self.session.add(ProductRecord(item_id=pid, **fields))
        else:
            for k, v in fields.items():
                setattr(row, k, v)

        self.session.execute(delete(ProductOrdLineLink).where(ProductOrdLineLink.item_id == pid))
        self.session.flush()
        for ord_line in links:
            self.session.merge(
                ProductOrdLineLink(
                    ord_line_no=ord_line,
                    item_id=pid,
                    crt_time=now,
                )
            )

    def save_all(self, items: list[dict[str, Any]]) -> None:
        items = self._dedupe_products(items)
        keep_ids = {_product_item_id(p) for p in items}
        keep_ids.discard("")
        existing_ids = {
            str(x) for x in self.session.scalars(select(ProductRecord.item_id)).all() if x
        }
        removed = existing_ids - keep_ids
        if removed:
            self.session.execute(
                delete(ProductOrdLineLink).where(ProductOrdLineLink.item_id.in_(removed))
            )
            self.session.execute(delete(ProductRecord).where(ProductRecord.item_id.in_(removed)))
        for product in items:
            if isinstance(product, dict):
                self._upsert_one(product)

    def update_product(
        self,
        tangbuy_id: str,
        updater: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        current = self.get_by_id(tangbuy_id)
        if not current:
            return None
        updated = updater(dict(current))
        self._upsert_one(updated)
        return updated


class ConfigRepository:
    CONFIG_KEY = "config_center"
    SYNC_STATE_KEY = "order_sync_state"
    NOTE_CLASSIFY_CACHE_KEY = "note_classify_cache"
    FIND_CACHE_KEY = "product_find_cache"
    BRIEFING_SNAPSHOT_KEY = "command_center_briefing"

    def __init__(self, session: Session) -> None:
        self.session = session

    def load_document(self, doc_key: str) -> Optional[dict[str, Any]]:
        row = self.session.get(AppConfigDocument, doc_key)
        if not row or not isinstance(row.doc_json, dict):
            return None
        return dict(row.doc_json)

    def save_document(self, doc_key: str, payload: dict[str, Any]) -> None:
        now = _utcnow()
        row = self.session.get(AppConfigDocument, doc_key)
        if row is None:
            self.session.add(AppConfigDocument(doc_key=doc_key, doc_json=payload, upd_time=now))
        else:
            row.doc_json = payload
            row.upd_time = now

    def load(self) -> Optional[dict[str, Any]]:
        return self.load_document(self.CONFIG_KEY)

    def save(self, payload: dict[str, Any]) -> None:
        self.save_document(self.CONFIG_KEY, payload)

    def count(self) -> int:
        row = self.session.get(AppConfigDocument, self.CONFIG_KEY)
        return 1 if row else 0


class DispositionOverrideRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_all(self) -> dict[str, dict[str, Any]]:
        rows = self.session.scalars(select(DispositionOverride)).all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = row.override_json if isinstance(row.override_json, dict) else {}
            out[row.ord_line_no] = payload
        return out

    def get(self, ord_line_no: str) -> Optional[dict[str, Any]]:
        key = ord_line_no.strip()
        row = self.session.get(DispositionOverride, key)
        if not row or not isinstance(row.override_json, dict):
            return None
        return dict(row.override_json)

    def merge(self, ord_line_no: str, patch: dict[str, Any]) -> dict[str, Any]:
        key = ord_line_no.strip()
        prev = self.get(key) or {}
        merged = {**prev, **patch}
        now = _utcnow()
        row = self.session.get(DispositionOverride, key)
        if row is None:
            self.session.add(
                DispositionOverride(ord_line_no=key, override_json=merged, upd_time=now)
            )
        else:
            row.override_json = merged
            row.upd_time = now
        return merged

    def count(self) -> int:
        return len(self.session.scalars(select(DispositionOverride.ord_line_no)).all())
