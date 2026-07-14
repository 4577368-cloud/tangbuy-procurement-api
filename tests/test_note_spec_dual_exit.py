"""备注规格双出口校验与处置测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.orders.note_spec_verify import verify_note_spec_alignment, verify_note_spec_rules
from app.services.orders.disposition import DispositionError, submit_disposition


def test_rules_aligned_when_pink_in_actual():
    row = {
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:粉色",
    }
    r = verify_note_spec_rules(row)
    assert r.aligned is True
    assert "粉" in r.expected_specs


def test_rules_fail_when_still_black():
    row = {
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:黑色",
    }
    r = verify_note_spec_rules(row)
    assert r.aligned is False
    assert "粉色" in r.mismatch_summary
    assert "黑" in r.mismatch_summary


def test_unchanged_snapshot_makes_clear_error():
    from app.services.orders.note_spec_verify import apply_spec_before_snapshot

    row = {
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:黑色",
    }
    base = verify_note_spec_rules(row)
    out = apply_spec_before_snapshot(base, spec_before="颜色:黑色")
    assert out.changed is False
    assert "未检测到规格变更" in out.mismatch_summary
    assert "粉" in out.mismatch_summary


def test_changed_but_still_wrong():
    from app.services.orders.note_spec_verify import apply_spec_before_snapshot

    row = {
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:白色",
    }
    base = verify_note_spec_rules(row)
    out = apply_spec_before_snapshot(base, spec_before="颜色:黑色")
    assert out.changed is True
    assert "→" in out.mismatch_summary or "白色" in out.mismatch_summary


def test_rules_fail_when_wrong_color_white():
    row = {
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:白色",
    }
    r = verify_note_spec_rules(row)
    assert r.aligned is False
    assert "白" in r.mismatch_summary or "白色" in r.mismatch_summary


def test_verify_prefers_llm_when_available():
    row = {"usr_rmk": "实际采购粉色", "item_attr_cn": "颜色:黑色"}
    fake = type("R", (), {"content": '{"aligned":false,"expected_specs":"粉色","actual_specs":"黑色","mismatch_summary":"备注要粉色，当前仍为黑色","confidence":0.9}'})()
    with (
        patch("app.core.config.get_settings") as gs,
        patch("app.services.agent.llm.chat_completion", return_value=fake),
    ):
        gs.return_value.llm_configured = True
        r = verify_note_spec_alignment(row, allow_llm=True)
    assert r.source == "llm"
    assert r.aligned is False
    assert "粉色" in r.mismatch_summary


def test_note_defer_1688_acks_and_resumes():
    row = {
        "ord_line_no": "TI_NOTE_1",
        "ord_no": "TO_NOTE_1",
        "ord_line_stat": 23,
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:黑色",
    }
    with (
        patch("app.services.orders.disposition.get_ord_line", return_value=row),
        patch("app.services.orders.pipeline_store.ack_blocker") as ack,
        patch(
            "app.services.orders.procurement_pipeline.resume_pipeline",
            return_value={"ok": True, "state": {"pipeline_step": "prepare", "ord_line_stat": 23, "blockers": []}},
        ),
        patch("app.services.orders.disposition_store.append_audit"),
    ):
        result = submit_disposition(
            ord_line_no="TI_NOTE_1",
            action_key="note_defer_1688",
            action_label="后续 1688 改规格",
            operator="tester",
        )
    assert result["ok"] is True
    assert result["action_key"] == "note_defer_1688"
    assert any(c.args[1] == "NOTE_HANDLED" for c in ack.call_args_list)


def test_note_spec_modified_blocks_when_mismatch():
    row = {
        "ord_line_no": "TI_NOTE_2",
        "ord_no": "TO_NOTE_2",
        "ord_line_stat": 23,
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:黑色",
    }
    with (
        patch("app.services.orders.disposition.get_ord_line", return_value=row),
        patch("app.services.orders.order_line_sync.refresh_ord_lines"),
        patch(
            "app.services.orders.note_spec_verify.verify_note_spec_alignment",
            return_value=verify_note_spec_rules(row),
        ),
        patch("app.services.orders.pipeline_store.save_pipeline_state", return_value={}),
        patch("app.services.orders.disposition_store.append_audit"),
    ):
        with pytest.raises(DispositionError) as exc:
            submit_disposition(
                ord_line_no="TI_NOTE_2",
                action_key="note_spec_modified",
                action_label="已修改",
            )
    assert exc.value.code == "note_spec_mismatch"
    assert "粉" in str(exc.value)


def test_note_spec_modified_resumes_when_aligned():
    row = {
        "ord_line_no": "TI_NOTE_3",
        "ord_no": "TO_NOTE_3",
        "ord_line_stat": 23,
        "usr_rmk": "实际采购粉色",
        "item_attr_cn": "颜色:粉色",
    }
    with (
        patch("app.services.orders.disposition.get_ord_line", return_value=row),
        patch("app.services.orders.order_line_sync.refresh_ord_lines"),
        patch(
            "app.services.orders.note_spec_verify.verify_note_spec_alignment",
            return_value=verify_note_spec_rules(row),
        ),
        patch("app.services.orders.pipeline_store.ack_blocker"),
        patch(
            "app.services.orders.procurement_pipeline.resume_pipeline",
            return_value={"ok": True, "state": {"pipeline_step": "prepare", "blockers": []}},
        ),
        patch("app.services.orders.disposition_store.append_audit"),
    ):
        result = submit_disposition(
            ord_line_no="TI_NOTE_3",
            action_key="note_spec_modified",
            action_label="已修改",
        )
    assert result["ok"] is True
    assert result.get("verify", {}).get("aligned") is True
