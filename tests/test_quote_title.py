"""报价标题清洗翻译测试。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.products.quote_title import (  # noqa: E402
    clean_quote_title_rules,
    _rules_translate,
)


def test_clean_removes_wholesale_noise():
    raw = "跨境外贸假发非非洲卷发平克小精灵短款头套Human Hair Pixie Wigs 批发爆款"
    out = clean_quote_title_rules(raw)
    assert "批发" not in out
    assert "跨境" not in out
    assert "Human" in out or "Hair" in out or "Pixie" in out


def test_rules_translate_english_title_case():
    raw = "human hair pixie wig short 6 inch"
    out = _rules_translate(raw, "en")
    assert out[0].isupper() or out.startswith("Human")
