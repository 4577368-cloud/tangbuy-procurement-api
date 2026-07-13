"""AI 自进化引擎 · 技能注册表（对齐前端 skill-registry.ts）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.evolution.types import (
    EvolutionDomain,
    EvolutionPhase,
    EvalCriterion,
    FeedbackSource,
    PatchInjection,
    SkillEvolutionDescriptor,
)

# ─── 评估标准定义 ───

ORDER_FLOW_CRITERIA = [
    EvalCriterion(name="routing_accuracy", description="意图路由准确率", threshold=0.90, weight=0.30),
    EvalCriterion(name="hallucination_rate", description="编造结果比例（越低越好）", threshold=0.05, weight=0.25),
    EvalCriterion(name="adoption_rate", description="AI建议采纳率", threshold=0.70, weight=0.25),
    EvalCriterion(name="resolution_speed", description="问题解决速度提升率", threshold=0.15, weight=0.20),
]

PRODUCT_PROCESSING_CRITERIA = [
    EvalCriterion(name="category_accuracy", description="品类映射准确率", threshold=0.85, weight=0.35),
    EvalCriterion(name="confidence_alignment", description="置信度与实际准确性对齐度", threshold=0.80, weight=0.25),
    EvalCriterion(name="override_rate", description="人工纠正率（越低越好）", threshold=0.15, weight=0.25),
    EvalCriterion(name="coverage_rate", description="成功给出建议的覆盖率", threshold=0.90, weight=0.15),
]

AGENT_CORE_CRITERIA = [
    EvalCriterion(name="tool_call_accuracy", description="工具调用准确率", threshold=0.90, weight=0.30),
    EvalCriterion(name="hallucination_rate", description="编造结果比例", threshold=0.05, weight=0.30),
    EvalCriterion(name="user_satisfaction", description="用户满意度评分", threshold=0.75, weight=0.20),
    EvalCriterion(name="context_efficiency", description="上下文利用率（减少追问）", threshold=0.80, weight=0.20),
]

# ─── 技能定义 ───

SKILL_DEFINITIONS: list[SkillEvolutionDescriptor] = [
    # ── 采购助手总入口 ──
    SkillEvolutionDescriptor(
        skill_id="procurement-assistant",
        skill_name="采购助手",
        domain=EvolutionDomain.AGENT_CORE,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[
            FeedbackSource.MANUAL_AUDIT,
            FeedbackSource.MANUAL_RATING,
            FeedbackSource.USAGE_SIGNAL,
        ],
        eval_criteria=AGENT_CORE_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=8,
        priority_badcase_trigger=True,
        analysis_prompt_template=(
            "采购助手 badcase：重点检查是否该调工具未调、编造商品/催单、路由到错误 Skill。"
        ),
    ),
    SkillEvolutionDescriptor(
        skill_id="order-data-query",
        skill_name="订单数据查询",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        workflow_stage="pay_accept",
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.MANUAL_RATING],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.90,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
        analysis_prompt_template="订单查询 badcase：检查筛选条件理解、字段映射、统计口径。",
    ),
    # ── Agent 对话核心域 ──
    SkillEvolutionDescriptor(
        skill_id="newton-cloud",
        skill_name="牛顿云智能咨询",
        domain=EvolutionDomain.AGENT_CORE,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_ADOPTION, FeedbackSource.MANUAL_RATING],
        eval_criteria=AGENT_CORE_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
    ),
    SkillEvolutionDescriptor(
        skill_id="1688-product-find",
        skill_name="1688智能选品",
        domain=EvolutionDomain.AGENT_CORE,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_DISMISS, FeedbackSource.MANUAL_RATING, FeedbackSource.USAGE_SIGNAL],
        eval_criteria=AGENT_CORE_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
    ),
    SkillEvolutionDescriptor(
        skill_id="product-compare",
        skill_name="选品比价",
        domain=EvolutionDomain.PRODUCT_PROCESSING,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.MANUAL_RATING, FeedbackSource.USAGE_SIGNAL],
        eval_criteria=PRODUCT_PROCESSING_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=3,
    ),
    SkillEvolutionDescriptor(
        skill_id="1688-sourcing",
        skill_name="1688寻源询盘",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.AUTO_OVERRIDE, FeedbackSource.USAGE_SIGNAL],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
    ),
    SkillEvolutionDescriptor(
        skill_id="supplychain-procurement",
        skill_name="供应链批量询盘",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.AUTO_OVERRIDE, FeedbackSource.USAGE_SIGNAL],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
    ),
    SkillEvolutionDescriptor(
        skill_id="order-followup",
        skill_name="订单催单",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        workflow_stage="pipeline_advance",
        feedback_channels=[FeedbackSource.MANUAL_AUDIT, FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_ADOPTION, FeedbackSource.MANUAL_RATING],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
        analysis_prompt_template="催单场景badcase分析：重点关注催发货/物流核实/改价三类意图的路由准确性和回复质量。",
        patch_prompt_template="催单场景prompt调优：重点补充催发货的关键词触发条件和回复模板。",
    ),
    # ── 商品处理域 ──
    SkillEvolutionDescriptor(
        skill_id="category-mapping",
        skill_name="品类映射（HS编码）",
        domain=EvolutionDomain.PRODUCT_PROCESSING,
        phase=EvolutionPhase.PASSIVE,
        workflow_stage="category_map",
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_DISMISS, FeedbackSource.AUTO_ADOPTION, FeedbackSource.MANUAL_AUDIT],
        eval_criteria=PRODUCT_PROCESSING_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=8,
        min_feedback_for_analysis=3,           # 【问题4】品类映射使用频率高，反馈积累快，3 条即可触发
        priority_badcase_trigger=True,          # 【问题4】品类映射纠正直接影响业务，允许单条触发
        analysis_prompt_template=(
            "品类映射badcase分析框架：\n"
            "1. 检查标题意图与推荐品类是否语义一致（参考同义词群）\n"
            "2. 统计错误模式：多义词误判、视觉模型偏移、历史命中率下降\n"
            "3. 识别哪些锚点词导致了误判\n"
            "4. 评估多信号融合评分中哪个信号权重需要调整"
        ),
        patch_prompt_template=(
            "品类映射prompt调优：\n"
            "1. 补充「XX品类与YY品类容易混淆，应优先检查ZZ关键词」的区分指令\n"
            "2. 降低特定锚点词的权重\n"
            "3. 增加特定场景的判断规则（如「标题含X但不含Y时，应排除Z品类」）\n"
            "4. 标题同时出现「制服/工作服/演出服」与「西装/西服」时，优先按制服/演出服领域处理，不要误归为普通西装\n"
            "5. 过滤非商品词（如测试、形象、模特、广告）和无效 HS 编码的 junk 类目\n"
            "6. 职业词单独成类且无有效 HS 编码时（如仅有「保安」而无「保安服」），应排除或大幅降权"
        ),
    ),
    # ── 订单流程域 ──
    SkillEvolutionDescriptor(
        skill_id="risk-signal-detection",
        skill_name="风险信号识别",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        workflow_stage="release_gate",
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_DISMISS, FeedbackSource.AUTO_ADOPTION, FeedbackSource.MANUAL_AUDIT],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=5,
        min_feedback_for_analysis=5,           # 【问题4】风险信号直接关系资金安全，5条即触发
        priority_badcase_trigger=True,          # 【问题4】关键风险误判允许单条触发
        analysis_prompt_template=(
            "风险信号badcase分析：\n"
            "1. 统计哪些信号类型被人工驳回最多\n"
            "2. 分析AI建议的置信度与实际准确性是否对齐\n"
            "3. 识别遗漏的风险场景（AI没检测到但人工发现了）\n"
            "4. 评估建议操作的实用性"
        ),
    ),
    SkillEvolutionDescriptor(
        skill_id="auto-release",
        skill_name="Agent自动放行",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        workflow_stage="release_gate",
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_DISMISS, FeedbackSource.MANUAL_AUDIT],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.90,
        auto_review_threshold=0.60,
        patch_injection=PatchInjection.THRESHOLD_CONFIG_UPDATE,
        max_active_patches=3,
    ),
    SkillEvolutionDescriptor(
        skill_id="order-note-classify",
        skill_name="订单备注分类",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        workflow_stage="pipeline_advance",
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.MANUAL_AUDIT],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.ROUTE_RULE_INSERT,
        max_active_patches=5,
    ),
    SkillEvolutionDescriptor(
        skill_id="topup-request",
        skill_name="补款通知生成",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.AUTO_DISMISS, FeedbackSource.MANUAL_AUDIT],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.85,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=3,
    ),
    SkillEvolutionDescriptor(
        skill_id="seller-contact",
        skill_name="卖家联系Prompt生成",
        domain=EvolutionDomain.ORDER_FLOW,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.MANUAL_RATING],
        eval_criteria=ORDER_FLOW_CRITERIA,
        auto_pass_threshold=0.80,
        auto_review_threshold=0.50,
        patch_injection=PatchInjection.SYSTEM_PROMPT_APPEND,
        max_active_patches=3,
    ),
    # ── 前端路由与防编造 ──
    SkillEvolutionDescriptor(
        skill_id="deterministic-routing",
        skill_name="前端确定性路由",
        domain=EvolutionDomain.AGENT_CORE,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.AUTO_OVERRIDE, FeedbackSource.MANUAL_AUDIT],
        eval_criteria=[EvalCriterion(name="routing_accuracy", description="路由准确率", threshold=0.90, weight=1.0)],
        auto_pass_threshold=0.90,
        auto_review_threshold=0.60,
        patch_injection=PatchInjection.ROUTE_RULE_INSERT,
        max_active_patches=10,
    ),
    SkillEvolutionDescriptor(
        skill_id="hallucination-detect",
        skill_name="编造检测",
        domain=EvolutionDomain.AGENT_CORE,
        phase=EvolutionPhase.PASSIVE,
        feedback_channels=[FeedbackSource.MANUAL_AUDIT],
        eval_criteria=[EvalCriterion(name="detection_accuracy", description="编造检测准确率", threshold=0.95, weight=1.0)],
        auto_pass_threshold=0.95,
        auto_review_threshold=0.70,
        patch_injection=PatchInjection.ROUTE_RULE_INSERT,
        max_active_patches=5,
    ),
]

# ─── 注册表管理 ───

_registry: dict[str, SkillEvolutionDescriptor] = {
    s.skill_id: s for s in SKILL_DEFINITIONS
}


def get_all_evolution_skills() -> list[dict[str, Any]]:
    """获取所有已注册技能的公开信息。"""
    return [s.to_public() for s in _registry.values()]


def get_evolution_skill(skill_id: str) -> Optional[SkillEvolutionDescriptor]:
    """获取指定技能的描述符。"""
    return _registry.get(skill_id)


def workflow_stage_for_skill(skill_id: str) -> Optional[str]:
    """技能对应的 WorkflowRun 步骤（供 audit / trace 聚合）。"""
    desc = _registry.get(skill_id)
    if desc and desc.workflow_stage:
        return desc.workflow_stage
    return None


def get_skills_by_domain(domain: EvolutionDomain) -> list[SkillEvolutionDescriptor]:
    """按域获取技能列表。"""
    return [s for s in _registry.values() if s.domain == domain]


def register_skill_for_evolution(descriptor: SkillEvolutionDescriptor) -> None:
    """注册新技能（未来技能注入入口）。"""
    _registry[descriptor.skill_id] = descriptor


def update_skill_phase(skill_id: str, phase: EvolutionPhase) -> None:
    """更新技能进化阶段。"""
    existing = _registry.get(skill_id)
    if existing:
        existing.phase = phase


def get_domain_summary() -> dict[str, Any]:
    """按域汇总。"""
    summary: dict[str, Any] = {}
    for domain in EvolutionDomain:
        skills = get_skills_by_domain(domain)
        summary[domain.value] = {
            "count": len(skills),
            "skills": [s.skill_name for s in skills],
        }
    return summary
