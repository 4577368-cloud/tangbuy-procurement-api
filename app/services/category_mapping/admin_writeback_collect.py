"""子单号收集（Admin 品类回写）。"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


def collect_item_nos(product: dict[str, Any]) -> list[str]:
    """收集可回写 Admin 的子单号（TI / ord_line_no）；TO 主单号会解析为 TI。"""
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        for oid in _expand_ord_line_ref(raw):
            if oid and oid not in seen:
                seen.add(oid)
                out.append(oid)

    for line in product.get("linked_ord_lines") or []:
        _add(line)

    if out:
        return out

    pid = str(product.get("tangbuy_product_id") or product.get("item_id") or "").strip()
    splr = str(product.get("source_product_id") or "").strip()

    try:
        from sqlalchemy import select

        from app.db.session import db_session, is_db_enabled

        if is_db_enabled():
            from app.db.models import OrdLineSnapshot, ProductOrdLineLink

            with db_session() as session:
                if pid:
                    for row in session.scalars(
                        select(ProductOrdLineLink.ord_line_no).where(
                            ProductOrdLineLink.item_id == pid
                        )
                    ).all():
                        _add(row)
                if splr:
                    for row in session.scalars(
                        select(OrdLineSnapshot.ord_line_no).where(
                            OrdLineSnapshot.splr_item_id == splr
                        )
                    ).all():
                        _add(row)
                if pid:
                    for row in session.scalars(
                        select(OrdLineSnapshot.ord_line_no).where(
                            OrdLineSnapshot.item_id == pid
                        )
                    ).all():
                        _add(row)
    except Exception as exc:
        _log.debug("collect_item_nos db fallback failed pid=%s: %s", pid, exc)

    return out


def _expand_ord_line_ref(raw: Any) -> list[str]:
    """Admin changeItemCategory 只认 TI；TO 主单号从快照展开为子单列表。"""
    ref = str(raw or "").strip()
    if not ref:
        return []
    upper = ref.upper()
    if upper.startswith("TI"):
        return [ref]
    if not upper.startswith("TO"):
        return [ref]

    try:
        from sqlalchemy import select

        from app.db.session import db_session, is_db_enabled

        if not is_db_enabled():
            return []
        from app.db.models import OrdLineSnapshot

        with db_session() as session:
            rows = session.scalars(
                select(OrdLineSnapshot.ord_line_no).where(OrdLineSnapshot.ord_no == ref)
            ).all()
        return [str(r).strip() for r in rows if str(r or "").strip()]
    except Exception as exc:
        _log.debug("expand TO->TI failed ref=%s: %s", ref, exc)
        return []
