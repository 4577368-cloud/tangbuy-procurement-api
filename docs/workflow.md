# WorkflowRun（Phase 1）

采购履约端到端 trace，粒度 **`ord_line_no`（TI 子单）**。

## 目标

把分散在 pipeline、品类映射、Admin 回写、放行评估里的步骤，串成一条可查询、可审计、后续可优化的 **WorkflowRun**。

## 对象

```json
{
  "run_id": "wf-TI26030000055",
  "ord_line_no": "TI26030000055",
  "ord_no": "TO26030000056",
  "workflow_type": "procurement_fulfillment",
  "current_step": "admin_writeback",
  "status": "running",
  "step_history": [
    {
      "step": "category_map",
      "status": "ok",
      "actor": "user",
      "evidence": { "category_id": 121450006 },
      "linked_refs": { "product_id": "178383858042986745" },
      "at": "2026-07-13T08:00:00.000Z"
    }
  ],
  "blockers": []
}
```

## 步骤（Phase 1）

| step | 触发点 |
|------|--------|
| `pay_accept` | 预留（支付入池后） |
| `category_map` | `persist_product_mapping_side_effects` |
| `admin_writeback` | `schedule_admin_writeback` 完成 |
| `release_gate` | `procurement_release._persist_release` |
| `pipeline_advance` | `pipeline_store.save_pipeline_state` |

## API

- `GET /api/workflow/runs` — 列表
- `GET /api/workflow/runs/{ord_line_no}` — 单子 trace

## 存储

- SQLite：`workflow_run_record`（`run_json`）
- 无 DB：`data/workflow/workflow-runs.jsonl`

## 后续阶段

- **Phase 2** ✅：skill `workflow_stage`；audit 关联 `ord_line_no`；订单详情 + Agent审计「履约 trace」
## Phase 3 ✅

### Shadow replay
- `POST /api/evolution/patches/shadow-eval` — 历史纠正样本对比
- 写入 `eval_result`（准确率 delta、passed）
- 未通过 → `discarded`；通过 → 保持 `approved` 可灰度部署

### 灰度部署
- `deploy` → `gray_percent=5%`（非立即 100%）
- `POST /api/evolution/patches/advance-gray` → 5→20→50→100
- `auto_deploy.should_apply_patch(context_key, gray_percent)` 稳定分桶

### Policy 注入
- `keyword_boost` → `category_mapping/suggest` 候选加成
- `threshold_adjust` → `procurement_release` 毛利阈值（按子单灰度）

### 指标
- `GET /api/evolution/metrics` — 部署后指标
- `should_rollback_patch` — override 率超阈值建议回滚

### UI
- Agent审计 · 建议：`试运行` → `灰度生效` → `扩大灰度`

## Phase 2

### Skill workflow_stage

| skill_id | workflow_stage |
|----------|----------------|
| category-mapping | category_map |
| auto-release / risk-signal-detection | release_gate |
| order-note-classify / order-followup | pipeline_advance |
| order-data-query | pay_accept |

Skill 调用记录字段：`ord_line_no`、`workflow_run_id`、`workflow_stage`。

### UI

- 订单详情 · 采购 tab：履约 trace 时间线（来自 `/api/orders/{ord_line_no}/pipeline` → `workflow`）
- Agent审计 · **履约 trace** tab：按子单聚合步骤 + Skill 调用
