"""工具执行（牛顿网关 + 催单 + 寻源 + 品类映射）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from app.core.paths import PROJECT_ROOT
from app.integrations.newton.tasks import task_create
from app.integrations.skill_cli import (
    run_inquiry_query,
    run_inquiry_submit,
    run_procurement_inquiry,
    run_supplychain_inquiry,
)
from app.services.agent.data_query import execute_order_query, execute_procurement_stats
from app.services.agent.followup import (
    execute_order_followup_send,
    resolve_followup_order_id,
    resolve_followup_question,
)
from app.services.agent.routing import looks_like_merchant_inquiry
from app.services.products.find_cache import (
    extract_products_from_tool_result,
    upsert_find_cache_items,
)
from app.services.products.store import confirm_product_mapping, find_by_source_product_id, finalize_product_mapping_confirm, update_product
from app.services.tasks.supplychain import create_supplychain_inquiry_task, parse_supplychain_query

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import newton_cli  # noqa: E402


def _int_arg(val: Optional[str], default: int) -> int:
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _cache_product_find_result(tool_name: str, args: dict[str, str], result: dict[str, Any]) -> None:
    if not result.get("success"):
        return
    products = extract_products_from_tool_result(result)
    if not products:
        return
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    upsert_find_cache_items(
        products,
        tool=tool_name,
        query=(args.get("query") or data.get("query") or "").strip() or None,
        image_url=(args.get("image_url") or data.get("image_url") or "").strip() or None,
        source_url=(args.get("url") or data.get("source_url") or "").strip() or None,
        search_type=str(data.get("search_type") or "").strip() or None,
    )


def _run_product_find(tool_name: str, args: dict[str, str], runner) -> dict[str, Any]:
    result = runner()
    try:
        _cache_product_find_result(tool_name, args, result)
    except Exception:
        pass
    return result


def _build_suggest_markdown(result: dict[str, Any]) -> str:
    if not result.get("success") or not result.get("category_id"):
        return f"❌ 品类映射失败：{result.get('error') or '未知错误'}"
    lines = [
        "✅ 品类映射建议",
        "",
        f"- **决策**: {result.get('decision', 'manual_suggested')}",
        f"- **分类中文名**: {result.get('category_cn_name', '')}",
        f"- **分类英文名**: {result.get('category_en_name', '')}",
        f"- **分类编号**: {result.get('category_id', '')}",
        f"- **海关编码**: {result.get('hs_code', '')}",
        f"- **中文描述**: {result.get('declare_cn_name', '')}",
        f"- **英文描述**: {result.get('declare_en_name', '')}",
    ]
    keywords = result.get("matched_keywords")
    if keywords:
        lines.append(f"- **命中词**: {'、'.join(keywords)}")
    if result.get("vision_summary"):
        lines.append(f"- **图片理解**: {result['vision_summary']}")
    if result.get("match_detail"):
        lines.append(f"- **说明**: {result['match_detail']}")
    return "\n".join(lines)


def _suggest_result_from_product(product: dict[str, Any]) -> dict[str, Any]:
    hs = product.get("hs_mapping") if isinstance(product.get("hs_mapping"), dict) else {}
    rec = product.get("mapping_record") if isinstance(product.get("mapping_record"), dict) else {}
    status = str(product.get("category_status") or "")
    ok = status in ("auto_passed", "confirmed", "needs_review", "mapping")
    return {
        "success": ok and bool(hs.get("category_id") or rec.get("category_id")),
        "decision": rec.get("decision", ""),
        "category_id": hs.get("category_id") or rec.get("category_id") or 0,
        "category_cn_name": hs.get("category_cn_name") or rec.get("category_cn_name", ""),
        "category_en_name": hs.get("category_en_name") or rec.get("category_en_name", ""),
        "hs_code": hs.get("hs_code") or rec.get("hs_code", ""),
        "declare_cn_name": hs.get("declare_cn_name") or rec.get("declare_cn_name", ""),
        "declare_en_name": hs.get("declare_en_name") or rec.get("declare_en_name", ""),
        "tariff": hs.get("tariff") or rec.get("tariff"),
        "matched_keywords": rec.get("matched_keywords"),
        "vision_summary": rec.get("vision_summary"),
        "match_detail": rec.get("match_detail"),
        "error": None if ok else "映射未完成",
    }


def execute_tool(
    tool_name: str,
    args: dict[str, str],
    order_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if tool_name == "product_text_search":
        q = (args.get("query") or "").strip()
        if not q:
            return {"success": False, "markdown": "❌ 需要搜索关键词 query"}
        return _run_product_find(
            tool_name,
            args,
            lambda: newton_cli.search_text(q, _int_arg(args.get("limit"), 10), args.get("sort")),
        )

    if tool_name == "product_image_search":
        url = (args.get("image_url") or "").strip()
        if not url:
            return {"success": False, "markdown": "❌ 需要商品主图 URL image_url"}
        return _run_product_find(
            tool_name,
            args,
            lambda: newton_cli.search_image(url, _int_arg(args.get("limit"), 10), args.get("sort")),
        )

    if tool_name == "product_link_search":
        url = (args.get("url") or "").strip()
        if not url:
            return {"success": False, "markdown": "❌ 需要 1688 商品链接 url"}
        return _run_product_find(
            tool_name,
            args,
            lambda: newton_cli.search_link(
                url,
                (args.get("image_url") or "").strip() or None,
                _int_arg(args.get("limit"), 10),
                args.get("sort"),
            ),
        )

    if tool_name == "product_compare":
        return _run_product_find(
            tool_name,
            args,
            lambda: newton_cli.compare_products(
                (args.get("url") or "").strip() or None,
                (args.get("image_url") or "").strip() or None,
                (args.get("query") or "").strip() or None,
                _int_arg(args.get("limit"), 3),
            ),
        )

    if tool_name == "procurement_inquiry":
        offer = (args.get("offerName") or "").strip()
        count = (args.get("count") or "").strip()
        demand = (args.get("demand") or "").strip()
        if not offer or not count or not demand:
            return {"success": False, "markdown": "❌ 参数不完整：需要 offerName、count、demand"}
        return run_procurement_inquiry(offer, count, demand)

    if tool_name == "supplychain_inquiry_start":
        requirement = (args.get("requirement") or "").strip()
        questions_raw = (args.get("questions") or "").strip()
        if not requirement:
            return {"success": False, "markdown": "❌ 需要找品需求 requirement"}
        if not questions_raw:
            return {"success": False, "markdown": "❌ 需要询盘问题列表 questions（JSON 数组格式）"}
        try:
            parsed_questions = json.loads(questions_raw)
            if not isinstance(parsed_questions, list):
                raise ValueError("questions 必须是 JSON 数组")
        except (json.JSONDecodeError, ValueError):
            return {
                "success": False,
                "markdown": '❌ questions 必须是 JSON 数组格式，如：[{"question":"是否支持定制？"}]',
            }
        questions = [
            {
                "question": (q.get("question") or "").strip(),
                "type": (q.get("type") or "current").strip(),
            }
            for q in parsed_questions
            if isinstance(q, dict)
        ]
        image_urls = None
        if args.get("image_urls", "").strip():
            image_urls = [u.strip() for u in args["image_urls"].split(",") if u.strip()]
        outcome = create_supplychain_inquiry_task(
            requirement,
            questions,
            purchase_size=_int_arg(args.get("purchase_size"), 1),
            inquiry_item_size=_int_arg(args.get("inquiry_item_size"), 3),
            recall_item_size=_int_arg(args.get("recall_item_size"), 10),
            image_urls=image_urls,
        )
        task = outcome.get("task")
        if not task:
            return {
                "success": False,
                "error": outcome.get("error"),
                "markdown": f"❌ 发起供应链询盘失败：{outcome.get('error')}",
            }
        if outcome.get("via") == "newton":
            return {
                "success": True,
                "taskId": task["id"],
                "data": {"task_id": task["id"], "via": "newton"},
                "markdown": (
                    f"✅ 已发起供应链询盘。\n任务：**{task['title']}**。\n"
                    "结果约数分钟后在任务中心查看。"
                ),
            }
        instance_id = (task.get("payload") or {}).get("instance_id", "")
        return {
            "success": True,
            "taskId": task["id"],
            "data": {"instance_id": instance_id, "task_id": task["id"]},
            "markdown": (
                f"✅ 已发起供应链询盘（实例 {instance_id[:12]}…）。\n"
                f"任务已创建：**{task['title']}**。\n"
                "结果约 5 分钟后可查询，请在任务中心查看进度。"
            ),
        }

    if tool_name == "supplychain_inquiry_query":
        instance_id = (args.get("instance_id") or "").strip()
        if not instance_id:
            return {"success": False, "markdown": "❌ 需要 instance_id"}
        result = run_supplychain_inquiry(
            ["--instance-id", instance_id, "--output-mode", "stdout"]
        )
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error"),
                "markdown": f"❌ 查询失败：{result.get('error')}",
            }
        parsed = parse_supplychain_query(instance_id, result.get("data"))
        if not parsed:
            return {"success": False, "markdown": "❌ 查询结果解析失败"}
        snapshot = parsed["snapshot"]
        lines = []
        for item in snapshot.get("inquired_items", []):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("itemId") or "商品"
            company = item.get("company") or "未知供应商"
            summary = item.get("inquirySummary") or item.get("inquiry_summary") or "无摘要"
            lines.append(f"- **{title}**（{company}）：{summary}")
        body = "\n".join(lines) if lines else "暂无商家回复，请稍后重试。"
        return {
            "success": True,
            "data": {"snapshot": snapshot, "replyCount": parsed["reply_count"]},
            "markdown": (
                "## 供应链询盘查询结果\n\n"
                f"- 实例：{snapshot.get('instance_id')}\n"
                f"- 阶段：{snapshot.get('stage')} / 状态：{snapshot.get('status')}\n"
                f"- 找品数：{snapshot.get('total_items')} | 已回复：{parsed['reply_count']}\n\n"
                f"### 商家回复\n\n{body}"
            ),
        }

    if tool_name == "inquiry_submit":
        item = (args.get("item") or "").strip()
        question = (args.get("question") or "").strip()
        if not item or not question:
            return {"success": False, "markdown": "❌ 需要商品链接和询盘问题"}
        return run_inquiry_submit(item, question, args.get("quantity"), args.get("address"))

    if tool_name == "inquiry_query":
        task_id = (args.get("taskId") or "").strip()
        if not task_id:
            return {"success": False, "markdown": "❌ 需要 taskId"}
        return run_inquiry_query(task_id)

    if tool_name == "procurement_stats":
        return execute_procurement_stats(args)

    if tool_name == "order_query":
        return execute_order_query(args)

    if tool_name == "order_query_capabilities":
        from app.services.agent.data_query import execute_order_query_capabilities

        return execute_order_query_capabilities(args)

    if tool_name == "order_inquiry_send":
        order_id = resolve_followup_order_id(args.get("order_id") or "", order_context) or (
            args.get("order_id") or ""
        ).strip()
        raw_q = (args.get("question") or "").strip()
        question = (
            raw_q
            if len(raw_q) >= 4
            else resolve_followup_question("", order_id, order_context)
        )
        if not order_id:
            return {"success": False, "markdown": "❌ 需要 1688 订单号"}
        sent = execute_order_followup_send(order_id, question)
        out: dict[str, Any] = {
            "success": sent["success"],
            "markdown": sent.get("markdown"),
            "error": sent.get("error"),
        }
        if sent.get("taskId"):
            out["taskId"] = sent["taskId"]
        if sent.get("data"):
            out["data"] = sent["data"]
        return out

    if tool_name == "newton_consult":
        message = (args.get("message") or "").strip()
        if not message:
            return {"success": False, "markdown": "❌ 需要咨询内容 message"}
        outcome = task_create(message)
        if not outcome.ok or not outcome.result:
            return {
                "success": False,
                "error": outcome.error,
                "markdown": f"❌ 智能咨询发起失败：{outcome.error or '调用失败'}",
            }
        r = outcome.result
        if not r.get("success") or not r.get("taskId"):
            return {
                "success": False,
                "error": r.get("error"),
                "markdown": f"❌ 智能咨询发起失败：{r.get('error') or '未知错误'}",
            }
        display_q = (args.get("user_question") or message).strip()
        ask_seller = looks_like_merchant_inquiry(display_q)
        return {
            "success": True,
            "taskId": r.get("taskId"),
            "data": {
                "sessionId": r.get("sessionId"),
                "status": r.get("status"),
                "question": display_q,
                "ask_seller": ask_seller,
            },
            "markdown": (
                "✅ 已向该商品卖家发起询问（长程任务，通常需数分钟至数十分钟）。回复会显示在**任务中心**。"
                if ask_seller
                else "✅ 已发起智能咨询（长程任务，通常需要几分钟）。进度与结果会显示在**任务中心**，无需记住任务编号。"
            ),
        }

    if tool_name == "category_map_suggest":
        goods_id = (args.get("goods_id") or "").strip()
        title = (args.get("title") or "").strip()
        product = find_by_source_product_id(goods_id) if goods_id else None
        if not title and product:
            title = str(product.get("product_name") or "").strip()
        if not title:
            return {"success": False, "markdown": "❌ 需要商品标题 title"}

        write_back = ""
        if product:
            from app.services.products.service import map_product_category_by_id

            updated = map_product_category_by_id(product["tangbuy_product_id"])
            if not updated:
                return {"success": False, "markdown": "❌ 映射执行失败"}
            result = _suggest_result_from_product(updated)
            pid = product["tangbuy_product_id"]
            status = str(updated.get("category_status") or "")
            if status in ("auto_passed", "confirmed"):
                write_back = f"\n\n✅ 已写入商品中心（{pid}）。"
            elif status == "needs_review":
                write_back = f"\n\n⚠️ 建议已生成，需在商品中心复核（{pid}）。"
            else:
                write_back = f"\n\n商品中心状态：{status}（{pid}）"
        else:
            from app.services.category_mapping.suggest import run_category_mapping_suggest

            result = run_category_mapping_suggest(
                title,
                hint=(args.get("hint") or "").strip() or None,
                goods_id=goods_id or None,
                image_url=(args.get("image_url") or "").strip() or None,
            )
        return {
            "success": bool(result.get("success")),
            "markdown": _build_suggest_markdown(result) + write_back,
            "data": result,
            "error": result.get("error"),
        }

    if tool_name == "category_map_confirm":
        goods_id = (args.get("goods_id") or "").strip()
        category_cn = (args.get("category_cn_name") or "").strip()
        hs_code = (args.get("hs_code") or "").strip()
        if not goods_id:
            return {"success": False, "markdown": "❌ 需要 goods_id 定位商品"}
        if not hs_code or not category_cn:
            return {"success": False, "markdown": "❌ 需要 category_cn_name 和 hs_code"}
        product = find_by_source_product_id(goods_id)
        if not product:
            return {
                "success": False,
                "markdown": f"❌ 未找到 1688 商品 {goods_id} 对应的大店商品，请先加入商品中心",
            }
        hs = {
            "category_id": int(args.get("category_id") or 0),
            "category_cn_name": category_cn,
            "category_en_name": (args.get("category_en_name") or "").strip(),
            "hs_code": hs_code,
            "declare_cn_name": (args.get("declare_cn_name") or category_cn).strip(),
            "declare_en_name": (args.get("declare_en_name") or args.get("category_en_name") or "").strip(),
            "tariff": None,
        }
        updated = update_product(
            product["tangbuy_product_id"],
            lambda p: confirm_product_mapping(p, hs, manual=True, defer_side_effects=True),
        )
        finalize_product_mapping_confirm(updated, manual=True, resolution="manual_correct")
        if not updated:
            return {"success": False, "markdown": "❌ 写入失败"}
        return {
            "success": True,
            "markdown": (
                "✅ 已确认并写入商品中心\n\n"
                f"- 商品：{product['tangbuy_product_id']}\n"
                f"- 类目：{hs['category_cn_name']}\n"
                f"- HS 编码：{hs['hs_code']}\n"
                f"- 中文描述：{hs['declare_cn_name']}\n"
                f"- 英文描述：{hs['declare_en_name']}"
            ),
            "data": {"product_id": product["tangbuy_product_id"], "hs": hs},
        }

    return {
        "success": False,
        "markdown": f"❌ 工具 {tool_name} 尚未迁移到 Python 后端，请稍后重试。",
        "error": "not_migrated",
    }
