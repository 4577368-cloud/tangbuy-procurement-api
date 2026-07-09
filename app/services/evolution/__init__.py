"""AI 自进化引擎 · 模块入口。"""

from app.services.evolution.engine import (
    capture_feedback,
    trigger_analysis,
    approve_patch,
    deploy_patch,
    rollback_patch,
    discard_patch,
    get_overview,
)
from app.services.evolution.patch_generator import (
    get_active_prompt_patches,
    get_active_route_patches,
    get_active_threshold_patches,
)
