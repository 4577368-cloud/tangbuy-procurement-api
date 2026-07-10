"""Skill 审计 → 自进化引擎桥接：Badcase 留档后捕获反馈、LLM 单条诊断、触发分析。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.services.agent.llm import chat_completion
from app.services.evolution.engine import capture_feedback, trigger_analysis
from app.services.evolution.skill_registry import get_evolution_skill


def _resolve_domain(skill_id: str) -> str:
    descriptor = get_evolution_skill(skill_id)
    if descriptor:
        return descriptor.domain.value
    if skill_id in ("category-mapping", "product-compare"):
        return "product_processing"
    if skill_id in ("order-followup", "risk-signal-detection", "auto-release", "order-note-classify"):
        return "order_flow"
    return "agent_core"


def _infer_error_category(inv: dict[str, Any]) -> str:
    outcome = str(inv.get("outcome") or "")
    if outcome == "no_tool":
        return "routing_mistake"
    if outcome == "manual_kill":
        return "context_missing"
    if outcome == "api_fail":
        return "wrong_params"
    if outcome == "permission_denied":
        return "other"
    return "wrong_suggestion"


def _preview(text: Optional[str], limit: int = 400) -> str:
    if not text:
        return ""
    clean = str(text).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def capture_invocation_feedback(
    inv: dict[str, Any],
    *,
    note: str = "",
    issue: Optional[str] = None,
) -> str:
    """写入 evolution feedback.jsonl，返回 feedback id。"""
    skill_id = str(inv.get("skill_id") or "unknown")
    user_msg = _preview(inv.get("user_message_preview"), 300)
    response = _preview(inv.get("response_preview") or inv.get("error"), 400)
    tool = str(inv.get("tool") or "")
    outcome = str(inv.get("outcome") or "")

    ai_preview = f"[{tool}] {response}" if response else f"[{tool}] ({outcome})"
    human_preview = note.strip() or f"审计标记 Badcase · {inv.get('id', '')}"

    return capture_feedback(
        {
            "skill_id": skill_id,
            "domain": _resolve_domain(skill_id),
            "source": "manual_audit",
            "sentiment": "negative",
            "feedback_intent": "correction",
            "is_priority_badcase": True,
            "ai_output_preview": ai_preview,
            "human_decision_preview": human_preview,
            "correction_value": user_msg or None,
            "context_ref": str(inv.get("task_id") or inv.get("id") or ""),
            "error_category": issue or _infer_error_category(inv),
        }
    )


def diagnose_invocation(inv: dict[str, Any], note: str = "") -> dict[str, Any]:
    """对单条 Skill 执行记录做 LLM 回溯诊断。"""
    skill_id = str(inv.get("skill_id") or "unknown")
    descriptor = get_evolution_skill(skill_id)
    skill_name = descriptor.skill_name if descriptor else skill_id
    custom = (descriptor.analysis_prompt_template or "") if descriptor else ""

    custom_block = f"专项指引:\n{custom}\n" if custom else ""

    prompt = f"""你是 AI 质量分析师。采购员在 Skill 审计中把以下执行标记为 Badcase，请回溯诊断根因。

技能: {skill_name} ({skill_id})
工具: {inv.get("tool") or "—"}
执行结果: {inv.get("outcome") or "—"}
用户输入:
{inv.get("user_message_preview") or "(无)"}

AI / 工具返回:
{inv.get("response_preview") or inv.get("error") or "(无)"}

审计备注:
{note.strip() or "(无)"}

{custom_block}
请输出 JSON（不要附加解释）:
{{
  "root_cause": "一句话根因",
  "error_category": "routing_mistake | wrong_params | hallucination | low_confidence | wrong_suggestion | context_missing | format_issue | other",
  "findings": ["诊断要点1", "诊断要点2"],
  "suggested_rule": "可写入 Agent 提示词的一条规则",
  "summary": "2-3句复盘摘要"
}}"""

    try:
        resp = chat_completion([{"role": "user", "content": prompt}], temperature=0.2)
        content = (resp.content or "").strip()
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            parsed = json.loads(match.group(0))
            return {
                "root_cause": parsed.get("root_cause") or "",
                "error_category": parsed.get("error_category") or _infer_error_category(inv),
                "findings": parsed.get("findings") or [],
                "suggested_rule": parsed.get("suggested_rule") or "",
                "summary": parsed.get("summary") or "",
            }
    except Exception:
        pass

    return {
        "root_cause": note.strip() or "审计标记为 Badcase，待进一步归纳",
        "error_category": _infer_error_category(inv),
        "findings": [],
        "suggested_rule": "",
        "summary": f"{skill_name} 执行被标记 Badcase（{inv.get('outcome') or '—'}）",
    }


def bridge_badcase_to_evolution(
    inv: dict[str, Any],
    *,
    note: str = "",
    issue: Optional[str] = None,
    run_diagnosis: bool = True,
    run_analysis: bool = True,
) -> dict[str, Any]:
    """Badcase / 打补丁后：捕获反馈 → 单条诊断 → 可选触发批量分析。"""
    feedback_id = capture_invocation_feedback(inv, note=note, issue=issue)

    diagnosis: Optional[dict[str, Any]] = None
    if run_diagnosis:
        diagnosis = diagnose_invocation(inv, note)

    report: Optional[dict[str, Any]] = None
    analysis_triggered = False
    if run_analysis:
        skill_id = str(inv.get("skill_id") or "")
        report = trigger_analysis(min_feedback_count=1, skill_id=skill_id or None)
        analysis_triggered = report is not None

    return {
        "feedback_id": feedback_id,
        "diagnosis": diagnosis,
        "analysis_triggered": analysis_triggered,
        "report_id": report.get("id") if report else None,
        "generated_patch_count": report.get("generated_patch_count") if report else 0,
    }
