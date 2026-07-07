#!/usr/bin/env python3
"""
1688 牛顿 Hub API 桥接 CLI（采购 Demo 内置）

通过 skills-gateway.1688.com 调用牛顿 Skill API。
鉴权复用 1688-sourcing-inquiry 的 AK 存储（workspace/.1688-AK）。

命令：
  text_search   --query "关键词" [--limit 10] [--sort price_asc|price_desc|sold_desc|yx_desc]
  image_search  --image-url "https://..." [--limit 10]
  link_search   --url "1688商品链接" [--image-url "主图URL"] [--limit 10]
  compare       --url "链接" | --image-url "主图" [--query "规格"] [--limit 3]
  order_inquiry_send --order-id "订单ID" --question "催发货问题"
  status        检查 AK 是否已配置

输出 JSON：{"success": bool, "markdown": str, "data": {...}}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

FIND_PRODUCT_API = "/api/find_product/1.0.0"
ORDER_INQUIRY_API = "/api/NewtonOrderBatchInquiry/1.0.0"
GATEWAY_BASE = "https://skills-gateway.1688.com"

# AK 签名依赖寻源技能的 _auth/_const；默认走仓库内 vendor 副本（随代码部署），
# 本地/线上可用 NEWTON_AUTH_SCRIPTS 覆盖。
SOURCING_SCRIPTS = Path(
    os.environ.get(
        "NEWTON_AUTH_SCRIPTS",
        Path(__file__).resolve().parent / "vendor/1688-sourcing-inquiry/scripts",
    )
)

if str(SOURCING_SCRIPTS) not in sys.path and SOURCING_SCRIPTS.exists():
    sys.path.insert(0, str(SOURCING_SCRIPTS))

try:
    import requests  # type: ignore
    from _auth import build_auth_headers  # type: ignore
except ImportError as exc:
    print(
        json.dumps(
            {
                "success": False,
                "markdown": (
                    "❌ 未找到 1688 鉴权模块。请安装 1688-sourcing-inquiry Skill，"
                    "或设置 NEWTON_AUTH_SCRIPTS 指向其 scripts 目录。\n\n"
                    f"详情：{exc}"
                ),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    sys.exit(1)


def _output(success: bool, markdown: str = "", data: dict | None = None) -> None:
    payload: dict[str, Any] = {"success": success}
    if markdown:
        payload["markdown"] = markdown
    if data is not None:
        payload["data"] = data
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _ak_ready() -> bool:
    return bool(build_auth_headers("POST", FIND_PRODUCT_API, "{}"))


def _gateway_post(path: str, body: dict, timeout: int = 30) -> dict:
    body_str = json.dumps(body, ensure_ascii=False)
    headers = build_auth_headers("POST", path, body_str)
    if not headers:
        raise RuntimeError("AK 未配置")

    resp = requests.post(
        f"{GATEWAY_BASE}{path}",
        headers=headers,
        data=body_str.encode("utf-8"),
        timeout=timeout,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("success") is False:
        detail = result.get("msgInfo") or result.get("msgCode") or "未知业务错误"
        raise RuntimeError(str(detail))
    return result


def _unwrap_gateway_model(result: dict) -> Any:
    model = result.get("model")
    if isinstance(model, dict):
        return model
    data = result.get("data")
    if isinstance(data, dict):
        inner = data.get("model")
        if isinstance(inner, dict):
            return inner
        return data
    if isinstance(data, list):
        return data
    return result


def _gateway_find_products(body: dict) -> list[dict]:
    resp = _unwrap_gateway_model(_gateway_post(FIND_PRODUCT_API, body))
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("API 返回格式异常（data 不是列表）")
    return [_parse_product(item) for item in data]


def _parse_product(item: dict) -> dict:
    product_id = str(item.get("itemId", ""))
    detail_url = item.get("detailUrl") or (
        f"https://detail.1688.com/offer/{product_id}.html" if product_id else ""
    )
    return {
        "product_id": product_id,
        "title": item.get("title", ""),
        "image_url": item.get("imageUrl", ""),
        "detail_url": detail_url,
        "price": item.get("currentPrice"),
        "supplier": item.get("company", ""),
        "sold_count": item.get("soldOut", 0),
        "similarity_score": float(item.get("score", 0)),
        "yx_index": item.get("yxIndex"),
    }


def _search_products(body: dict) -> list[dict]:
    return _gateway_find_products(body)


def _format_table(products: list[dict], header: str) -> str:
    if not products:
        return header + "\n\n未找到匹配商品。"

    lines = [header, ""]
    for i, p in enumerate(products, 1):
        price = p.get("price")
        price_text = f"¥{price}" if price not in (None, "") else "价格面议"
        lines.append(f"**{i}. {p.get('title', '（无标题）')}**")
        lines.append(f"- 价格：{price_text} · 销量：{p.get('sold_count', 0)}")
        if p.get("supplier"):
            lines.append(f"- 供应商：{p['supplier']}")
        if p.get("detail_url"):
            lines.append(f"- 链接：{p['detail_url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_request(
    *,
    limit: int,
    sort_type: str | None,
    purchase_amount: int,
    extra: dict,
) -> dict:
    body: dict[str, Any] = {
        "pageSize": limit,
        "purchaseAmount": purchase_amount,
        "scoreLevel": "high",
        "tags": "4306497",
    }
    if sort_type:
        body["sortType"] = sort_type
    body.update(extra)
    return body


def cmd_status() -> None:
    configured = _ak_ready()
    _output(
        configured,
        "✅ 1688 牛顿 AK 已配置" if configured else (
            "❌ 1688 牛顿 AK 未配置。请登录 https://clawhub.1688.com/ 获取 AK，"
            "然后在项目目录执行：\n"
            "`python3 ~/.cursor/skills/1688-sourcing-inquiry/cli.py configure YOUR_AK`"
        ),
        {"configured": configured},
    )


def _skill_result(
    success: bool,
    markdown: str = "",
    data: dict | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"success": success}
    if markdown:
        out["markdown"] = markdown
    if data is not None:
        out["data"] = data
    if error:
        out["error"] = error
    return out


def search_text(
    query: str,
    limit: int = 10,
    sort_type: str | None = None,
    purchase_amount: int = 1,
) -> dict[str, Any]:
    if not _ak_ready():
        return _skill_result(False, markdown="❌ AK 未配置", error="ak_not_configured")
    body = _build_request(
        limit=limit,
        sort_type=sort_type,
        purchase_amount=purchase_amount,
        extra={"query": query},
    )
    products = _search_products(body)
    result = {
        "query": query,
        "search_type": "text_search",
        "total_results": len(products),
        "similar_products": products,
    }
    header = f"✅ 搜索「{query}」找到 {len(products)} 个商品"
    return _skill_result(True, _format_table(products, header), result)


def search_image(
    image_url: str,
    limit: int = 10,
    sort_type: str | None = None,
    purchase_amount: int = 1,
) -> dict[str, Any]:
    if not _ak_ready():
        return _skill_result(False, markdown="❌ AK 未配置", error="ak_not_configured")
    body = _build_request(
        limit=limit,
        sort_type=sort_type,
        purchase_amount=purchase_amount,
        extra={"imageUrl": image_url, "imgBase64": ""},
    )
    products = _search_products(body)
    result = {
        "image_url": image_url,
        "search_type": "image_search",
        "total_results": len(products),
        "similar_products": products,
    }
    header = f"✅ 以图搜图找到 {len(products)} 个相似商品"
    return _skill_result(True, _format_table(products, header), result)


def search_link(
    url: str,
    image_url: str | None = None,
    limit: int = 10,
    sort_type: str | None = None,
    purchase_amount: int = 1,
) -> dict[str, Any]:
    if not _ak_ready():
        return _skill_result(False, markdown="❌ AK 未配置", error="ak_not_configured")
    offer_id, norm_url = _normalize_offer_url(url)
    img = image_url or _fetch_og_image(url) or (norm_url and _fetch_og_image(norm_url))
    if not img:
        return _skill_result(
            False,
            markdown="❌ 未能从链接获取主图。请粘贴商品主图 URL，或改用关键词搜索。",
        )
    body = _build_request(
        limit=limit,
        sort_type=sort_type,
        purchase_amount=purchase_amount,
        extra={"imageUrl": img, "imgBase64": ""},
    )
    products = _search_products(body)
    result = {
        "source_url": norm_url or url,
        "source_image": img,
        "search_type": "link_search",
        "total_results": len(products),
        "similar_products": products,
    }
    header = f"✅ 链接找同款找到 {len(products)} 个商品"
    return _skill_result(True, _format_table(products, header), result)


def compare_products(
    url: str | None = None,
    image_url: str | None = None,
    query: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    if not _ak_ready():
        return _skill_result(False, markdown="❌ AK 未配置", error="ak_not_configured")
    if not url and not image_url:
        return _skill_result(False, markdown="❌ 需要商品链接 url 或主图 image_url")
    img = image_url
    source_url = url
    if url and not img:
        _, norm = _normalize_offer_url(url)
        source_url = norm or url
        img = _fetch_og_image(source_url)
    if not img:
        return _skill_result(False, markdown="❌ 未能获取主图，请提供 image_url")
    body = _build_request(limit=max(limit * 5, 15), extra={"imageUrl": img, "imgBase64": ""})
    if query:
        body["query"] = query
    candidates = _search_products(body)
    selected = _select_compare_products(candidates, limit=limit)
    result = {
        "search_type": "compare",
        "total_candidates": len(candidates),
        "total_compared": len(selected),
        "source_url": source_url,
        "source_image": img,
        "query": query,
        "compare_products": selected,
        "similar_products": selected,
    }
    header = f"✅ 同款比价（共 {len(candidates)} 候选，展示 {len(selected)} 款）"
    return _skill_result(True, _format_compare_table(selected, len(candidates)), result)


def cmd_text_search(args: argparse.Namespace) -> None:
    if not _ak_ready():
        cmd_status()
        return
    r = search_text(args.query, args.limit, args.sort, args.purchase_amount)
    _output(r["success"], r.get("markdown", ""), r.get("data"))


def cmd_image_search(args: argparse.Namespace) -> None:
    if not _ak_ready():
        cmd_status()
        return
    r = search_image(args.image_url, args.limit, args.sort, args.purchase_amount)
    _output(r["success"], r.get("markdown", ""), r.get("data"))


def _extract_offer_id(url: str) -> str | None:
    m = re.search(r"offer/(\d+)", url)
    if m:
        return m.group(1)
    if url.isdigit():
        return url
    return None


def _normalize_offer_url(url: str) -> tuple[str | None, str | None]:
    """返回 (offer_id, 规范化详情页 URL)。"""
    offer_id = _extract_offer_id(url)
    if not offer_id:
        return None, None
    return offer_id, f"https://detail.1688.com/offer/{offer_id}.html"


_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _parse_image_from_html(html: str) -> str | None:
    for pattern in (
        r'property="og:image"\s+content="([^"]+)"',
        r'"offerImageUrl"\s*:\s*"([^"]+)"',
        r'"fullImageURI"\s*:\s*"([^"]+)"',
        r'"imageUrl"\s*:\s*"(https://[^"]+)"',
        r'"(https://cbu0\d\.alicdn\.com/img/ibank/[^"]+)"',
        r'"(https://img\.alicdn\.com/[^"]+)"',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1).replace("\\u002F", "/").replace("\\/", "/")
    return None


def _fetch_html(url: str, user_agent: str, max_bytes: int = 800_000) -> str | None:
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.1688.com/",
            },
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text[:max_bytes]
    except (requests.RequestException, OSError):
        return None
    if "x5secdata" in html[:1000] or "_____tmd_____/punish" in html[:1000]:
        return None
    return html


def _fetch_og_image(url: str) -> str | None:
    """
    从 1688 商品链接解析主图 URL。
    桌面 detail 页常被反爬拦截，优先走 m.1688.com + 移动端 UA，并带有限重试。
    """
    offer_id, _canonical = _normalize_offer_url(url)
    if not offer_id:
        return None

    candidates: list[tuple[str, str]] = [
        (f"https://m.1688.com/offer/{offer_id}.html", _MOBILE_UA),
        (f"https://detail.1688.com/offer/{offer_id}.html", _DESKTOP_UA),
    ]

    import time

    for page_url, ua in candidates:
        for attempt in range(2):
            html = _fetch_html(page_url, ua)
            if html:
                image = _parse_image_from_html(html)
                if image:
                    return image
            if attempt == 0:
                time.sleep(0.4)
    return None


def cmd_link_search(args: argparse.Namespace) -> None:
    if not _ak_ready():
        cmd_status()
        return
    r = search_link(
        args.url,
        args.image_url,
        args.limit,
        args.sort,
        args.purchase_amount,
    )
    _output(r["success"], r.get("markdown", ""), r.get("data"))


def _select_compare_products(products: list[dict], limit: int = 3) -> list[dict]:
    if not products:
        return []

    label_map: dict[str, list[str]] = {}
    ordered: list[dict] = []

    def add(product: dict | None, label: str) -> None:
        if not product:
            return
        pid = product.get("product_id") or id(product)
        key = str(pid)
        if key not in label_map:
            label_map[key] = []
            ordered.append(product)
        if label not in label_map[key]:
            label_map[key].append(label)

    by_sales = sorted(products, key=lambda p: int(p.get("sold_count") or 0), reverse=True)
    add(by_sales[0] if by_sales else None, "销量最高")

    priced = [p for p in products if p.get("price") is not None]
    if priced:
        by_price = sorted(priced, key=lambda p: float(p["price"]))
        add(by_price[0], "价格最低")

    by_yx = sorted(products, key=lambda p: float(p.get("yx_index") or 0), reverse=True)
    add(by_yx[0] if by_yx else None, "综合最优")

    result: list[dict] = []
    for p in ordered[:limit]:
        pid = str(p.get("product_id") or id(p))
        enriched = dict(p)
        enriched["compare_label"] = " · ".join(label_map.get(pid, []))
        result.append(enriched)
    return result


def _format_compare_table(products: list[dict], total_candidates: int) -> str:
    if not products:
        return "未找到可比价的同款商品"
    lines = [
        f"✅ 从 {total_candidates} 个同款中选出 {len(products)} 款对比",
        "",
    ]
    for i, p in enumerate(products, 1):
        price = p.get("price")
        price_text = f"¥{price}" if price not in (None, "") else "面议"
        label = p.get("compare_label") or ""
        lines.append(f"**{i}. {p.get('title', '（无标题）')}**")
        if label:
            lines.append(f"- 维度：{label}")
        lines.append(f"- 价格：{price_text} · 销量：{p.get('sold_count', 0)}")
        if p.get("detail_url"):
            lines.append(f"- 链接：{p['detail_url']}")
        lines.append("")
    return "\n".join(lines).strip()


def cmd_compare(args: argparse.Namespace) -> None:
    if not _ak_ready():
        cmd_status()
        return
    r = compare_products(args.url, args.image_url, args.query, args.limit)
    _output(r["success"], r.get("markdown", ""), r.get("data"))


def send_order_inquiry(order_ids: list[str], question: str) -> dict[str, Any]:
    """B 层网关直发订单询盘。返回 {success, markdown, data?, error?}。"""
    if not _ak_ready():
        return {"success": False, "error": "AK 未配置", "markdown": "❌ AK 未配置"}
    if not order_ids:
        return {"success": False, "error": "需要订单 ID", "markdown": "❌ 需要订单 ID"}
    if len(order_ids) > 10:
        return {"success": False, "error": "单次最多 10 个订单", "markdown": "❌ 单次最多 10 个订单 ID"}

    body = {
        "orderIds": order_ids,
        "question": question.strip(),
        "appKey": "newton_api_order_inquiry",
        "imageList": [],
        "taskId": str(uuid.uuid4()),
    }
    try:
        raw = _gateway_post(ORDER_INQUIRY_API, body, timeout=100)
    except Exception as exc:
        return {"success": False, "error": str(exc), "markdown": f"❌ 订单询盘失败：{exc}"}

    model = _unwrap_gateway_model(raw)
    if isinstance(model, dict):
        nested = model.get("model")
        if isinstance(nested, dict):
            model = nested
        suc = bool(model.get("suc", False))
        error_msg = model.get("errorMsg", "") or ""
    else:
        suc = False
        error_msg = "返回结构异常"

    if not suc:
        msg = f"❌ 订单询盘触发失败：{error_msg or '未知错误'}"
        return {"success": False, "error": error_msg or "未知错误", "markdown": msg, "data": {"order_ids": order_ids}}

    ids_text = "、".join(order_ids)
    markdown = (
        f"✅ 已向商家发起订单询盘\n\n"
        f"- **订单 ID**：{ids_text}\n"
        f"- **问题**：{question.strip()}\n\n"
        "商家回复将由 1688 平台异步处理，请稍后关注订单状态或旺旺消息。"
    )
    return {
        "success": True,
        "markdown": markdown,
        "data": {"order_ids": order_ids, "question": question.strip(), "search_type": "order_inquiry"},
    }


def cmd_order_inquiry_send(args: argparse.Namespace) -> None:
    if not _ak_ready():
        cmd_status()
        return

    order_ids = [x.strip() for x in re.split(r"[,，\s]+", args.order_id) if x.strip()]
    result = send_order_inquiry(order_ids, args.question)
    _output(result.get("success", False), result.get("markdown", ""), result.get("data"))


def main() -> None:
    parser = argparse.ArgumentParser(description="1688 牛顿 Hub API 桥接")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="检查 AK 配置")

    p_text = sub.add_parser("text_search", help="关键词搜索商品")
    p_text.add_argument("--query", "-q", required=True)
    p_text.add_argument("--limit", "-l", type=int, default=10)
    p_text.add_argument(
        "--sort",
        "-s",
        choices=["price_asc", "price_desc", "sold_desc", "yx_desc"],
        default=None,
    )
    p_text.add_argument("--purchase-amount", type=int, default=1)

    p_image = sub.add_parser("image_search", help="图片 URL 以图搜图")
    p_image.add_argument("--image-url", required=True)
    p_image.add_argument("--limit", "-l", type=int, default=10)
    p_image.add_argument(
        "--sort",
        "-s",
        choices=["price_asc", "price_desc", "sold_desc", "yx_desc"],
        default=None,
    )
    p_image.add_argument("--purchase-amount", type=int, default=1)

    p_link = sub.add_parser("link_search", help="1688 链接找同款")
    p_link.add_argument("--url", "-u", required=True)
    p_link.add_argument("--image-url", default=None)
    p_link.add_argument("--limit", "-l", type=int, default=10)
    p_link.add_argument(
        "--sort",
        "-s",
        choices=["price_asc", "price_desc", "sold_desc", "yx_desc"],
        default=None,
    )
    p_link.add_argument("--purchase-amount", type=int, default=1)

    p_compare = sub.add_parser("compare", help="同款比价")
    p_compare.add_argument("--url", "-u", default=None)
    p_compare.add_argument("--image-url", default=None)
    p_compare.add_argument("--query", "-q", default=None)
    p_compare.add_argument("--limit", "-l", type=int, default=3)
    p_compare.add_argument(
        "--sort",
        "-s",
        choices=["price_asc", "price_desc", "sold_desc", "yx_desc"],
        default=None,
    )
    p_compare.add_argument("--purchase-amount", type=int, default=1)

    p_order = sub.add_parser("order_inquiry_send", help="订单询盘（催发货/改价）")
    p_order.add_argument("--order-id", "-o", required=True)
    p_order.add_argument("--question", "-q", required=True)

    args = parser.parse_args()

    try:
        if args.command == "status":
            cmd_status()
        elif args.command == "text_search":
            cmd_text_search(args)
        elif args.command == "image_search":
            cmd_image_search(args)
        elif args.command == "link_search":
            cmd_link_search(args)
        elif args.command == "compare":
            cmd_compare(args)
        elif args.command == "order_inquiry_send":
            cmd_order_inquiry_send(args)
    except Exception as exc:
        _output(False, f"❌ 牛顿 API 调用失败：{exc}")


if __name__ == "__main__":
    main()
