"""WorkflowRun 聚合测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.workflow.aggregate import enrich_workflow_run


class WorkflowAggregateTests(unittest.TestCase):
    def test_enrich_groups_invocations_by_stage(self) -> None:
        run = {
            "ord_line_no": "TI26030000055",
            "status": "running",
            "step_history": [
                {"step": "category_map", "status": "ok", "at": "2026-07-13T08:00:00.000Z"},
            ],
        }
        invocations = [
            {
                "id": "inv-1",
                "skill_id": "category-mapping",
                "tool": "category_map_confirm",
                "workflow_stage": "category_map",
                "audit_status": "pending",
                "outcome": "api_ok",
                "at": "2026-07-13T08:00:01.000Z",
            }
        ]
        with patch(
            "app.services.skill_audit.store.list_invocations_for_ord_line",
            return_value=invocations,
        ):
            enriched = enrich_workflow_run(run)
        self.assertEqual(enriched["invocation_summary"]["total"], 1)
        self.assertIn("category_map", enriched["invocations_by_stage"])
        hist = enriched["step_history_enriched"][0]
        self.assertEqual(hist.get("linked_invocation_ids"), ["inv-1"])
        self.assertTrue(hist.get("linked_skills"))


if __name__ == "__main__":
    unittest.main()
