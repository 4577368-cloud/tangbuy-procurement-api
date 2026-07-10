"""采购助手编排（对齐 orchestrator.ts 主路径）。"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.auth.permissions import RoleGrants
from app.services.agent.llm import (
    LlmResponse,
    ToolCall,
    build_assistant_tool_call_message,
    chat_completion,
    new_tool_call_id,
)
from app.services.agent.product_parse import (
    build_product_search_summary,
    is_product_search_tool,
    parse_product_search_payload,
)
from app.services.agent.routing import (
    extract_image_urls,
    extract_product_search_query,
    looks_like_fabricated_followup,
    looks_like_fabricated_products,
    looks_like_product_find,
    resolve_deterministic_route,
    resolve_order_followup_route,
    resolve_product_compare_route,
)
from app.services.agent.skills import (
    UNIFIED_ASSISTANT_ID,
    UNIFIED_SYSTEM_PROMPT,
    filter_tools,
    is_tool_allowed,
    resolve_skill_id_for_tool,
)
from app.services.agent.tools import execute_tool
from app.services.tasks.register import register_task_from_tool

MAX_TOOL_ROUNDS = 4

INTENT_HINTS = {
    "followup": "用户意图：催单 / 问物流 / 跟进已下单商品。优先 order_inquiry_send。",
    "consult": "用户意图：智能咨询或问商家。长程任务用 newton_consult；纯找货仍用 product_*。",
    "sourcing": "用户意图：寻源 / 批量采购。",
    "product_find": "用户意图：选品 / 搜款 / 以图搜图。",
}


def _append_skill_audit_tuning(system_parts: list[str]) -> None:
    """注入 Skill 审计打补丁的活跃调优指令。"""
    try:
        from app.services.skill_audit.store import get_active_skill_tuning_instructions

        instructions = get_active_skill_tuning_instructions()
        if instructions:
            system_parts.append(
                "## Skill 审计调优\n" + "\n".join(f"- {t}" for t in instructions)
            )
    except Exception:
        pass


def _append_evolution_prompt_patches(system_parts: list[str]) -> None:
    """注入已部署的自进化 prompt 补丁（失败时静默跳过）。"""
    try:
        from app.services.evolution.patch_generator import get_all_active_prompt_patches

        patches = get_all_active_prompt_patches()
        if patches:
            system_parts.append(
                "## 自进化调优补丁\n" + "\n".join(f"- {p}" for p in patches)
            )
    except Exception:
        pass


def _tool_denied(tool_name: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "permission_denied",
        "markdown": f"❌ 你当前账号没有「{tool_name}」权限，请联系管理员开通。",
    }


def _format_ctx(context: Optional[dict[str, Any]]) -> str:
    if not context:
        return ""
    lines = []
    for k, label in [
        ("pur_no", "采购单号"),
        ("ord_line_no", "子单号"),
        ("item_nm", "商品"),
        ("splr_item_id", "offer"),
    ]:
        if context.get(k):
            lines.append(f"- {label}：{context[k]}")
    return "\n".join(lines)


def _run_single_tool(
    tool_name: str,
    args: dict[str, str],
    grants: Optional[RoleGrants],
    tool_trace: list[dict[str, Any]],
    order_context: Optional[dict[str, Any]],
    registered_tasks: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    owner = resolve_skill_id_for_tool(tool_name)
    denied = grants is not None and not is_tool_allowed(tool_name, grants)
    result = _tool_denied(tool_name) if denied else execute_tool(tool_name, args, order_context)
    if not denied:
        task = register_task_from_tool(owner, tool_name, args, result)
        if task and registered_tasks is not None:
            registered_tasks.append(task)
    tool_trace.append({"tool": tool_name, "arguments": args, "result": result})

    payload = parse_product_search_payload(result.get("data"))
    if result.get("success") and (is_product_search_tool(tool_name) or tool_name == "product_compare") and payload:
        return {"short_circuit": True, "content": build_product_search_summary(result.get("data"))}

    if result.get("success") and tool_name in ("procurement_stats", "order_query"):
        return {"short_circuit": True, "content": result.get("summary") or result.get("markdown") or "查询完成。"}

    if result.get("success") and tool_name in ("newton_consult", "order_inquiry_send"):
        return {"short_circuit": True, "content": result.get("markdown") or "已提交，请到任务中心查看进度。"}

    if result.get("success") and tool_name in (
        "supplychain_inquiry_start",
        "procurement_inquiry",
    ):
        return {"short_circuit": True, "content": result.get("markdown") or "已提交，请到任务中心查看进度。"}

    if result.get("success") and tool_name in ("category_map_suggest", "category_map_confirm"):
        return {"short_circuit": True, "content": result.get("markdown") or "已完成。"}

    if not result.get("success"):
        if tool_name == "product_link_search":
            return None
        if is_product_search_tool(tool_name) or tool_name == "product_compare":
            raw = (result.get("markdown") or result.get("error") or "搜款接口调用失败").replace("❌", "").strip()
            return {"short_circuit": True, "content": f"{raw}。请稍后重试。"}
        if tool_name in ("newton_consult", "order_inquiry_send"):
            return {"short_circuit": True, "content": result.get("markdown") or f"❌ {result.get('error')}"}

    tool_content = result.get("markdown") or json.dumps(
        {"success": result.get("success"), "error": result.get("error")},
        ensure_ascii=False,
    )
    call_id = new_tool_call_id()
    return {
        "continue_transcript": [
            build_assistant_tool_call_message([ToolCall(call_id, tool_name, args)]),
            {"role": "tool", "content": tool_content, "tool_call_id": call_id, "name": tool_name},
        ]
    }


def run_agent_chat(
    skill_id: str,
    messages: list[dict[str, Any]],
    *,
    context: Optional[dict[str, Any]] = None,
    intent: Optional[str] = None,
    grants: Optional[RoleGrants] = None,
) -> dict[str, Any]:
    if skill_id != UNIFIED_ASSISTANT_ID:
        raise ValueError(f"未知或未迁移 Skill: {skill_id}")

    system_parts = [UNIFIED_SYSTEM_PROMPT]
    ctx_text = _format_ctx(context)
    if ctx_text:
        system_parts.append("## 当前订单上下文\n" + ctx_text)
    if intent and intent in INTENT_HINTS:
        system_parts.append("## 用户当前意图倾向\n" + INTENT_HINTS[intent])
    _append_evolution_prompt_patches(system_parts)
    _append_skill_audit_tuning(system_parts)

    transcript: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(system_parts)},
        *[m for m in messages if m.get("role") in ("user", "assistant")],
    ]

    allowed_tools = filter_tools(grants)
    allowed_names = {t["name"] for t in allowed_tools}
    tool_trace: list[dict[str, Any]] = []
    registered_tasks: list[dict[str, Any]] = []

    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    user_preview = (last_user.get("content") or "")[:200] if last_user else ""

    if last_user:
        route = resolve_deterministic_route(
            last_user.get("content") or "",
            intent,
            allowed_names,
            context,
        )
        if route:
            pre = _run_single_tool(route["tool"], route["args"], grants, tool_trace, context, registered_tasks)
            if pre and pre.get("short_circuit"):
                return {
                    "message": {"role": "assistant", "content": pre["content"]},
                    "toolTrace": tool_trace or None,
                    "registeredTasks": registered_tasks or None,
                }
            if pre and pre.get("continue_transcript"):
                transcript.extend(pre["continue_transcript"])

            link_failed = any(
                t["tool"] == "product_link_search" and not t["result"].get("success") for t in tool_trace
            )
            if link_failed and last_user and "product_image_search" in allowed_names:
                imgs = extract_image_urls(last_user.get("content") or "")
                if imgs:
                    img_run = _run_single_tool(
                        "product_image_search",
                        {"image_url": imgs[0], "limit": "10"},
                        grants,
                        tool_trace,
                        context,
                        registered_tasks,
                    )
                    if img_run and img_run.get("short_circuit"):
                        return {
                            "message": {"role": "assistant", "content": img_run["content"]},
                            "toolTrace": tool_trace or None,
                            "registeredTasks": registered_tasks or None,
                        }
            if link_failed:
                err = next(
                    (t["result"].get("markdown") for t in tool_trace if t["tool"] == "product_link_search"),
                    None,
                )
                return {
                    "message": {
                        "role": "assistant",
                        "content": (err or "").replace("❌", "").strip()
                        or "未能从链接获取主图。请粘贴商品主图 URL，或改用关键词搜索。",
                    },
                    "toolTrace": tool_trace,
                    "registeredTasks": registered_tasks or None,
                }

    for _ in range(MAX_TOOL_ROUNDS):
        llm: LlmResponse = chat_completion(transcript, allowed_tools)

        if not llm.tool_calls:
            if tool_trace:
                cards = next(
                    (
                        t
                        for t in tool_trace
                        if t["result"].get("success")
                        and (is_product_search_tool(t["tool"]) or t["tool"] == "product_compare")
                        and parse_product_search_payload(t["result"].get("data"))
                    ),
                    None,
                )
                if cards:
                    return {
                        "message": {
                            "role": "assistant",
                            "content": build_product_search_summary(cards["result"].get("data")),
                        },
                        "toolTrace": tool_trace,
                        "registeredTasks": registered_tasks or None,
                    }

            if not tool_trace and user_preview:
                if looks_like_fabricated_products(llm.content or ""):
                    return {
                        "message": {
                            "role": "assistant",
                            "content": "以上商品未走搜索接口，不可信。请换关键词重试，或使用选品标签。",
                        }
                    }
                if looks_like_fabricated_followup(llm.content or ""):
                    return {
                        "message": {
                            "role": "assistant",
                            "content": "催单未走接口，不可信。请点「催单」标签并附上订单号后重试。",
                        }
                    }

                followup_route = resolve_order_followup_route(user_preview, allowed_names, context, intent)
                if followup_route:
                    fr = _run_single_tool(
                        followup_route["tool"],
                        followup_route["args"],
                        grants,
                        tool_trace,
                        context,
                        registered_tasks,
                    )
                    if fr and fr.get("short_circuit"):
                        return {
                            "message": {"role": "assistant", "content": fr["content"]},
                            "toolTrace": tool_trace,
                            "registeredTasks": registered_tasks or None,
                        }

                compare_route = resolve_product_compare_route(user_preview, allowed_names)
                if compare_route:
                    cr = _run_single_tool(
                        compare_route["tool"],
                        compare_route["args"],
                        grants,
                        tool_trace,
                        context,
                        registered_tasks,
                    )
                    if cr and cr.get("short_circuit"):
                        return {
                            "message": {"role": "assistant", "content": cr["content"]},
                            "toolTrace": tool_trace,
                            "registeredTasks": registered_tasks or None,
                        }
                elif extract_image_urls(user_preview) and "product_image_search" in allowed_names:
                    ir = _run_single_tool(
                        "product_image_search",
                        {"image_url": extract_image_urls(user_preview)[0], "limit": "10"},
                        grants,
                        tool_trace,
                        context,
                        registered_tasks,
                    )
                    if ir and ir.get("short_circuit"):
                        return {
                            "message": {"role": "assistant", "content": ir["content"]},
                            "toolTrace": tool_trace,
                            "registeredTasks": registered_tasks or None,
                        }
                elif "product_text_search" in allowed_names and (
                    looks_like_product_find(user_preview) or looks_like_fabricated_products(llm.content or "")
                ):
                    sr = _run_single_tool(
                        "product_text_search",
                        {
                            "query": extract_product_search_query(user_preview or llm.content or ""),
                            "limit": "10",
                        },
                        grants,
                        tool_trace,
                        context,
                        registered_tasks,
                    )
                    if sr and sr.get("short_circuit"):
                        return {
                            "message": {"role": "assistant", "content": sr["content"]},
                            "toolTrace": tool_trace,
                            "registeredTasks": registered_tasks or None,
                        }

            return {
                "message": {
                    "role": "assistant",
                    "content": (llm.content or "").strip() or "（模型未返回内容）",
                },
                "toolTrace": tool_trace or None,
                "registeredTasks": registered_tasks or None,
            }

        transcript.append(build_assistant_tool_call_message(llm.tool_calls))
        for call in llm.tool_calls:
            denied = grants is not None and not is_tool_allowed(call.name, grants)
            result = _tool_denied(call.name) if denied else execute_tool(call.name, call.arguments, context)
            if not denied:
                task = register_task_from_tool(
                    resolve_skill_id_for_tool(call.name), call.name, call.arguments, result
                )
                if task:
                    registered_tasks.append(task)
            tool_trace.append({"tool": call.name, "arguments": call.arguments, "result": result})
            tool_content = result.get("markdown") or json.dumps(
                {"success": result.get("success"), "error": result.get("error"), "data": result.get("data")},
                ensure_ascii=False,
            )
            transcript.append(
                {
                    "role": "tool",
                    "content": tool_content,
                    "tool_call_id": call.id,
                    "name": call.name,
                }
            )

        product_cards = next(
            (
                t
                for t in tool_trace
                if t["result"].get("success")
                and (is_product_search_tool(t["tool"]) or t["tool"] == "product_compare")
                and parse_product_search_payload(t["result"].get("data"))
            ),
            None,
        )
        if product_cards:
            return {
                "message": {
                    "role": "assistant",
                    "content": build_product_search_summary(product_cards["result"].get("data")),
                },
                "toolTrace": tool_trace,
                "registeredTasks": registered_tasks or None,
            }

        long_running = next(
            (
                t
                for t in tool_trace
                if t["result"].get("success")
                and t["tool"]
                in (
                    "newton_consult",
                    "order_inquiry_send",
                    "supplychain_inquiry_start",
                    "procurement_inquiry",
                )
            ),
            None,
        )
        if long_running:
            return {
                "message": {
                    "role": "assistant",
                    "content": long_running["result"].get("markdown") or "已提交，请到任务中心查看进度。",
                },
                "toolTrace": tool_trace,
                "registeredTasks": registered_tasks or None,
            }

    final = chat_completion(transcript, allowed_tools)
    return {
        "message": {
            "role": "assistant",
            "content": (final.content or "").strip() or "处理完成，请查看上方工具结果。",
        },
        "toolTrace": tool_trace,
        "registeredTasks": registered_tasks or None,
    }
