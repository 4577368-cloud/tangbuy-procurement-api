"""演示/接库前：写操作失败时按成功返回，避免 UI 卡在提交中。"""

from __future__ import annotations

from typing import Any


def is_demo_submit_always_success() -> bool:
    from app.config.store import get_business_config

    return bool(get_business_config().get("demo_submit_always_success", False))


def demo_submit_stub(**fields: Any) -> dict[str, Any]:
    return {"ok": True, "demo_submit_stub": True, **fields}


def disposition_stub(
    *,
    ord_line_no: str,
    action_key: str,
    stage_after: str | None = None,
) -> dict[str, Any]:
    if stage_after is None:
        stage_after = (
            "pending_payment"
            if action_key in ("manual_confirm", "place_1688_order", "generate_1688_order")
            else "pending_procurement"
        )
    return demo_submit_stub(
        ord_line_no=ord_line_no,
        action_key=action_key,
        stage_before="pending_procurement",
        stage_after=stage_after,
        admin_write="demo_stub",
    )


def flag_release_stub(ord_line_no: str) -> dict[str, Any]:
    return demo_submit_stub(
        ord_line_no=ord_line_no,
        reverted_to="pending_procurement",
        admin_submitted=False,
        message="已退回待下单",
        release={"release_id": f"demo-{ord_line_no}", "review_status": "flagged"},
    )


def acknowledge_release_stub(ord_line_no: str) -> dict[str, Any]:
    return demo_submit_stub(ord_line_no=ord_line_no)


def pre_purchase_stub(ord_line_no: str) -> dict[str, Any]:
    return demo_submit_stub(
        ord_line_no=ord_line_no,
        ord_line_stat_before=23,
        ord_line_stat_after=54,
        admin_write="demo_stub",
        auto_confirmed=False,
        release={"release_id": f"demo-{ord_line_no}", "stage_after": "pending_procurement"},
    )


def place_order_stub(ord_line_nos: list[str]) -> dict[str, Any]:
    return demo_submit_stub(
        ord_line_nos=ord_line_nos,
        ord_line_stat_after=55,
        batches=[
            {
                "store_id": "demo",
                "ord_line_nos": ord_line_nos,
                "admin_write": "demo_stub",
            }
        ],
    )


def evolution_patch_stub(patch_id: str, status: str) -> dict[str, Any]:
    return {"ok": True, "patch": {"id": patch_id, "status": status, "demo_submit_stub": True}}


def skill_audit_stub(invocation_id: str, *, audit_status: str = "ok") -> dict[str, Any]:
    return {
        "invocation": {
            "invocation_id": invocation_id,
            "audit_status": audit_status,
            "demo_submit_stub": True,
        }
    }
