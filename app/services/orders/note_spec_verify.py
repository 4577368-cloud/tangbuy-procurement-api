"""备注规格对齐校验：备注期望规格 vs Admin 当前子单规格。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from app.core.config import get_settings

_COLOR_WORDS = (
    "粉",
    "红",
    "黑",
    "白",
    "蓝",
    "绿",
    "黄",
    "紫",
    "灰",
    "米",
    "杏",
    "银",
    "金",
    "橙",
    "棕",
    "咖",
    "卡其",
    "藏青",
    "酒红",
    "乌梅紫",
)


@dataclass
class NoteSpecVerifyResult:
    aligned: bool
    expected_specs: str
    actual_specs: str
    mismatch_summary: str
    confidence: float
    source: str  # llm | rules
    changed: Optional[bool] = None  # vs Admin 打开前快照；None=未提供快照
    spec_before: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_spec_text(text: str) -> str:
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"[\s\u3000]+", "", raw)
    raw = re.sub(r"[：:;；,，/\\|]+", "", raw)
    return raw


def specs_changed(before: str, after: str) -> bool:
    a = normalize_spec_text(before)
    b = normalize_spec_text(after)
    if not a and not b:
        return False
    return a != b


def apply_spec_before_snapshot(
    result: NoteSpecVerifyResult,
    *,
    spec_before: Optional[str],
) -> NoteSpecVerifyResult:
    """对照打开 Admin 前快照，标注是否变更，并强化未改动的错误文案。"""
    before = (spec_before or "").strip()
    if not before:
        return result
    changed = specs_changed(before, result.actual_specs)
    summary = result.mismatch_summary
    if not result.aligned:
        if not changed:
            summary = (
                f"未检测到规格变更（仍为 {result.actual_specs}）。"
                f"备注要求：{result.expected_specs or '见备注'}。"
                "请先在 Admin 改规格后，再点「已修改」。"
            )
        elif before and result.actual_specs:
            summary = (
                f"已刷新：{before} → {result.actual_specs}；"
                f"仍与备注不符（期望 {result.expected_specs}）。{result.mismatch_summary}"
            )
    return NoteSpecVerifyResult(
        aligned=result.aligned,
        expected_specs=result.expected_specs,
        actual_specs=result.actual_specs,
        mismatch_summary=summary,
        confidence=result.confidence,
        source=result.source,
        changed=changed,
        spec_before=before,
    )


def _note_text(row: dict[str, Any]) -> str:
    for key in ("usr_rmk", "ord_rmk", "rmk", "item_rmk"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return str(row.get("note_classify_reason") or "").strip()


def _actual_spec_text(row: dict[str, Any]) -> str:
    for key in ("item_attr_cn", "front_sku_attr_desc", "item_attr", "sku_nm"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return ""


def _extract_colors(text: str) -> list[str]:
    found: list[str] = []
    for c in _COLOR_WORDS:
        if c in text:
            found.append(c if c.endswith("色") or len(c) > 1 else f"{c}色")
    # prefer bare 粉 → 粉色
    normalized: list[str] = []
    for c in found:
        if c in ("粉", "红", "黑", "白", "蓝", "绿", "黄", "紫", "灰", "米", "杏", "银", "金", "橙", "棕", "咖"):
            normalized.append(f"{c}色")
        else:
            normalized.append(c if c.endswith("色") else c)
    return list(dict.fromkeys(normalized))


def _extract_sizes(text: str) -> list[str]:
    sizes: list[str] = []
    for m in re.finditer(
        r"(?:尺码|尺寸|码)[:：\s]*([Xx]{0,3}[SsMmLl]{1,3}|\d{2,3})",
        text,
        re.I,
    ):
        sizes.append(m.group(1).upper())
    for m in re.finditer(r"\b([Xx]{0,3}[SsMmLl]{1,3})\b", text):
        sizes.append(m.group(1).upper())
    return list(dict.fromkeys(sizes))


def verify_note_spec_rules(row: dict[str, Any]) -> NoteSpecVerifyResult:
    note = _note_text(row)
    actual = _actual_spec_text(row)
    expected_colors = _extract_colors(note)
    actual_colors = _extract_colors(actual)
    expected_sizes = _extract_sizes(note)
    actual_sizes = _extract_sizes(actual)

    expected_parts: list[str] = []
    if expected_colors:
        expected_parts.append("/".join(expected_colors))
    if expected_sizes:
        expected_parts.append("尺码 " + "/".join(expected_sizes))
    expected = "、".join(expected_parts) or note or "（备注未解析出明确规格）"

    mismatches: list[str] = []
    if expected_colors:
        if not actual_colors:
            mismatches.append(f"备注要{'/'.join(expected_colors)}，当前规格未识别到颜色（{actual or '空'}）")
        elif not any(c in actual or c.rstrip("色") in actual for c in expected_colors):
            mismatches.append(
                f"备注要{'/'.join(expected_colors)}，当前为{'/'.join(actual_colors) or actual}"
            )
    if expected_sizes:
        if not actual_sizes:
            mismatches.append(f"备注要尺码{'/'.join(expected_sizes)}，当前规格未识别到尺码")
        elif not any(s.upper() in {a.upper() for a in actual_sizes} for s in expected_sizes):
            mismatches.append(
                f"备注要尺码{'/'.join(expected_sizes)}，当前为{'/'.join(actual_sizes) or actual}"
            )

    if not expected_colors and not expected_sizes:
        # 无结构化期望：要求实际规格文本相对备注有可对齐词，否则保守不通过
        if note and actual:
            note_tokens = [t for t in re.split(r"[\s,;；:：]+", note) if len(t) >= 2]
            hit = any(t in actual for t in note_tokens if t not in ("实际", "采购", "需要", "下单", "请"))
            if not hit:
                mismatches.append(f"当前规格「{actual}」与备注「{note}」未对齐")
        elif note and not actual:
            mismatches.append("备注要求改规格，但当前子单无规格字段")

    aligned = len(mismatches) == 0 and bool(note)
    if aligned and (expected_colors or expected_sizes):
        summary = f"已对齐备注：期望 {expected}，当前 {actual}"
    elif aligned:
        summary = "备注与当前规格可对齐"
    else:
        summary = "；".join(mismatches) or "规格与备注不符"

    return NoteSpecVerifyResult(
        aligned=aligned,
        expected_specs=expected,
        actual_specs=actual or "—",
        mismatch_summary=summary,
        confidence=0.75 if expected_colors or expected_sizes else 0.55,
        source="rules",
    )


def _verify_with_llm(row: dict[str, Any]) -> Optional[NoteSpecVerifyResult]:
    settings = get_settings()
    if not settings.llm_configured:
        return None
    note = _note_text(row)
    actual = _actual_spec_text(row)
    if not note:
        return NoteSpecVerifyResult(
            aligned=True,
            expected_specs="",
            actual_specs=actual or "—",
            mismatch_summary="无备注",
            confidence=1.0,
            source="rules",
        )

    from app.services.agent.llm import chat_completion

    try:
        resp = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "你是跨境采购规格核对员。用户下单有备注要求改规格（如颜色/尺码）。"
                        "判断「当前货源规格」是否已按「用户备注」改好。"
                        "只有当前规格满足备注要求才算 aligned=true；"
                        "仍为原规格、或改成备注以外的规格均为 false。"
                        "mismatch_summary 用一句中文说明，例如："
                        "「备注要粉色，当前仍为黑色」或「误改为白色，与备注粉色不符」。"
                        "只输出 JSON："
                        '{"aligned":true|false,"expected_specs":"...","actual_specs":"...",'
                        '"mismatch_summary":"...","confidence":0.0-1.0}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "note": note,
                            "current_spec": actual,
                            "sku_id": row.get("sku_id") or row.get("front_sku_id"),
                            "item_name": row.get("item_nm") or row.get("item_nm_cn"),
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        raw = (resp.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        aligned = bool(data.get("aligned"))
        return NoteSpecVerifyResult(
            aligned=aligned,
            expected_specs=str(data.get("expected_specs") or note),
            actual_specs=str(data.get("actual_specs") or actual or "—"),
            mismatch_summary=str(
                data.get("mismatch_summary")
                or ("已对齐备注" if aligned else "规格与备注不符")
            ),
            confidence=min(0.95, max(0.5, float(data.get("confidence") or 0.8))),
            source="llm",
        )
    except Exception:
        return None


def verify_note_spec_alignment(
    row: dict[str, Any],
    *,
    allow_llm: bool = True,
    spec_before: Optional[str] = None,
) -> NoteSpecVerifyResult:
    if allow_llm:
        llm = _verify_with_llm(row)
        if llm is not None:
            return apply_spec_before_snapshot(llm, spec_before=spec_before)
    return apply_spec_before_snapshot(verify_note_spec_rules(row), spec_before=spec_before)
