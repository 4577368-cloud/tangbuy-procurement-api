"""补款通知文案翻译（LLM）。"""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_settings
from app.services.agent.llm import chat_completion

_TOPUP_SYSTEM_TAIL = (
    "The Chinese draft is the sole source of content—translate its meaning faithfully. "
    "Do NOT add order numbers, amounts, reasons, or other facts that are not already in the draft. "
    "Order context is reference only when the draft mentions related details. "
    "Use a clear, warm, professional tone. "
    "Output ONLY the buyer-facing message body—no titles, labels, or meta commentary."
)

TOPUP_LANG_META: dict[str, dict[str, str]] = {
    "en": {
        "name": "English",
        "system": (
            "You are a senior customer success specialist at Tangbuy, a cross-border procurement platform. "
            "Translate the procurement team's Chinese draft into English for the buyer. "
            + _TOPUP_SYSTEM_TAIL
        ),
    },
    "fr": {
        "name": "French",
        "system": (
            "Vous êtes chargé(e) de la relation client chez Tangbuy, plateforme d'achat transfrontalier. "
            "Traduisez le brouillon chinois de l'équipe achats en français pour l'acheteur. "
            "Le brouillon chinois est la seule source de contenu—traduisez fidèlement son sens. "
            "N'ajoutez PAS de numéros de commande, montants, motifs ou autres faits absents du brouillon. "
            "Le contexte commande sert uniquement de référence si le brouillon y fait allusion. "
            "Ton clair, chaleureux et professionnel. "
            "Répondez UNIQUEMENT avec le corps du message destiné à l'acheteur, sans titre ni commentaire méta."
        ),
    },
    "es": {
        "name": "Spanish",
        "system": (
            "Eres especialista en atención al cliente de Tangbuy, plataforma de compras transfronterizas. "
            "Traduce el borrador chino del equipo de compras al español para el comprador. "
            "El borrador chino es la única fuente de contenido—traduce fielmente su significado. "
            "NO añadas números de pedido, importes, motivos u otros datos que no estén en el borrador. "
            "El contexto del pedido es solo referencia si el borrador lo menciona. "
            "Tono claro, cordial y profesional. "
            "Devuelve SOLO el cuerpo del mensaje para el comprador, sin títulos ni comentarios meta."
        ),
    },
    "ar": {
        "name": "Arabic",
        "system": (
            "أنت متخصص نجاح العملاء في Tangbuy، منصة مشتريات عبر الحدود. "
            "ترجم المسودة الصينية لفريق المشتريات إلى العربية للمشتري. "
            "المسودة الصينية هي المصدر الوحيد للمحتوى—ترجم معناها بإخلاص. "
            "لا تُضف أرقام طلبات أو مبالغ أو أسبابًا أو حقائق غير موجودة في المسودة. "
            "سياق الطلب مرجع فقط عندما تذكره المسودة. "
            "نبرة واضحة ودافئة ومهنية. "
            "أخرج نص الرسالة الموجه للمشتري فقط، دون عناوين أو تعليقات وصفية."
        ),
    },
    "de": {
        "name": "German",
        "system": (
            "Sie sind Kundenservice-Spezialist bei Tangbuy, einer grenzüberschreitenden Beschaffungsplattform. "
            "Übersetzen Sie den chinesischen Entwurf des Einkaufsteams ins Deutsche für den Käufer. "
            "Der chinesische Entwurf ist die einzige Inhaltsquelle—übersetzen Sie die Bedeutung treu. "
            "Fügen Sie KEINE Bestellnummern, Beträge, Gründe oder andere Fakten hinzu, die nicht im Entwurf stehen. "
            "Der Bestellkontext dient nur als Referenz, wenn der Entwurf darauf Bezug nimmt. "
            "Klarer, freundlicher und professioneller Ton. "
            "Geben Sie NUR den Nachrichtentext für den Käufer aus—ohne Überschriften oder Meta-Kommentare."
        ),
    },
    "ja": {
        "name": "Japanese",
        "system": (
            "あなたは越境調達プラットフォーム Tangbuy のカスタマーサクセス担当です。"
            "調達チームの中国語下書きを購入者向けに日本語へ翻訳してください。"
            "中国語下書きが唯一の内容源です—意味を忠実に訳してください。"
            "下書きにない注文番号・金額・理由などの事実は追加しないでください。"
            "注文コンテキストは、下書きが関連内容に言及している場合のみ参考にします。"
            "明確で丁寧かつプロフェッショナルな文体。"
            "購入者向け本文のみを出力し、見出しやメタコメントは含めないでください。"
        ),
    },
}


def _build_user_prompt(
    *,
    source_text: str,
    reason_label: str,
    order_context: dict[str, Any],
    target_lang: str,
) -> str:
    lang_name = TOPUP_LANG_META.get(target_lang, {}).get("name", target_lang)
    ctx_lines = [
        f"TI (line): {order_context.get('ord_line_no') or '—'}",
        f"TO (order): {order_context.get('ord_no') or '—'}",
        f"Payment bill: {order_context.get('pay_no') or '—'}",
        f"Top-up amount (CNY): {order_context.get('topup_amount')}",
        f"Customer paid (CNY): {order_context.get('customer_paid')}",
        f"Purchase payable (CNY): {order_context.get('purchase_payable')}",
    ]
    return (
        f"Target language: {lang_name}\n"
        f"Top-up reason (reference only): {reason_label}\n"
        f"Order context (reference only—do not add to message unless draft mentions it):\n"
        + "\n".join(f"- {line}" for line in ctx_lines)
        + "\n\n"
        f"Procurement draft (Chinese)—translate this faithfully:\n{source_text.strip()}"
    )


def translate_topup_message(
    *,
    source_text: str,
    reason_label: str,
    target_lang: str,
    order_context: Optional[dict[str, Any]] = None,
) -> str:
    settings = get_settings()
    if not settings.llm_configured:
        raise RuntimeError("LLM 未配置，无法翻译")

    raw = (source_text or "").strip()
    if not raw:
        raise ValueError("请先填写给用户的中文说明")

    lang = (target_lang or "en").strip().lower()
    meta = TOPUP_LANG_META.get(lang)
    if not meta:
        raise ValueError(f"不支持的语言: {target_lang}")

    resp = chat_completion(
        [
            {"role": "system", "content": meta["system"]},
            {
                "role": "user",
                "content": _build_user_prompt(
                    source_text=raw,
                    reason_label=reason_label or "—",
                    order_context=order_context or {},
                    target_lang=lang,
                ),
            },
        ]
    )
    translated = (resp.content or "").strip()
    if not translated:
        raise RuntimeError("翻译结果为空，请重试")
    return translated
