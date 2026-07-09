"""英文商品标题 → 中文（品类映射前置）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class PreparedTitle:
    original_title: str
    title: str
    hint: Optional[str]
    was_translated: bool


def needs_title_translation(text: str) -> bool:
    """标题以英文/Latin 为主时需先译成中文再跑类目匹配。"""
    text = (text or "").strip()
    if len(text) < 3:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    letters = cjk + latin
    if letters == 0:
        return False
    if cjk >= 3:
        return False
    return latin >= max(6, int(letters * 0.55))


def translate_product_title_en_to_zh(title: str) -> Optional[str]:
    from app.core.config import get_settings
    from app.services.agent.llm import chat_completion

    if not get_settings().llm_configured:
        return None

    raw = (title or "").strip()
    if not raw:
        return None

    try:
        resp = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "你是跨境采购商品标题翻译器。将英文电商标题译为简体中文，用于海关品类识别。"
                        "保留品牌名、型号、规格、数量、材质。只输出一行中文标题，不要解释。"
                    ),
                },
                {"role": "user", "content": raw},
            ]
        )
        zh = (resp.content or "").strip()
        zh = re.sub(r'^["\'\s]+|["\'\s]+$', "", zh)
        zh = zh.split("\n")[0].strip()
        if not zh or len(zh) < 2 or needs_title_translation(zh):
            return None
        return zh
    except Exception:
        return None


def prepare_title_for_mapping(title: str, hint: Optional[str] = None) -> PreparedTitle:
    original = (title or "").strip()
    clean_hint = (hint or "").strip() or None
    if not needs_title_translation(original):
        return PreparedTitle(original, original, clean_hint, False)

    zh = translate_product_title_en_to_zh(original)
    if zh:
        return PreparedTitle(original, zh, clean_hint, True)

    return PreparedTitle(original, original, clean_hint, False)
