"""选品结果解析。"""

from __future__ import annotations

from typing import Any, Optional

PRODUCT_SEARCH_TOOLS = frozenset(
    {"product_text_search", "product_image_search", "product_link_search", "product_compare"}
)


def is_product_search_tool(name: str) -> bool:
    return name in PRODUCT_SEARCH_TOOLS


def parse_product_search_payload(data: Any) -> Optional[dict[str, Any]]:
    if not data or not isinstance(data, dict):
        return None
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(root, dict):
        return None
    raw_list = root.get("similar_products") or root.get("compare_products")
    if not isinstance(raw_list, list):
        return None
    products = [p for p in raw_list if isinstance(p, dict)]
    if not products:
        return None
    st = root.get("search_type") or "text_search"
    return {
        "search_type": st,
        "total_results": root.get("total_results") or len(products),
        "products": products,
    }


def build_product_search_summary(data: Any) -> str:
    payload = parse_product_search_payload(data)
    n = len(payload["products"]) if payload else 0
    if n == 0:
        return "未找到相似商品。可换一张图，或改用关键词搜索。"
    top = payload["products"][0]
    title = str(top.get("title", "相似款"))[:36]
    st = payload.get("search_type")
    verb = {
        "link_search": "链接找同款",
        "compare": "比价",
        "text_search": "搜索",
    }.get(st, "以图搜图")
    return f"{verb}找到 {n} 个商品。首推：{title}。详见下方卡片，价格与链接以卡片为准。"
