"""Shadow eval 指标计算。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.evolution.types import EvolutionEngineConfig


def build_shadow_eval_result(
    *,
    test_case_count: int,
    old_hits: int,
    new_hits: int,
    config: EvolutionEngineConfig | None = None,
) -> dict[str, Any]:
    cfg = config or EvolutionEngineConfig()
    n = max(test_case_count, 1)
    old_accuracy = round(old_hits / n * 100, 2)
    new_accuracy = round(new_hits / n * 100, 2)
    delta = round(new_accuracy - old_accuracy, 2)
    passed = delta >= cfg.shadow_accuracy_improvement_threshold
    return {
        "test_case_count": test_case_count,
        "old_accuracy": old_accuracy,
        "new_accuracy": new_accuracy,
        "accuracy_delta": delta,
        "old_hallucination_rate": round((n - old_hits) / n * 100, 2),
        "new_hallucination_rate": round((n - new_hits) / n * 100, 2),
        "passed": passed,
        "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
