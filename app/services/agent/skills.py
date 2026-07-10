"""采购助手 Skill 定义与权限。"""

from __future__ import annotations

from typing import Any, Optional

from app.auth.permissions import RoleGrants, grants_allow

UNIFIED_ASSISTANT_ID = "procurement-assistant"

UNIFIED_SYSTEM_PROMPT = """你是采购助手（Tangbuy 统一对话入口），帮助采购员完成选品、寻源、催单等任务。

## 工作方式
- **同一条对话**内可连续处理多件事；根据用户最新意图选择合适工具。
- 若系统提供了「当前订单上下文」，优先使用其中的订单号、商品名、链接。
- 任务提交后引导用户到**任务中心**查看进度。

## 铁律：不得编造
- 商品名称、价格、销量、供应商、链接**只能来自工具返回**。
- 本轮**没有调用工具**时，**绝不**列出具体商品、价格或供应商。

## 工具路由
| 场景 | 工具 |
|------|------|
| 关键词搜商品 | product_text_search |
| 1688 链接找同款 | product_link_search |
| 主图以图搜图 | product_image_search |
| 同款比价 | product_compare |
| 模糊采购需求、批量询盘 | supplychain_inquiry_start |
| 查询供应链询盘 | supplychain_inquiry_query |
| 模糊寻源（1688 平台报价） | procurement_inquiry |
| 问商家 / 智能咨询 | newton_consult |
| 已下单催发货 | order_inquiry_send |
| 查订单数量/状态/队列 | procurement_stats |
| 查具体订单 / 按条件列订单 | order_query |
| HS 品类映射 | category_map_suggest / category_map_confirm |

## 数据查询铁律
- 问「多少单」「各状态分布」「某订单状态」→ **必须**调用 procurement_stats 或 order_query，不得凭记忆回答数字。
- order_query lookup 返回的订单卡片可点击跳转订单中心。

## 回复风格
- 中文、简洁、面向采购员。"""

TOOL_PERMISSION: dict[str, tuple[str, str]] = {
    "product_text_search": ("assistant.image_search", "edit"),
    "product_link_search": ("assistant.image_search", "edit"),
    "product_image_search": ("assistant.image_search", "edit"),
    "product_compare": ("assistant.image_search", "edit"),
    "procurement_inquiry": ("assistant.sourcing", "edit"),
    "supplychain_inquiry_start": ("assistant.sourcing", "edit"),
    "supplychain_inquiry_query": ("assistant.sourcing", "edit"),
    "inquiry_submit": ("assistant.sourcing", "edit"),
    "inquiry_query": ("assistant.sourcing", "edit"),
    "newton_consult": ("assistant.consult", "edit"),
    "order_inquiry_send": ("assistant.order_followup", "edit"),
    "procurement_stats": ("order.data", "view"),
    "order_query": ("order.data", "view"),
    "category_map_suggest": ("product.category_mapping", "edit"),
    "category_map_confirm": ("product.category_mapping", "edit"),
}

TOOL_OWNER_SKILL: dict[str, str] = {
    "product_text_search": "1688-product-find",
    "product_link_search": "1688-product-find",
    "product_image_search": "1688-product-find",
    "product_compare": "product-compare",
    "procurement_inquiry": "1688-sourcing",
    "supplychain_inquiry_start": "supplychain-procurement",
    "supplychain_inquiry_query": "supplychain-procurement",
    "inquiry_submit": "inquiry-1688",
    "inquiry_query": "inquiry-1688",
    "newton_consult": "newton-cloud",
    "order_inquiry_send": "order-followup",
    "procurement_stats": "order-data-query",
    "order_query": "order-data-query",
    "category_map_suggest": "category-mapping",
    "category_map_confirm": "category-mapping",
}

UNIFIED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "product_text_search",
        "description": "按关键词在 1688 搜索商品。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "string"},
                "sort": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "product_link_search",
        "description": "根据 1688 商品链接找同款。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "image_url": {"type": "string"},
                "limit": {"type": "string"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "product_image_search",
        "description": "根据商品主图 URL 以图搜图。",
        "parameters": {
            "type": "object",
            "properties": {"image_url": {"type": "string"}, "limit": {"type": "string"}},
            "required": ["image_url"],
        },
    },
    {
        "name": "product_compare",
        "description": "同款商品比价。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "image_url": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "string"},
            },
        },
    },
    {
        "name": "procurement_inquiry",
        "description": "发起 1688 寻源询盘（offerName + count + demand）。",
        "parameters": {
            "type": "object",
            "properties": {
                "offerName": {"type": "string"},
                "count": {"type": "string"},
                "demand": {"type": "string"},
            },
            "required": ["offerName", "count", "demand"],
        },
    },
    {
        "name": "supplychain_inquiry_start",
        "description": "发起供应链批量询盘（requirement + questions JSON）。",
        "parameters": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string"},
                "questions": {"type": "string"},
                "purchase_size": {"type": "string"},
                "inquiry_item_size": {"type": "string"},
                "recall_item_size": {"type": "string"},
                "image_urls": {"type": "string"},
            },
            "required": ["requirement", "questions"],
        },
    },
    {
        "name": "supplychain_inquiry_query",
        "description": "按 instance_id 查询供应链询盘进度。",
        "parameters": {
            "type": "object",
            "properties": {"instance_id": {"type": "string"}},
            "required": ["instance_id"],
        },
    },
    {
        "name": "newton_consult",
        "description": "长程智能咨询 / 问商家。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "user_question": {"type": "string"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "order_inquiry_send",
        "description": "已下单催发货 / 问物流。order_id 必填，question 可省略。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "question": {"type": "string"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "procurement_stats",
        "description": "查询采购系统订单队列统计、异常信号、系统概览。scope: orders(默认)/signals/overview。",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "orders=队列分布, signals=指挥中心异常信号, overview=订单+任务概览",
                },
                "queue": {
                    "type": "string",
                    "description": "可选队列：pending_procurement/pending_payment/ordered/shipped/in_warehouse/dispatched/exception/reverse",
                },
            },
        },
    },
    {
        "name": "order_query",
        "description": "查询具体订单或按条件列出订单。lookup=按单号查，list=按队列/关键词列单。",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "lookup 或 list，默认 lookup"},
                "order_id": {"type": "string", "description": "lookup 时：子单号/主单号/1688采购单号"},
                "queue": {"type": "string", "description": "list 时：队列筛选"},
                "keyword": {"type": "string", "description": "list 时：商品名/用户/店铺关键词"},
                "limit": {"type": "string", "description": "list 时返回条数，默认 5，最大 20"},
            },
        },
    },
    {
        "name": "category_map_suggest",
        "description": "建议 HS 品类映射字段。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "hint": {"type": "string"},
                "goods_id": {"type": "string"},
                "image_url": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "category_map_confirm",
        "description": "确认品类映射并写入商品中心。",
        "parameters": {
            "type": "object",
            "properties": {
                "goods_id": {"type": "string"},
                "category_cn_name": {"type": "string"},
                "category_en_name": {"type": "string"},
                "category_id": {"type": "string"},
                "hs_code": {"type": "string"},
                "declare_cn_name": {"type": "string"},
                "declare_en_name": {"type": "string"},
            },
            "required": ["goods_id", "category_cn_name", "hs_code"],
        },
    },
]

LEGACY_SKILLS: list[dict[str, Any]] = [
    {
        "id": "newton-cloud",
        "name": "智能咨询",
        "description": "牛顿云长程任务：开放式采购咨询、对具体商品问价/MOQ（异步，结果进任务中心）",
        "status": "ready",
        "welcomeMessage": "把完整诉求告诉我，我会发起智能咨询长程任务，结果在任务中心查看。",
        "toolCount": 1,
    },
    {
        "id": "1688-product-find",
        "name": "1688 智能选品",
        "description": "关键词 / 链接 / 图片搜索 1688 商品，找同款比价（牛顿 Hub API）",
        "status": "ready",
        "welcomeMessage": "用关键词、1688 链接或商品主图 URL 搜款。",
        "toolCount": 3,
    },
    {
        "id": "product-compare",
        "name": "选品比价",
        "description": "同款商品多维度比价（销量 / 价格 / 综合）",
        "status": "ready",
        "welcomeMessage": "提供 1688 商品链接或主图 URL，我会从同款中选出值得对比的几款。",
        "toolCount": 1,
    },
    {
        "id": "1688-sourcing",
        "name": "1688 寻源询盘",
        "description": "模糊采购需求 → 发起 1688 采购询盘，获取供应商报价",
        "status": "ready",
        "welcomeMessage": "描述你想采购什么、多少、有什么要求。",
        "toolCount": 1,
    },
    {
        "id": "supplychain-procurement",
        "name": "供应链询盘",
        "description": "1688 批量询盘：向多家供应商发起找品+询盘，获取报价与回复（DigitalHuman API）",
        "status": "ready",
        "welcomeMessage": "描述找品需求和询盘问题，我会向多家 1688 供应商发起批量询盘。",
        "toolCount": 2,
    },
    {
        "id": "inquiry-1688",
        "name": "1688 商品询盘",
        "description": "待接入牛顿云 · 对商品链接询价 / 问 MOQ（替代遨虾）",
        "status": "placeholder",
        "welcomeMessage": "本能力将改用牛顿云长程任务，接入前暂不可用。",
        "toolCount": 0,
    },
    {
        "id": "order-data-query",
        "name": "订单数据查询",
        "description": "自然语言查询订单队列统计、具体订单状态、按条件列单（只读）",
        "status": "ready",
        "welcomeMessage": "问订单数量、各状态分布，或粘贴单号查详情。",
        "toolCount": 2,
    },
    {
        "id": "order-followup",
        "name": "催单",
        "description": "1688 订单催发货、核实物流、改价等订单询盘",
        "status": "ready",
        "welcomeMessage": "粘贴 1688 订单号即可，询盘内容系统自动补全。",
        "toolCount": 1,
    },
    {
        "id": "category-mapping",
        "name": "品类映射",
        "description": "待采购商品 → HS 海关编码 / 报关品类字段映射",
        "status": "ready",
        "welcomeMessage": "提供 1688 商品标题、平台类目、商品 ID，尽量附上商品主图链接。",
        "toolCount": 2,
    },
]


def is_tool_allowed(tool_name: str, grants: Optional[RoleGrants]) -> bool:
    req = TOOL_PERMISSION.get(tool_name)
    if not req:
        return True
    return grants_allow(grants, req[0], req[1])  # type: ignore[arg-type]


def filter_tools(grants: Optional[RoleGrants]) -> list[dict[str, Any]]:
    if not grants:
        return []
    return [t for t in UNIFIED_TOOLS if is_tool_allowed(t["name"], grants)]


def resolve_skill_id_for_tool(tool_name: str) -> str:
    return TOOL_OWNER_SKILL.get(tool_name, UNIFIED_ASSISTANT_ID)
