"""Shadow eval 执行器（按 skill 适配）。"""

from __future__ import annotations

import re
from typing import Any

from app.services.evolution.eval.case_enrich import parse_margin_pct
from app.services.evolution.eval.dataset import load_correction_cases
from app.services.evolution.eval.evaluator import build_shadow_eval_result
from app.services.evolution.types import EvolutionEngineConfig


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _baseline_hit(case: dict[str, Any]) -> bool:
    expected = _norm(str(case.get("correction_value") or case.get("human_decision_preview") or ""))
    ai = _norm(str(case.get("ai_output_preview") or ""))
    if not expected or not ai:
        return False
    return expected in ai or ai in expected


def _patch_would_fix(case: dict[str, Any], patch: dict[str, Any]) -> bool:
    if _baseline_hit(case):
        return True
    payload = patch.get("payload") if isinstance(patch.get("payload"), dict) else {}
    boost = payload.get("keyword_boost") if isinstance(payload.get("keyword_boost"), dict) else {}
    keywords = [str(k).strip() for k in (boost.get("trigger_keywords") or []) if str(k).strip()]
    target = _norm(str(boost.get("target_category_name") or ""))
    expected = _norm(str(case.get("correction_value") or case.get("human_decision_preview") or ""))
    if not keywords or not target or not expected:
        return False
    haystack = " ".join(
        [
            str(case.get("title") or ""),
            str(case.get("product_title") or ""),
            str(case.get("human_decision_preview") or ""),
            str(case.get("ai_output_preview") or ""),
            str(case.get("context_ref") or ""),
        ]
    )
    if not any(k in haystack for k in keywords):
        return False
    return target in expected or expected in target


def _eval_threshold_patch(cases: list[dict[str, Any]], patch: dict[str, Any]) -> tuple[int, int]:
    """threshold_adjust：按毛利门槛对比旧/新策略在纠正样本上的表现。"""
    from app.config.store import get_business_config

    payload = patch.get("payload") if isinstance(patch.get("payload"), dict) else {}
    threshold_key = str(payload.get("threshold_key") or "gross_margin_threshold")

    if threshold_key == "gross_margin_threshold":
        default_old = float(get_business_config().get("gross_margin_threshold") or 15)
    else:
        default_old = 0.85

    try:
        old_thr = float(payload.get("old_value") or default_old)
    except (TypeError, ValueError):
        old_thr = default_old
    try:
        new_thr = float(payload.get("new_value") or old_thr)
    except (TypeError, ValueError):
        new_thr = old_thr

    old_hits = 0
    new_hits = 0
    evaluated = 0
    for case in cases:
        margin = parse_margin_pct(case)
        if margin is None:
            continue
        evaluated += 1
        old_released = margin >= old_thr
        new_released = margin >= new_thr
        # 纠正样本：人工认为不应放行 → 拦截为正确
        old_hits += int(not old_released)
        new_hits += int(not new_released)
    if evaluated == 0:
        return 0, 0
    return old_hits, new_hits


def run_shadow_eval_for_patch(
    patch: dict[str, Any],
    *,
    config: EvolutionEngineConfig | None = None,
) -> dict[str, Any]:
    cfg = config or EvolutionEngineConfig()
    skill_id = str(patch.get("target_skill_id") or "").strip()
    patch_type = str(patch.get("type") or "").strip()
    cases = load_correction_cases(skill_id, limit=cfg.shadow_test_case_count)
    if not cases:
        return build_shadow_eval_result(
            test_case_count=0,
            old_hits=0,
            new_hits=0,
            config=cfg,
        ) | {"passed": False, "reason": "no_correction_cases"}

    old_hits = 0
    new_hits = 0
    if patch_type == "threshold_adjust":
        old_hits, new_hits = _eval_threshold_patch(cases, patch)
    else:
        for case in cases:
            base_ok = _baseline_hit(case)
            old_hits += int(base_ok)
            new_hits += int(_patch_would_fix(case, patch))

    return build_shadow_eval_result(
        test_case_count=len(cases),
        old_hits=old_hits,
        new_hits=new_hits,
        config=cfg,
    )
