"""auto_deploy 与 shadow eval 测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.evolution.auto_deploy import advance_gray_percent, gray_bucket, should_apply_patch
from app.services.evolution.engine import deploy_patch
from app.services.evolution.eval.evaluator import build_shadow_eval_result
from app.services.evolution.eval.runner import run_shadow_eval_for_patch
from app.services.evolution.policy_apply import resolve_threshold_for_skill
from app.services.evolution.store import append_patch, get_patch_by_id


class AutoDeployTests(unittest.TestCase):
    def test_should_apply_stable(self) -> None:
        key = "TI26030000055"
        a = should_apply_patch(key, 50)
        b = should_apply_patch(key, 50)
        self.assertEqual(a, b)

    def test_gray_bucket_range(self) -> None:
        self.assertGreaterEqual(gray_bucket("x"), 0)
        self.assertLess(gray_bucket("x"), 100)

    def test_advance_gray_steps(self) -> None:
        self.assertEqual(advance_gray_percent(0), 5)
        self.assertEqual(advance_gray_percent(5), 20)
        self.assertEqual(advance_gray_percent(100), 100)


class ShadowEvalTests(unittest.TestCase):
    def test_build_shadow_result_pass(self) -> None:
        r = build_shadow_eval_result(test_case_count=10, old_hits=3, new_hits=8)
        self.assertTrue(r["passed"])
        self.assertEqual(r["new_accuracy"], 80.0)

    def test_keyword_boost_patch_eval(self) -> None:
        sample_patch = {
            "target_skill_id": "category-mapping",
            "type": "keyword_boost",
            "payload": {
                "keyword_boost": {
                    "trigger_keywords": ["假发"],
                    "target_category_name": "假发",
                }
            },
        }
        with patch(
            "app.services.evolution.eval.runner.load_correction_cases",
            return_value=[
                {
                    "ai_output_preview": "羽绒服",
                    "correction_value": "假发",
                    "human_decision_preview": "标题含假发",
                    "feedback_intent": "correction",
                }
            ],
        ):
            result = run_shadow_eval_for_patch(sample_patch)
        self.assertEqual(result["test_case_count"], 1)
        self.assertGreaterEqual(result["new_accuracy"], result["old_accuracy"])


class DeployGuardTests(unittest.TestCase):
    def test_deploy_requires_shadow_pass(self) -> None:
        patch_id = f"patch-test-deploy-{int(__import__('time').time() * 1000)}"
        append_patch(
            {
                "id": patch_id,
                "type": "keyword_boost",
                "target_skill_id": "category-mapping",
                "status": "approved",
                "active": False,
            }
        )
        self.assertIsNone(deploy_patch(patch_id))
        from app.services.evolution.store import update_patch_eval_result

        update_patch_eval_result(patch_id, {"passed": True, "new_accuracy": 80})
        self.assertIsNotNone(deploy_patch(patch_id))
        deployed = get_patch_by_id(patch_id)
        self.assertEqual(deployed.get("status"), "deployed")
        self.assertEqual(int(deployed.get("gray_percent") or 0), 5)


class ThresholdApplyTests(unittest.TestCase):
    def test_ignores_wrong_threshold_key(self) -> None:
        patches = [
            {
                "target_skill_id": "auto-release",
                "type": "threshold_adjust",
                "gray_percent": 100,
                "payload": {
                    "threshold_key": "auto_pass_threshold",
                    "new_value": 0.85,
                },
            }
        ]
        self.assertEqual(
            resolve_threshold_for_skill(
                "auto-release",
                15.0,
                "TI001",
                patches,
                threshold_key="gross_margin_threshold",
            ),
            15.0,
        )

    def test_applies_matching_threshold_key(self) -> None:
        patches = [
            {
                "target_skill_id": "auto-release",
                "type": "threshold_adjust",
                "gray_percent": 100,
                "payload": {
                    "threshold_key": "gross_margin_threshold",
                    "new_value": 12.0,
                },
            }
        ]
        self.assertEqual(
            resolve_threshold_for_skill(
                "auto-release",
                15.0,
                "TI001",
                patches,
                threshold_key="gross_margin_threshold",
            ),
            12.0,
        )


class ThresholdEvalTests(unittest.TestCase):
    def test_margin_threshold_improves_on_raise(self) -> None:
        patch = {
            "target_skill_id": "auto-release",
            "type": "threshold_adjust",
            "payload": {
                "threshold_key": "gross_margin_threshold",
                "old_value": 15,
                "new_value": 18,
            },
        }
        cases = [
            {"margin_pct": 16.0, "feedback_intent": "correction"},
            {"margin_pct": 20.0, "feedback_intent": "correction"},
        ]
        old_hits, new_hits = _eval_threshold_patch(cases, patch)
        self.assertLess(old_hits, new_hits)


class DeployTrackingTests(unittest.TestCase):
    def test_tracks_override_for_gray_patch(self) -> None:
        from app.services.evolution.eval.deploy_tracking import track_deploy_feedback
        from app.services.evolution.eval.metrics import list_deploy_metrics
        from app.services.evolution.store import append_patch, update_patch_gray, update_patch_status

        patch_id = f"patch-track-{int(__import__('time').time() * 1000)}"
        append_patch(
            {
                "id": patch_id,
                "type": "keyword_boost",
                "target_skill_id": "category-mapping",
                "status": "approved",
                "active": False,
                "gray_percent": 0,
                "payload": {"keyword_boost": {"trigger_keywords": ["x"], "target_category_name": "y"}},
            }
        )
        update_patch_status(patch_id, "deployed")
        update_patch_gray(patch_id, 100)
        track_deploy_feedback(
            {
                "skill_id": "category-mapping",
                "feedback_intent": "correction",
                "source": "auto_override",
                "sentiment": "negative",
                "context_ref": "goods-1",
                "context_meta": {"goods_id": "goods-1"},
            }
        )
        metrics = list_deploy_metrics(patch_id=patch_id, limit=10)
        self.assertTrue(any(m.get("metric") == "post_deploy_override" for m in metrics))


# late import for threshold test
from app.services.evolution.eval.runner import _eval_threshold_patch  # noqa: E402


if __name__ == "__main__":
    unittest.main()
