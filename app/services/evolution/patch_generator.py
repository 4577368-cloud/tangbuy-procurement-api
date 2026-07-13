"""AI 自进化引擎 · 补丁自动生成器（直接调用 llm.py）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.agent.llm import chat_completion
from app.services.evolution.store import append_patch, get_patches, get_active_patches, update_patch_status
from app.services.evolution.skill_registry import get_evolution_skill
from app.services.evolution.types import (
    EvolutionDomain,
    PatchType,
    EvolutionPatchStatus,
)


# ─── 从分析报告生成补丁 ───


def generate_patches_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """根据分析报告自动生成补丁草案。"""
    new_patches: list[dict[str, Any]] = []

    for pattern in report.get("patterns") or []:
        skill_id = ""
        # 从代表性案例推断技能 ID
        cases = pattern.get("representative_cases") or []
        if cases and isinstance(cases[0], dict):
            skill_id = cases[0].get("skill_id") or ""
        if not skill_id:
            continue

        descriptor = get_evolution_skill(skill_id)
        if not descriptor:
            continue

        # 检查补丁数量上限
        existing = get_patches(skill_id=skill_id, active_only=True)
        if len(existing) >= descriptor.max_active_patches:
            continue

        fix_type = pattern.get("suggested_fix_type") or "prompt_patch"
        patch_type = PatchType(fix_type) if fix_type in [t.value for t in PatchType] else PatchType.PROMPT_PATCH

        try:
            content, payload = _generate_patch_content_with_llm(descriptor, pattern, patch_type)
        except Exception:
            content = _build_template_patch_content(descriptor, pattern, patch_type)
            payload = _build_template_patch_payload(descriptor, pattern, patch_type)

        patch_id = f"patch-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{skill_id[:8]}"

        patch = {
            "id": patch_id,
            "type": patch_type.value,
            "target_skill_id": descriptor.skill_id,
            "domain": descriptor.domain.value,
            "source_analysis_id": report.get("id"),
            "source_pattern_name": pattern.get("name"),
            "content": content,
            "payload": payload,  # 【问题3修复】结构化 payload
            "status": EvolutionPatchStatus.DRAFT.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "auto_generated",
            "active": False,
        }

        append_patch(patch)
        new_patches.append(patch)

    return new_patches


# ─── LLM 生成补丁内容 ───


def _generate_patch_content_with_llm(
    descriptor: Any,
    pattern: dict[str, Any],
    patch_type: PatchType,
) -> tuple[str, Optional[dict[str, Any]]]:
    """直接调用后端 LLM 生成补丁内容。返回 (content, payload)。"""
    custom_template = descriptor.patch_prompt_template or ""
    cases_preview = ""
    for c in (pattern.get("representative_cases") or [])[:3]:
        if isinstance(c, dict):
            cases_preview += f"AI输出: {c.get('ai_output_preview', '')}\n人工决策: {c.get('human_decision_preview') or '(无)'}\n"

    payload_instruction = ""
    if patch_type == PatchType.THRESHOLD_ADJUST:
        payload_instruction = "同时输出 JSON payload: {\"skill_id\": \"...\", \"threshold_key\": \"...\", \"old_value\": ..., \"new_value\": ...}"
    elif patch_type == PatchType.ROUTE_RULE:
        payload_instruction = "同时输出 JSON payload: {\"trigger_pattern\": \"...\", \"target_skill\": \"...\", \"condition\": \"...\", \"priority\": \"high|low\"}"

    custom_block = f"技能专项调优指引:\n{custom_template}\n" if custom_template else ""

    prompt = f"""你是一个AI系统调优专家。请根据以下错误模式和技能描述，生成一个具体的调优补丁。

技能: {descriptor.skill_name}({descriptor.skill_id})
业务域: {descriptor.domain.value}
补丁类型: {patch_type.value}
错误模式: {pattern.get('name')}
模式描述: {pattern.get('description')}
频次: {pattern.get('frequency')}次
触发关键词: {', '.join(pattern.get('trigger_keywords') or []) or '(无)'}

{custom_block}
代表性案例:
{cases_preview}

请生成一条简短、精确的调优指令（不超过100字），格式要求:
- prompt_patch: 直接写出追加到 system prompt 的指令文本
- route_rule: 写出 JSON 格式的路由规则
- threshold_adjust: 写出建议的新阈值数值及理由。{payload_instruction}
- context_enrichment: 写出建议补充的上下文字段名及格式

只输出补丁内容，不要附加解释。"""

    # 直接调用后端 LLM
    resp = chat_completion(
        [{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    content = (resp.content or "").strip()
    if not content:
        return _build_template_patch_content(descriptor, pattern, patch_type), _build_template_patch_payload(descriptor, pattern, patch_type)
    
    # 尝试提取 payload（如果 LLM 输出了 JSON）
    payload = _try_extract_payload(content, patch_type)
    return content, payload


# ─── Fallback：模板生成 ───


def _try_extract_payload(content: str, patch_type: PatchType) -> Optional[dict[str, Any]]:
    """尝试从 LLM 输出中提取结构化 payload。"""
    if patch_type not in (PatchType.THRESHOLD_ADJUST, PatchType.ROUTE_RULE):
        return None
    json_match = re.search(r"\{[\s\S]*\}", content)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            if isinstance(data, dict) and ("new_value" in data or "trigger_pattern" in data):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _build_threshold_payload(
    descriptor: Any,
    pattern: dict[str, Any],
) -> dict[str, Any]:
    """按技能生成 threshold_adjust 结构化 payload。"""
    if descriptor.skill_id == "auto-release":
        from app.config.store import get_business_config

        old_val = float(get_business_config().get("gross_margin_threshold") or 15)
        new_val = min(100.0, round(old_val + 2.0, 2))
        return {
            "skill_id": descriptor.skill_id,
            "threshold_key": "gross_margin_threshold",
            "old_value": old_val,
            "new_value": new_val,
            "reason": pattern.get("description"),
        }
    new_threshold = descriptor.auto_pass_threshold - 0.05
    return {
        "skill_id": descriptor.skill_id,
        "threshold_key": "auto_pass_threshold",
        "old_value": descriptor.auto_pass_threshold,
        "new_value": new_threshold,
        "reason": pattern.get("description"),
    }


def _build_template_patch_content(
    descriptor: Any,
    pattern: dict[str, Any],
    patch_type: PatchType,
) -> str:
    keywords = "、".join(pattern.get("trigger_keywords") or [pattern.get("name")])

    if patch_type == PatchType.PROMPT_PATCH:
        return f"【自进化补丁】当用户输入涉及「{keywords}」时，应优先走{descriptor.skill_name}而非其他技能；若无法确定意图，应追问而非自行选择。"

    if patch_type == PatchType.ROUTE_RULE:
        keyword = (pattern.get("trigger_keywords") or ["关键词"])[0]
        return json.dumps({
            "trigger_pattern": keyword,
            "target_skill": descriptor.skill_id,
            "condition": f"用户消息含「{keyword}」时，强制路由到 {descriptor.skill_id}",
            "priority": "high",
        }, ensure_ascii=False)

    if patch_type == PatchType.THRESHOLD_ADJUST:
        payload = _build_threshold_payload(descriptor, pattern)
        threshold_key = payload["threshold_key"]
        content = (
            f"建议将 {descriptor.skill_name} 的 {threshold_key} "
            f"从 {payload['old_value']} 调整为 {payload['new_value']}，"
            f"原因: {pattern.get('description')}"
        )
        return content

    if patch_type == PatchType.CONTEXT_ENRICHMENT:
        return f"建议在 {descriptor.skill_name} 的上下文中补充 order_stage 字段，以帮助区分不同订单阶段的意图路由。"

    return f"补丁内容（{patch_type.value}）：{pattern.get('description')}"


def _build_template_patch_payload(
    descriptor: Any,
    pattern: dict[str, Any],
    patch_type: PatchType,
) -> Optional[dict[str, Any]]:
    """【问题3修复】为模板生成的补丁创建结构化 payload。"""
    if patch_type == PatchType.THRESHOLD_ADJUST:
        return _build_threshold_payload(descriptor, pattern)
    if patch_type == PatchType.ROUTE_RULE:
        keyword = (pattern.get("trigger_keywords") or ["关键词"])[0]
        return {
            "trigger_pattern": keyword,
            "target_skill": descriptor.skill_id,
            "condition": f"用户消息含「{keyword}」时，强制路由到 {descriptor.skill_id}",
            "priority": "high",
        }
    return None


# ─── 补丁注入（供 orchestrator 和 skills 使用） ───


def get_all_active_prompt_patches() -> list[str]:
    """汇总所有已部署的 prompt 补丁（统一助手 system prompt 注入）。"""
    seen: set[str] = set()
    out: list[str] = []
    for p in get_active_patches():
        if p.get("type") != PatchType.PROMPT_PATCH.value:
            continue
        content = (p.get("content") or "").strip()
        if content and content not in seen:
            seen.add(content)
            out.append(content)
    return out


def get_active_prompt_patches(skill_id: str) -> list[str]:
    """获取指定技能的活跃 prompt 补丁内容列表。"""
    patches = get_active_patches(skill_id=skill_id)
    return [p.get("content") for p in patches if p.get("type") == "prompt_patch"]


def get_active_route_patches() -> list[dict[str, Any]]:
    """获取所有活跃的路由规则补丁。"""
    patches = get_active_patches()
    result = []
    for p in patches:
        if p.get("type") != "route_rule":
            continue
        payload = p.get("payload")
        if isinstance(payload, dict) and payload.get("trigger_pattern"):
            result.append(payload)
            continue
        try:
            rule = json.loads(p.get("content") or "{}")
            if rule.get("trigger_pattern"):
                result.append(rule)
        except json.JSONDecodeError:
            continue
    return result


def get_active_threshold_patches(
    *,
    context_key: str = "default",
    threshold_key: str = "gross_margin_threshold",
) -> dict[str, float]:
    """已部署阈值补丁（含灰度分桶）。优先使用 resolve_threshold_for_skill。"""
    from app.config.store import get_business_config
    from app.services.evolution.policy_apply import resolve_threshold_for_skill

    patches = get_active_patches()
    skill_ids = {
        str(p.get("target_skill_id") or "")
        for p in patches
        if str(p.get("type") or "") == "threshold_adjust"
    }
    out: dict[str, float] = {}
    cfg = get_business_config()
    for sid in skill_ids:
        if not sid:
            continue
        default = float(cfg.get("gross_margin_threshold") or 15) if sid == "auto-release" else 0.85
        out[sid] = resolve_threshold_for_skill(
            sid,
            default,
            context_key,
            patches,
            threshold_key=threshold_key if sid == "auto-release" else "auto_pass_threshold",
        )
    return out


def get_active_keyword_boost_patches() -> list[dict[str, Any]]:
    """已部署的 keyword_boost 补丁（含灰度 percent）。"""
    return [
        p
        for p in get_active_patches()
        if str(p.get("type") or "") in ("keyword_boost",)
    ]


# ─── 手动创建补丁 ───


def create_manual_patch(
    skill_id: str,
    patch_type: str,
    content: str,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """人工手动创建补丁。"""
    descriptor = get_evolution_skill(skill_id)
    patch = {
        "id": f"patch-manual-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "type": patch_type,
        "target_skill_id": skill_id,
        "domain": descriptor.domain.value if descriptor else "agent_core",
        "content": content,
        "status": EvolutionPatchStatus.DRAFT.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by or "manual",
        "active": False,
    }
    append_patch(patch)
    return patch
