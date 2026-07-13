"""流水线卡点文案：Admin/1688 原始错误 → 采购员可读摘要。"""

from __future__ import annotations

import re


def _strip_admin_noise(raw: str) -> str:
    s = re.sub(r"失败\s*\d+\s*个\s*1688\s*订单[:：]\s*", "", raw, flags=re.I)
    s = re.sub(r"\[?错误信息[：:]", "", s, flags=re.I)
    s = re.sub(r"PO=[^;]+;\s*", "", s, flags=re.I)
    s = re.sub(r"PI=[^;]+;\s*", "", s, flags=re.I)
    s = re.sub(r"TOs?=[^;\]]+[;\]]?\s*", "", s, flags=re.I)
    s = re.sub(r"TIs?=[^;\]]+[;\]]?\s*", "", s, flags=re.I)
    s = re.sub(r"skuid?=\s*\d+\s*;?\s*", "", s, flags=re.I)
    s = re.sub(r"请确认[！!]?\s*", "", s)
    s = re.sub(r"成功[：:]\s*\[\s*\]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 96:
        s = s[:93] + "…"
    return s


def summarize_sku_admin_error(msg: str) -> str:
    need = re.search(r"需要[^;：:]*[:：]?\s*([^;]+)", msg)
    queried = re.search(r"1688查询的是\[([^\]]+)\]", msg)
    parts: list[str] = []
    if need:
        parts.append(f"下单规格 {need.group(1).strip()}")
    if queried:
        parts.append(f"1688 规格 {queried.group(1).strip()}")
    if parts:
        return "，".join(parts)
    core = re.search(r"sku[^;。]*不匹配[^;。]*", msg, flags=re.I)
    if core:
        cleaned = _strip_admin_noise(core.group(0))
        if cleaned:
            return cleaned
    cleaned = _strip_admin_noise(msg)
    return cleaned or "规格与 1688 不一致，请核对后放行"


def summarize_admin_blocker_detail(key: str, label: str, msg: str) -> str:
    if key == "ADMIN_SKU" or label == "规格不符":
        return summarize_sku_admin_error(msg)
    if key == "ADMIN_STOCK" or label == "库存不足":
        return "货源库存不足，请换供或联系卖家"
    if key == "ADMIN_MOQ" or label == "起批量不满足":
        return "未满足卖家起批量"
    if key == "ADMIN_MARGIN" or "毛利" in label:
        cleaned = _strip_admin_noise(msg)
        if re.search(r"毛利|运费|差价|金额", cleaned):
            return cleaned
        return "价格或毛利异常，请核对后放行"
    cleaned = _strip_admin_noise(msg)
    return cleaned or msg
