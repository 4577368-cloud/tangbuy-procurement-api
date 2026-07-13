"""部署后指标与回滚检测。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.services.evolution.store import get_patch_by_id

_METRICS_PATH = data_dir() / "evolution" / "deploy-metrics.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def append_deploy_metric(
    patch_id: str,
    *,
    metric: str,
    value: float,
    context: Optional[dict[str, Any]] = None,
) -> None:
    _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "patch_id": patch_id,
        "metric": metric,
        "value": value,
        "context": context or {},
        "at": _now_iso(),
    }
    with _METRICS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def list_deploy_metrics(*, patch_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    if not _METRICS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in _METRICS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if patch_id and item.get("patch_id") != patch_id:
            continue
        rows.append(item)
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return rows[:limit]


def should_rollback_patch(patch_id: str, *, override_rate_threshold: float = 0.25) -> bool:
    """部署后 override 率超阈值则建议回滚。"""
    patch = get_patch_by_id(patch_id)
    if not patch or patch.get("status") != "deployed":
        return False
    metrics = list_deploy_metrics(patch_id=patch_id, limit=500)
    overrides = [m for m in metrics if m.get("metric") == "post_deploy_override"]
    if len(overrides) < 5:
        return False
    rate = sum(float(m.get("value") or 0) for m in overrides) / len(overrides)
    return rate >= override_rate_threshold
