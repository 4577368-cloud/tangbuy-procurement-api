"""指挥中心履约简报 LLM 提示词。"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """你是 Tangbuy 跨境采购履约系统的数据分析顾问，面向采购主管班前阅读。

输出要求：
1. 必须按顺序输出四个章节，每章标题单独一行，格式为「## 章节名」（章节名固定为：距上次刷新、今日累计、昨日遗留、重点关注）。正文用「- 」开头的列表项，每章 2–4 条。
2. 只使用用户提供的 JSON 事实数据中的数字，禁止编造；缺失写「数据未采集」。
3. 「负毛利」对应 PAY_AMOUNT_GAP（用户实付不足以覆盖采购、毛利为负，同一口径）；「超时发货」对应 SHIP_OVERDUE。这两项必须在「重点关注」章单独成条；数量>0 时明确提醒。
4. 异常类型名（如零毛利、负毛利、规格不符）直接写汉字即可；关键数字用阿拉伯数字，不必用 Markdown 包裹。
5. 每条 bullet 不超过 2 行；全文 400–600 字；不用问候语。
6. 数字用阿拉伯数字；金额带 ¥。"""


def build_briefing_messages(
    *,
    facts: dict[str, Any],
    delta: dict[str, Any],
) -> list[dict[str, str]]:
    user_payload = {
        "facts": facts,
        "delta": delta,
        "notes": {
            "signal_labels": {
                "PAY_AMOUNT_GAP": "负毛利",
                "ZERO_MARGIN": "零毛利",
                "LOW_MARGIN": "低毛利",
                "SKU_MISMATCH": "规格不符",
                "SHIP_OVERDUE": "超时发货",
                "STOCKOUT": "无货",
                "NOTE_REVIEW": "备注待核",
                "SUGGESTED_PRICE_GAP": "建议价偏高",
                "OTHER": "其他",
            },
            "disposition_labels": {
                "manual_confirm": "人工放行",
                "change_seller": "换供",
                "to_exception": "转异常",
                "request_topup": "发起补款",
            },
            "task_type_labels": {
                "auto_release": "自动下单",
                "category_mapping": "品类映射",
                "order_followup": "催单",
                "inquiry_1688": "1688询盘",
                "sourcing_inquiry": "寻源询盘",
                "newton_agent": "智能咨询",
            },
        },
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请根据以下履约事实生成简报（含距上次刷新的变化 delta）：\n\n"
                + json.dumps(user_payload, ensure_ascii=False, indent=2)
            ),
        },
    ]
