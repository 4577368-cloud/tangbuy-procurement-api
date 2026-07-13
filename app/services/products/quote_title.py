"""报价单商品标题：清洗堆砌词并译为买家语言（英/法）。"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.core.config import get_settings
from app.services.agent.llm import chat_completion

QUOTE_LANG_META: dict[str, dict[str, str]] = {
    "en": {
        "name": "English",
        "structure": (
            "Brand (if present) + Product Type + Key Material/Feature + Size/Color/Spec"
        ),
    },
    "fr": {
        "name": "French",
        "structure": (
            "Marque (si connue) + Type de produit + Matériau/Caractéristique + Taille/Couleur/Spec"
        ),
    },
}

_NOISE_RE = re.compile(
    r"(批发|代发|跨境|外贸|爆款|热销|一件代发|工厂|现货|包邮|直销|新款|潮款|帅气|百搭|时尚|"
    r"精品|特价|促销|厂家|源头|实力|爆款|网红|同款|直播|专供|定制|可定制|"
    r"wholesale|cross[\s-]?border|hot\s?sale|factory|OEM|ODM|dropshipping)",
    re.I,
)

_BRACKET_RE = re.compile(r"[【\[\(（][^】\]\)）]*[】\]\)）]")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def clean_quote_title_rules(title: str) -> str:
    """规则清洗：去营销堆砌、括号促销段、多余空白。"""
    raw = _collapse_ws(title)
    if not raw:
        return ""
    t = _BRACKET_RE.sub(" ", raw)
    t = _NOISE_RE.sub(" ", t)
    t = re.sub(r"[!！]{2,}", " ", t)
    t = _collapse_ws(t)
    # 去掉重复片段（粗略）
    parts = re.split(r"[\s/|+,，、]+", t)
    seen: list[str] = []
    for p in parts:
        p = p.strip()
        if not p or len(p) < 2:
            continue
        key = p.lower()
        if key not in {x.lower() for x in seen}:
            seen.append(p)
    if seen:
        return _collapse_ws(" ".join(seen[:8]))
    return t[:120].strip()


def _title_case_en(text: str) -> str:
    small = {"and", "or", "for", "with", "of", "in", "on", "the", "a", "an", "to"}
    words = text.split()
    out: list[str] = []
    for i, w in enumerate(words):
        core = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", w)
        if not core:
            out.append(w)
            continue
        if i > 0 and core.lower() in small:
            out.append(core.lower())
        else:
            out.append(core[:1].upper() + core[1:])
    return " ".join(out)


def _rules_translate(title: str, target_lang: str) -> str:
    cleaned = clean_quote_title_rules(title)
    if not cleaned:
        return ""
    latin = len(re.findall(r"[A-Za-z]", cleaned))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    if latin >= max(8, cjk * 2):
        if target_lang == "en":
            return _title_case_en(cleaned)[:100]
        return cleaned[:100]
    return cleaned[:100]


def _build_system_prompt(target_lang: str) -> str:
    meta = QUOTE_LANG_META.get(target_lang, QUOTE_LANG_META["en"])
    lang_name = meta["name"]
    structure = meta["structure"]
    return (
        f"You are an international B2B e-commerce copy editor. "
        f"Rewrite raw supplier titles into ONE clean buyer-facing product title in {lang_name}.\n"
        f"Use mainstream Western e-commerce structure: {structure}.\n"
        "Remove: wholesale/cross-border/factory/hot-sale spam, duplicate words, emoji, "
        "supplier shop names, internal sourcing codes.\n"
        "Keep: brand, product type, material, size, color, model/SKU spec when essential.\n"
        "Max ~90 characters. Output ONLY the title line—no quotes, labels, or explanation."
    )


def _parse_titles_json(raw: str, *, expected: int) -> Optional[list[str]]:
    text = (raw or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out: list[str] = []
    for item in data:
        t = _collapse_ws(str(item or ""))
        out.append(t[:120])
    if len(out) != expected:
        return None
    return out


def translate_quote_titles(
    titles: list[str],
    *,
    target_lang: str = "en",
) -> list[str]:
    """批量清洗并翻译报价标题；LLM 不可用时规则降级。"""
    lang = (target_lang or "en").strip().lower()
    if lang not in QUOTE_LANG_META:
        lang = "en"

    inputs = [(t or "").strip() for t in titles]
    if not inputs:
        return []

    settings = get_settings()
    if not settings.llm_configured:
        return [_rules_translate(t, lang) or t for t in inputs]

    payload = [{"index": i, "raw_title": t} for i, t in enumerate(inputs)]
    try:
        resp = chat_completion(
            [
                {"role": "system", "content": _build_system_prompt(lang)},
                {
                    "role": "user",
                    "content": (
                        f"Target language: {QUOTE_LANG_META[lang]['name']}\n"
                        f"Return a JSON array of {len(inputs)} cleaned titles in the same order.\n"
                        f"Input:\n{json.dumps(payload, ensure_ascii=False)}"
                    ),
                },
            ]
        )
        parsed = _parse_titles_json(resp.content or "", expected=len(inputs))
        if parsed:
            return parsed
    except Exception:
        pass

    return [_rules_translate(t, lang) or t for t in inputs]
