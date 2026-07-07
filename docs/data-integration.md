# 数据集成架构

## 总原则

- **读**：订单、子单、商品、状态、金额 — 来自数仓/业务库 **API**，不以页面 state 为真相。
- **写**：本系统产生的结果（品类映射、大店商品、任务状态等）— 通过 **API 回写** 到目标库字段或业务表。
- **字段名**：请求/响应使用 `TangbuyOrdLineRow` 与 `field-catalog.ts` 中的 **DB 字段名**，UI 只做展示映射。

代码契约：`src/lib/tangbuy/integration-contract.ts`

## 采购履约入口

```
用户支付成功 (pay_time)
    → 子单 ord_line_no 进入采购履约范围
    → 指挥中心 / 订单中心 / 规则引擎 / 采购助手 从此读宽表作业
```

判定辅助：`isInProcurementScope(row)`（`pay_time` 存在且非删除/非终结状态）。

订单**数量、列表、状态**一律由 API 返回，不从本地 mock 数组「假装」为真相（mock 仅作种子，接口形状不变）。

## 读路径（规划）

| 能力 | API 职责 | 宽表/字段 |
|------|----------|-----------|
| 订单中心列表 | `OrdLineReadPort.list` | `ord_line_no`, `ord_line_stat`, `item_*`, 金额… |
| 订单详情 | `OrdLineReadPort.getByOrdLineNo` | 宽表行或子集 |
| 指挥中心队列 | 同上 + 聚合统计 | `ord_line_stat`, `pay_time` |
| 采购助手上下文 | `mapOrdLineToAgentContext(row)` | 同宽表 |

实现替换点：将 `lib/mock/orders.ts` 换为 `OrdLineReadPort` 的 HTTP 实现，**UI 与类型不变**。

## 写路径（规划）

### 1. 品类映射 → 回写宽表品类/报关字段

人工确认或自动通过后，调用 `OrdLineWritePort.updateCategory`：

| 写入字段 | 含义 |
|----------|------|
| `ctgy_id`, `lvl1_ctgy_id`, `lvl1_ctgy_nm` … | 类目 |
| `cstm_hs_cd` | 海关编码 |
| `dcl_cn_nm`, `dcl_en_nm` | 申报中英文 |

Payload 类型：`CategoryWriteBackPayload`；由 `categoryWriteBackFromHs()` 从映射结果生成。

商品中心、订单子单、Agent 映射 **同一套写回**，不要各写各的字段名。

### 2. 商品中心 / 大店 → 商品主数据

`ProductWritePort.upsertProduct` → `item_id`, `item_nm`, `prc`, `pur_prc`, `splr_item_id` 等（见 `PRODUCT_CENTER_TANGBUY_MAP`）。

### 3. 本系统新增实体（库中尚无或独立表）

| 实体 | 当前 | 未来 |
|------|------|------|
| Agent 任务 | 内存 + 可选本地 | `AgentTaskWritePort` → 业务库 |
| 商品中心列表 | `data/products/center.json` | 商品服务 API |
| 映射审计 | `data/category/*.jsonl` | 审计表 API |

**规则**：新功能若产生需长期保留的数据，设计时就要定义 **WritePort + 目标表/字段**，不要只停在前端或临时文件。

## 开发检查清单

1. 数据从哪读？是否对应 `OrdLineReadPort` 或明确的外部 API？
2. 写回哪几个 **DB 字段**？是否在 `field-catalog.ts` 登记为 `mapped` / `write`？
3. 是否经过 `integration-contract` 中的 payload 类型，避免 UI 直传随意 JSON？
4. 接库时是否只换 Port 实现，而不改页面与 Agent 契约？

## 与 mock 的关系

`lib/mock` = **种子数据 + Port 的临时实现**。接库后删除或降级为测试夹具，**不**改变产品数据流设计。
