"""在线品类共识加票 / soft 晋级测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pending_conventions as pc  # noqa: E402


@pytest.fixture()
def isolated_pending(tmp_path, monkeypatch):
    pending = tmp_path / "pending-conventions.json"
    live = tmp_path / "live-conventions.json"
    soft = tmp_path / "goods-id-soft.json"
    monkeypatch.setattr(pc, "PENDING_FILE", pending)
    monkeypatch.setattr(pc, "LIVE_CONVENTIONS_FILE", live)
    monkeypatch.setattr(pc, "GOODS_SOFT_FILE", soft)
    monkeypatch.setattr(pc, "DATA", tmp_path)
    pc.clear_pending_caches()
    yield tmp_path
    pc.clear_pending_caches()


def test_single_confirm_is_pending_not_soft(isolated_pending):
    r = pc.record_vote(
        title="宠物猫窝四季保暖狗窝宠物垫",
        cid="50001111",
        kind="confirm",
        hs={"category_cn_name": "宠物窝垫", "hs_code": "63079000", "declare_cn_name": "宠物垫"},
        goods_id="81550001",
    )
    assert r["ok"] is True
    data = json.loads((isolated_pending / "pending-conventions.json").read_text(encoding="utf-8"))
    terms = data.get("terms") or {}
    assert terms
    # 任一 term 下该 cid 应为 pending（未达 3）
    statuses = []
    for row in terms.values():
        for meta in (row.get("by_cid") or {}).values():
            if str(meta.get("category_id")) == "50001111" or meta.get("category_id") == 50001111:
                statuses.append(meta.get("status"))
    assert statuses
    assert all(s == "pending" for s in statuses)
    hits = pc.lookup_pending_conventions_for_text("宠物猫窝四季保暖")
    assert hits == []


def test_three_confirms_soft_boost(isolated_pending):
    hs = {"category_cn_name": "宠物窝垫", "hs_code": "63079000", "declare_cn_name": "宠物垫"}
    for i in range(3):
        pc.record_vote(
            title="宠物猫窝四季保暖狗窝宠物垫",
            cid="50001111",
            kind="confirm",
            hs=hs,
            goods_id="81550002",
            at=f"2026-07-14T10:0{i}:00.000Z",
        )
    hits = pc.lookup_pending_conventions_for_text("宠物猫窝四季保暖", ["猫窝"])
    assert hits
    assert any(str(h.get("category_id")) == "50001111" for h in hits)
    assert any(h.get("strength") == "pending_soft" for h in hits)
    soft = pc.lookup_goods_id_soft("81550002")
    assert soft is not None
    assert int(soft.get("support") or 0) >= 3
    assert soft.get("soft_only") is True


def test_correct_away_raises_conflict(isolated_pending):
    hs = {"category_cn_name": "宠物窝垫", "hs_code": "63079000", "declare_cn_name": "宠物垫"}
    for i in range(3):
        pc.record_vote(
            title="宠物猫窝四季保暖",
            cid="50001111",
            kind="confirm",
            hs=hs,
            at=f"2026-07-14T11:0{i}:00.000Z",
        )
    # 纠错拉走：对 orig 记 conflict
    for i in range(2):
        pc.record_vote(
            title="宠物猫窝四季保暖",
            cid="50002222",
            kind="correct",
            original_cid="50001111",
            hs={"category_cn_name": "其它", "hs_code": "99999999", "declare_cn_name": "其它"},
            at=f"2026-07-14T12:0{i}:00.000Z",
        )
    data = json.loads((isolated_pending / "pending-conventions.json").read_text(encoding="utf-8"))
    found_conflict = False
    for row in (data.get("terms") or {}).values():
        meta = (row.get("by_cid") or {}).get("50001111") or {}
        if meta.get("status") == "conflict" or float(meta.get("conflict_rate") or 0) >= 0.2:
            found_conflict = True
    assert found_conflict


def test_five_confirms_can_promote(isolated_pending):
    hs = {"category_cn_name": "宠物窝垫", "hs_code": "63079000", "declare_cn_name": "宠物垫"}
    for i in range(5):
        pc.record_vote(
            title="宠物猫窝四季保暖狗窝宠物垫",
            cid="50001111",
            kind="confirm",
            hs=hs,
            at=f"2026-07-14T13:0{i}:00.000Z",
        )
    live = json.loads((isolated_pending / "live-conventions.json").read_text(encoding="utf-8"))
    dominant = live.get("dominant") or {}
    assert any(
        str((meta or {}).get("category_id")) == "50001111" for meta in dominant.values()
    )
    hits = pc.lookup_pending_conventions_for_text("宠物猫窝保暖垫", ["猫窝"])
    assert any(h.get("strength") == "pending_promoted" for h in hits)


def test_ingest_feedback_confirm(isolated_pending):
    out = pc.ingest_feedback_entry(
        {
            "source_title": "宠物猫窝四季保暖",
            "corrected_category_id": "50001111",
            "original_category_id": "50001111",
            "confirmed": True,
            "corrected_hs": {
                "category_cn_name": "宠物窝垫",
                "hs_code": "63079000",
                "declare_cn_name": "宠物垫",
            },
            "matched_keywords": ["猫窝"],
            "created_at": "2026-07-14T14:00:00.000Z",
        }
    )
    assert out.get("ok") is True
