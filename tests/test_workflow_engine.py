"""WorkflowRun 引擎测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.workflow import engine as eng
from app.services.workflow import run_store as store


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workflow-runs.jsonl"
        self.patcher = patch.object(store, "_RUN_PATH", self.path)
        self.patcher.start()
        self.db_patcher = patch.object(store, "is_db_enabled", return_value=False)
        self.db_patcher.start()

    def tearDown(self) -> None:
        self.db_patcher.stop()
        self.patcher.stop()
        self.tmp.cleanup()

    def test_ensure_and_record_steps(self) -> None:
        run = eng.ensure_workflow_run("TI26030000055", ord_no="TO26030000056")
        self.assertEqual(run["ord_line_no"], "TI26030000055")
        self.assertEqual(run["workflow_type"], "procurement_fulfillment")
        self.assertEqual(run["status"], "running")

        eng.record_workflow_step(
            "TI26030000055",
            "category_map",
            status="ok",
            actor="user",
            evidence={"category_id": 121450006},
            linked_refs={"product_id": "178383858042986745"},
        )
        loaded = eng.get_workflow_run_for_line("TI26030000055")
        assert loaded is not None
        self.assertEqual(loaded["current_step"], "category_map")
        self.assertEqual(len(loaded.get("step_history") or []), 1)
        self.assertEqual(loaded["step_history"][0]["step"], "category_map")

    def test_release_gate_blocked(self) -> None:
        eng.ensure_workflow_run("TI26070000100")
        eng.record_workflow_step(
            "TI26070000100",
            "release_gate",
            status="blocked",
            actor="rule",
            evidence={"result": "needs_review", "eligible": False},
            blockers=[{"key": "category", "label": "品类已映射"}],
        )
        loaded = eng.get_workflow_run_for_line("TI26070000100")
        assert loaded is not None
        self.assertEqual(loaded["status"], "blocked")
        self.assertTrue(loaded.get("blockers"))

    def test_dedup_identical_pipeline_blocked(self) -> None:
        eng.ensure_workflow_run("TI26070000101")
        blockers = [{"key": "CAT", "label": "品类未映射", "detail": "需人工映射"}]
        for _ in range(3):
            eng.record_workflow_step(
                "TI26070000101",
                "pipeline_advance",
                status="blocked",
                evidence={"pipeline_step": "blocked", "summary": "备货处理：品类未映射"},
                blockers=blockers,
            )
        loaded = eng.get_workflow_run_for_line("TI26070000101")
        assert loaded is not None
        self.assertEqual(len(loaded.get("step_history") or []), 1)


if __name__ == "__main__":
    unittest.main()
