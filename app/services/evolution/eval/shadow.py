"""Shadow eval 编排。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.services.evolution.eval.runner import run_shadow_eval_for_patch
from app.services.evolution.store import get_patch_by_id, update_patch_eval_result, update_patch_status


def run_shadow_eval(patch_id: str) -> Optional[dict[str, Any]]:
    patch = get_patch_by_id(patch_id)
    if not patch:
        return None
    if patch.get("status") != "approved":
        return None

    update_patch_status(patch_id, "shadow")

    eval_result = run_shadow_eval_for_patch(patch)
    updated = update_patch_eval_result(patch_id, eval_result)
    if not updated:
        return None

    # 试运行结束回到 approved，未通过不自动废弃（用户可改补丁后重试）
    update_patch_status(patch_id, "approved")

    return {"patch": get_patch_by_id(patch_id), "eval_result": eval_result}
