"""AI 自进化引擎 · Badcase 分析引擎（直接调用 llm.py，无需前端代理）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.agent.llm import chat_completion
from app.services.evolution.store import (
    append_report,
    get_negative_unanalyzed_feedback,
    mark_feedback_analyzed,
    get_feedback_records,
)
from app.services.evolution.skill_registry import get_evolution_skill, get_skills_by_domain
from app.services.evolution.types import (
    EvolutionDomain,
    EvolutionErrorCategory,
    PatchType,
)


# ─── 分析 Prompt 构建 ───


def build_domain_analysis_prompt(
    domain: EvolutionDomain,
    feedbacks: list[dict[str, Any]],
) -> str:
    domain_label = {
        EvolutionDomain.ORDER_FLOW: "订单流程",
        EvolutionDomain.PRODUCT_PROCESSING: "商品处理",
        EvolutionDomain.AGENT_CORE: "Agent核心",
    }.get(domain, domain.value)

    skills = get_skills_by_domain(domain)
    skill_list = "、".join(f"{s.skill_name}({s.skill_id})" for s in skills)

    feedback_lines = []
    for i, f in enumerate(feedbacks, 1):
        error_label = f.get("error_category") or "未分类"
        feedback_lines.append(
            f"[{i}] 技能:{f.get('skill_id')} | 来源:{f.get('source')} | 错误:{error_label}\n"
            f"    AI输出: {f.get('ai_output_preview', '')}\n"
            f"    人工决策: {f.get('human_decision_preview') or '(无)'}\n"
            f"    纠正值: {f.get('correction_value') or '(无)'}"
        )

    skill_descriptor = get_evolution_skill(feedbacks[0].get("skill_id", "")) if feedbacks else None
    custom_template = skill_descriptor.analysis_prompt_template if skill_descriptor else ""
    custom_block = f"专项分析指引:\n{custom_template}\n" if custom_template else ""

    return f"""你是一个AI质量分析师。请分析以下 {domain_label} 领域的 AI badcase 反馈数据。

涉及技能: {skill_list}

{custom_block}
反馈数据（共{len(feedbacks)}条）:
{"".join(feedback_lines)}

请按以下结构输出分析报告（JSON格式）:

{{
  "patterns": [
    {{
      "name": "模式名称（如'催发货意图被路由到选品'）",
      "error_category": "routing_mistake | wrong_params | hallucination | low_confidence | wrong_suggestion | context_missing | format_issue | other",
      "description": "模式描述",
      "frequency": 出现次数,
      "trigger_keywords": ["触发关键词列表"],
      "confidence": 0.0-1.0的模式置信度,
      "suggested_fix_type": "prompt_patch | route_rule | threshold_adjust | context_enrichment",
      "suggested_fix_description": "建议修复方式描述"
    }}
  ],
  "domain_stats": {{
    "count": 总反馈数,
    "top_errors": ["前3个最常见错误分类"]
  }},
  "summary": "一段话总结本次分析发现",
  "improvement_suggestions": ["3-5条改进建议"]
}}

只输出JSON，不要附加解释。"""


# ─── 分析执行 ───


def run_badcase_analysis(
    min_feedback_count: int = 10,
    skill_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """执行 Badcase 分析：直接在后端调用 LLM，无需前端代理。
    
    【问题4修复】支持：
    1. 按 skill 分桶，每个 skill 使用自己的 min_feedback_for_analysis 阈值
    2. 优先级 badcase（is_priority_badcase=True）单条即可触发
    3. 也可指定 skill_id 只分析某个技能
    """
    unanalyzed = get_negative_unanalyzed_feedback()
    
    # 检查是否有优先级 badcase（单条触发）
    priority_cases = [f for f in unanalyzed if f.get("is_priority_badcase")]
    if priority_cases:
        # 有优先级 badcase → 立即触发分析，不需要等积累
        pass
    elif skill_id:
        # 只分析指定技能 → 使用该技能的独立阈值
        descriptor = get_evolution_skill(skill_id)
        skill_threshold = descriptor.min_feedback_for_analysis if descriptor else min_feedback_count
        skill_feedback = [f for f in unanalyzed if f.get("skill_id") == skill_id]
        if len(skill_feedback) < skill_threshold:
            return None
    elif len(unanalyzed) < min_feedback_count:
        # 全局分析 → 但数量不足
        return None

    # 【问题5修复】只分析 intent=correction 的反馈（真正的 badcase）
    # enrichment/confirmation/neutral 不纳入 badcase 分析
    effective_feedback = [
        f for f in unanalyzed 
        if f.get("feedback_intent") in ("correction", None)  # None 兼容旧数据
    ]
    if not effective_feedback and not priority_cases:
        return None

    # 按域分组
    domain_groups: dict[str, list[dict[str, Any]]] = {
        EvolutionDomain.ORDER_FLOW.value: [],
        EvolutionDomain.PRODUCT_PROCESSING.value: [],
        EvolutionDomain.AGENT_CORE.value: [],
    }
    for f in unanalyzed:
        d = f.get("domain") or "agent_core"
        if d in domain_groups:
            domain_groups[d].append(f)

    all_patterns: list[dict[str, Any]] = []
    domain_stats: dict[str, Any] = {}
    summary_parts: list[str] = []
    suggestions: list[str] = []

    for domain_str, feedbacks in domain_groups.items():
        # 【问题5修复】进一步过滤：只取 intent=correction 的反馈
        correction_feedbacks = [
            f for f in feedbacks
            if f.get("feedback_intent") in ("correction", None)
        ]
        min_domain = 1 if priority_cases else 3
        if len(correction_feedbacks) < min_domain:
            continue

        domain = EvolutionDomain(domain_str)
        prompt = build_domain_analysis_prompt(domain, correction_feedbacks)

        try:
            # 直接调用后端 LLM，不经过 /api/agent/chat
            resp = chat_completion(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            content = (resp.content or "").strip()

            # 提取 JSON
            json_match = re.search(r"\{[\s\S]*\}", content)
            if json_match:
                result = json.loads(json_match.group(0))
                patterns = result.get("patterns") or []
                # 附加代表性案例
                for p in patterns:
                    p["representative_cases"] = [
                        f.to_public() if hasattr(f, "to_public") else f
                        for f in feedbacks[:3]
                    ]
                all_patterns.extend(patterns)
                domain_stats[domain_str] = result.get("domain_stats") or {
                    "count": len(feedbacks),
                    "top_errors": [],
                }
                if result.get("summary"):
                    summary_parts.append(f"[{domain_str}]: {result['summary']}")
                suggestions.extend(result.get("improvement_suggestions") or [])
            else:
                # 降级为简单统计
                fallback = _build_fallback_patterns(feedbacks)
                all_patterns.extend(fallback)
                domain_stats[domain_str] = {
                    "count": len(feedbacks),
                    "top_errors": _get_top_error_categories(feedbacks),
                }
        except Exception as e:
            # 降级
            fallback = _build_fallback_patterns(feedbacks)
            all_patterns.extend(fallback)
            domain_stats[domain_str] = {
                "count": len(feedbacks),
                "top_errors": _get_top_error_categories(feedbacks),
            }

    # 标记反馈已分析
    mark_feedback_analyzed([f.get("id") for f in unanalyzed])

    report_id = f"analysis-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    period_start = min(f.get("at") or "" for f in unanalyzed) if unanalyzed else ""
    period_end = max(f.get("at") or "" for f in unanalyzed) if unanalyzed else ""

    report = {
        "id": report_id,
        "period_start": period_start,
        "period_end": period_end,
        "skill_ids": list(set(f.get("skill_id") for f in unanalyzed)),
        "domains": list(set(f.get("domain") for f in unanalyzed)),
        "feedback_count": len(unanalyzed),
        "patterns": all_patterns,
        "domain_stats": domain_stats,
        "summary": "\n".join(summary_parts) or f"分析了 {len(unanalyzed)} 条反馈，发现 {len(all_patterns)} 个错误模式",
        "improvement_suggestions": suggestions,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analyzer_version": "1.0.0-passive",
    }

    append_report(report)
    return report


# ─── Fallback：简单统计 ───


def _build_fallback_patterns(feedbacks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in feedbacks:
        cat = f.get("error_category") or "other"
        groups.setdefault(cat, []).append(f)

    patterns = []
    for category, records in groups.items():
        if len(records) < 2:
            continue
        patterns.append({
            "name": f"{category} 频发模式（{len(records)}次）",
            "error_category": category,
            "description": f"{category} 类型错误在近期反馈中出现了 {len(records)} 次",
            "frequency": len(records),
            "confidence": min(len(records) / len(feedbacks) + 0.3, 0.9),
            "trigger_keywords": _extract_keywords(records),
            "suggested_fix_type": _infer_fix_type(category),
            "representative_cases": records[:3],
        })
    return patterns


def _get_top_error_categories(feedbacks: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for f in feedbacks:
        cat = f.get("error_category") or "other"
        counts[cat] = counts.get(cat, 0) + 1
    return [cat for cat, _ in sorted(counts.items(), key=lambda x: -x[1])[:3]]


def _extract_keywords(records: list[dict[str, Any]]) -> list[str]:
    keywords: set[str] = set()
    for r in records:
        preview = r.get("ai_output_preview") or ""
        tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", preview)
        keywords.update(tokens)
    return list(keywords)[:10]


def _infer_fix_type(category: str) -> str:
    mapping = {
        "routing_mistake": "route_rule",
        "low_confidence": "threshold_adjust",
        "context_missing": "context_enrichment",
    }
    return mapping.get(category, "prompt_patch")
