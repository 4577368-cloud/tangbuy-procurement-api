"""后台任务单元测试。"""

from __future__ import annotations

import time

from app.services.background_jobs import create_job, get_job, run_job


def test_background_job_completes():
    job_id = create_job("test", label="ping")

    def _work() -> dict:
        return {"ok": True}

    run_job(job_id, _work)
    deadline = time.time() + 3
    status = "pending"
    while time.time() < deadline:
        row = get_job(job_id)
        assert row is not None
        status = row["status"]
        if status in ("done", "failed"):
            break
        time.sleep(0.05)

    row = get_job(job_id)
    assert row is not None
    assert row["status"] == "done"
    assert row["result"] == {"ok": True}
