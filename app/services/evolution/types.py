"""AI 自进化引擎 · 类型定义（对齐前端 types.ts）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ─── 基础类型 ───


class EvolutionPhase(str, Enum):
    PASSIVE = "passive"
    ACTIVE = "active"
    AUTONOMOUS = "autonomous"


class FeedbackSource(str, Enum):
    AUTO_OVERRIDE = "auto_override"
    AUTO_DISMISS = "auto_dismiss"
    AUTO_ADOPTION = "auto_adoption"
    MANUAL_AUDIT = "manual_audit"
    MANUAL_RATING = "manual_rating"
    USAGE_SIGNAL = "usage_signal"


class FeedbackSentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class FeedbackIntent(str, Enum):
    """反馈意图：区分「AI错了」vs「人补充信息」vs「确认AI正确」vs「无关操作」"""
    CORRECTION = "correction"       # AI 输出有错误，人工纠正了（真 badcase）
    ENRICHMENT = "enrichment"       # AI 输出不完整，人工补充了信息（不算 badcase）
    CONFIRMATION = "confirmation"   # 人工确认了 AI 输出正确（正样本）
    NEUTRAL = "neutral"             # 其他操作，不构成 badcase 也不构成正样本


class EvolutionDomain(str, Enum):
    ORDER_FLOW = "order_flow"
    PRODUCT_PROCESSING = "product_processing"
    AGENT_CORE = "agent_core"


class EvolutionErrorCategory(str, Enum):
    ROUTING_MISTAKE = "routing_mistake"
    WRONG_PARAMS = "wrong_params"
    HALLUCINATION = "hallucination"
    LOW_CONFIDENCE = "low_confidence"
    WRONG_SUGGESTION = "wrong_suggestion"
    CONTEXT_MISSING = "context_missing"
    FORMAT_ISSUE = "format_issue"
    OTHER = "other"


class PatchType(str, Enum):
    PROMPT_PATCH = "prompt_patch"
    ROUTE_RULE = "route_rule"
    THRESHOLD_ADJUST = "threshold_adjust"
    CONTEXT_ENRICHMENT = "context_enrichment"


class EvolutionPatchStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    SHADOW = "shadow"
    DEPLOYED = "deployed"
    ROLLED_BACK = "rolled_back"
    DISCARDED = "discarded"


class PatchInjection(str, Enum):
    SYSTEM_PROMPT_APPEND = "system_prompt_append"
    ROUTE_RULE_INSERT = "route_rule_insert"
    THRESHOLD_CONFIG_UPDATE = "threshold_config_update"


# ─── 数据类 ───


@dataclass
class EvalCriterion:
    name: str
    description: str
    threshold: float
    weight: float


@dataclass
class SkillEvolutionDescriptor:
    skill_id: str
    skill_name: str
    domain: EvolutionDomain
    phase: EvolutionPhase = EvolutionPhase.PASSIVE
    feedback_channels: list[FeedbackSource] = field(default_factory=list)
    eval_criteria: list[EvalCriterion] = field(default_factory=list)
    auto_pass_threshold: float = 0.85
    auto_review_threshold: float = 0.55
    analysis_prompt_template: Optional[str] = None
    patch_prompt_template: Optional[str] = None
    patch_injection: PatchInjection = PatchInjection.SYSTEM_PROMPT_APPEND
    max_active_patches: int = 5
    min_feedback_for_analysis: int = 10           # 【问题4修复】per-skill 独立阈值
    priority_badcase_trigger: bool = False        # 【问题4修复】是否允许单条高优先级 badcase 触发分析

    def to_public(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "domain": self.domain.value,
            "phase": self.phase.value,
            "feedback_channels": [c.value for c in self.feedback_channels],
            "eval_criteria": [c.to_public() for c in self.eval_criteria],
            "auto_pass_threshold": self.auto_pass_threshold,
            "auto_review_threshold": self.auto_review_threshold,
            "analysis_prompt_template": self.analysis_prompt_template,
            "patch_prompt_template": self.patch_prompt_template,
            "patch_injection": self.patch_injection.value,
            "max_active_patches": self.max_active_patches,
            "min_feedback_for_analysis": self.min_feedback_for_analysis,
            "priority_badcase_trigger": self.priority_badcase_trigger,
        }


@dataclass
class EvolutionFeedbackRecord:
    id: str
    skill_id: str
    domain: EvolutionDomain
    source: FeedbackSource
    sentiment: FeedbackSentiment
    ai_output_preview: str
    human_decision_preview: Optional[str] = None
    correction_value: Optional[str] = None
    context_ref: Optional[str] = None
    error_category: Optional[EvolutionErrorCategory] = None
    feedback_intent: FeedbackIntent = FeedbackIntent.NEUTRAL   # 【问题5修复】区分 correction / enrichment / confirmation / neutral
    is_priority_badcase: bool = False                           # 【问题4修复】是否为高优先级 badcase（单条可触发分析）
    at: str = ""
    analyzed: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "domain": self.domain.value,
            "source": self.source.value,
            "sentiment": self.sentiment.value,
            "ai_output_preview": self.ai_output_preview,
            "human_decision_preview": self.human_decision_preview,
            "correction_value": self.correction_value,
            "context_ref": self.context_ref,
            "error_category": self.error_category.value if self.error_category else None,
            "feedback_intent": self.feedback_intent.value,
            "is_priority_badcase": self.is_priority_badcase,
            "at": self.at,
            "analyzed": self.analyzed,
        }


@dataclass
class BadcasePattern:
    name: str
    error_category: EvolutionErrorCategory
    description: str
    frequency: int
    representative_cases: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.7
    trigger_keywords: list[str] = field(default_factory=list)
    suggested_fix_type: PatchType = PatchType.PROMPT_PATCH

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "error_category": self.error_category.value,
            "description": self.description,
            "frequency": self.frequency,
            "representative_cases": self.representative_cases,
            "confidence": self.confidence,
            "trigger_keywords": self.trigger_keywords,
            "suggested_fix_type": self.suggested_fix_type.value,
        }


@dataclass
class BadcaseAnalysisReport:
    id: str
    period_start: str
    period_end: str
    skill_ids: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    feedback_count: int = 0
    patterns: list[BadcasePattern] = field(default_factory=list)
    domain_stats: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    improvement_suggestions: list[str] = field(default_factory=list)
    generated_at: str = ""
    analyzer_version: str = "1.0.0-passive"

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "skill_ids": self.skill_ids,
            "domains": self.domains,
            "feedback_count": self.feedback_count,
            "patterns": [p.to_public() for p in self.patterns],
            "domain_stats": self.domain_stats,
            "summary": self.summary,
            "improvement_suggestions": self.improvement_suggestions,
            "generated_at": self.generated_at,
            "analyzer_version": self.analyzer_version,
        }


@dataclass
class EvolutionPatch:
    id: str
    type: PatchType
    target_skill_id: str
    domain: EvolutionDomain
    source_analysis_id: Optional[str] = None
    source_pattern_name: Optional[str] = None
    content: str = ""               # 人类可读描述（补丁摘要）
    payload: Optional[dict[str, Any]] = None  # 【问题3修复】结构化机器可读数据（threshold_adjust 存 {"skill_id":"...", "threshold":0.80}）
    status: EvolutionPatchStatus = EvolutionPatchStatus.DRAFT
    eval_result: Optional[dict[str, Any]] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    deployed_at: Optional[str] = None
    created_at: str = ""
    created_by: str = "auto_generated"
    active: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "target_skill_id": self.target_skill_id,
            "domain": self.domain.value,
            "source_analysis_id": self.source_analysis_id,
            "source_pattern_name": self.source_pattern_name,
            "content": self.content,
            "payload": self.payload,
            "status": self.status.value,
            "eval_result": self.eval_result,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "deployed_at": self.deployed_at,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "active": self.active,
        }


@dataclass
class EvolutionEngineConfig:
    global_phase: EvolutionPhase = EvolutionPhase.PASSIVE
    analysis_interval_days: int = 7
    min_feedback_for_analysis: int = 10
    shadow_test_case_count: int = 100
    shadow_accuracy_improvement_threshold: float = 5.0
    shadow_hallucination_tolerance: float = 2.0
    deploy_gradual_percentages: list[int] = field(default_factory=lambda: [5, 20, 50, 100])
    global_max_active_patches: int = 20
