# 指挥中心行动点盘点与目标动线

> **用途**：盘点当前所有「需人工判断 / 阻塞」入口、触发条件、建议动作与**目标后续动线**。  
> 现状除 **品类映射** 外，订单处置类点击多为前端模拟（信号从列表消失，无业务回写）。  
> 接真实订单前，按本文档逐项实现 WritePort / 外部集成。

**相关代码**

| 区域 | 路径 |
|------|------|
| 指挥中心 UI | `src/components/command-center/` |
| 信号派生 | `src/lib/scenario/derive.ts`（`SIGNAL_TEMPLATES`） |
| 静态种子 | `src/lib/mock/command-center.ts` |
| 停滞原因 taxonomy | `src/lib/mock/stage-subcategories.ts` |
| 处置提交 | `src/context/CommandCenterContext.tsx` → `submitDisposition` |
| Agent 决策分组 | `src/lib/tasks/decision-groups.ts` |
| 自动放行 | `src/lib/mock/agent-releases.ts` |
| 类型 | `src/lib/types/index.ts`（`RiskSignal`, `RecommendedAction`） |

---

## 1. 框架：什么才该在指挥中心「操作」

### 1.1 两大域

| 域 | 含义 | 入口 | 应有结果 |
|----|------|------|----------|
| **订单处理** | 子单在履约链路上卡住，需采购/跟单/仓务/售后介入 | 漏斗 tile → 异常卡片 → **处置抽屉** | 写回宽表状态、触发外部动作、关闭或升级信号 |
| **Agent 决策** | Agent 已跑完，等人确认/查询/跟进 | Agent tile → DrillOverlay | 内联完成或深链到审计/任务中心，结果写库 |

### 1.2 三层分类（订单信号）

| 层 | 字段 | 用途 |
|----|------|------|
| **队列** | `action_required` / `needs_attention` / `watch_list` | 由 `urgency` 映射：待处理 / 需关注 / 观察 |
| **阶段** | `stage`（`OrderStage`） | 漏斗与卡片分组：待采购、待支付、已订购…逆向 |
| **停滞原因** | `signal_type` |  drill 子 Tab、AI 结论与动作模板绑定 |

辅助维度：

- **`is_blocking`**：是否阻塞阶段推进（漏斗 `blocking` 计数）
- **`is_reverse`**：逆向售后流（未到货 / 已到仓）
- **`disposition_status`**：open → resolved（当前多为 open，处置后本地删除）

### 1.3 紧急度与 UI 带

| urgency | UI 带 | 典型 SLA 文案 |
|---------|-------|----------------|
| `immediate` | 待处理 | 「剩余约 X 分钟待处理」 |
| `today` | 待处理 | 「请于今日内…」 |
| `attention` | 需关注 | 「建议 N 天内…」 |
| `observe` | 观察 | 「可继续观察 / 延后提醒」 |

---

## 2. 当前实现状态（总览）

| 能力 | 点击后行为 | 是否接库 |
|------|------------|----------|
| 品类映射复核 | 打开映射弹窗 → `POST suggest` + `PATCH product` | **是** |
| 1688 询盘一键查询 | `POST /api/tasks/:id/refresh` | **是** |
| 牛顿/供应链任务刷新 | 同上 | **是** |
| 寻源查看报价 | 外链 1688 或任务中心 | 只读 |
| 自动放行复核 | 跳转 `/ai-decisions`，内存改状态 | **否** |
| **订单处置（全部 action_key）** | 信号从视图移除 + 可选记 feedback | **否** |
| `contact_seller_ai` | 提示「即将上线」，不提交 | **否** |
| `snooze` / `acknowledge` | 仅本地移除信号 | **否** |
| 卡片「升级」 | 本地 urgency +1 档 | **否** |

**核心缺口**：缺少 `DispositionWritePort`（或按 action 拆分的集成），`target_route` / `requires_confirm` 字段未使用。

---

## 3. 订单信号全表（已实例化 15 种）

检测条件列 = **目标检测逻辑**（接库后由规则引擎 / 宽表字段计算）；「当前」= derive 演示如何造数。

### 3.1 待处理 · 阻塞（7）

| signal_type | 阶段 | 阻塞 | 检测条件（目标） | AI 主结论 | 建议动作 | 目标动线（主操作） | 当前 |
|-------------|------|------|------------------|-----------|----------|-------------------|------|
| **PAY_AMOUNT_GAP** | 待支付 | ✓ | `customer_paid < purchase_payable` 且差额 > 阈值 | 先补款，勿确认支付 | 发起补款 / 等改价 / 异常工单 | 见 §4.1 `request_topup` | 本地移除 |
| **SKU_MISMATCH** | 待采购 | ✓ | 下单 SKU 与货源 SKU 不一致 | 暂停采购，核对或换源 | 异常工单 / 人工确认继续 | 见 §4.2 | 本地移除 |
| **MOQ_VIOLATION** | 待采购 | ✓ | `ord_cnt < MOQ` 且无合单 | 换源或改数量 | 采购异常 / 更换卖家 | 见 §4.3 | 本地移除 |
| **SHIP_OVERDUE** | 已订购 | ✓ | 下单后 > N 天无发货 | 今日催发货 | 催发货 / 提报异常 / 智能联系卖家 | 见 §4.4 | 本地移除 |
| **SHIP_NO_TRACKING** | 已发货 | ✓ | 有发货状态但物流单号无效 | 核实虚假发货 | 提报异常 / 人工核对 / 智能核实 | 见 §4.5 | 本地移除 |
| **REVERSE_RETURN_TRANSIT** | 逆向·未到货 | ✓ | 退货申请 + 包裹在途 | 立即拦截 | 物流拦截 / 同意退货 / 驳回 | 见 §4.6 | 本地移除 |
| **REVERSE_EXCHANGE_TRANSIT** | 逆向·未到货 | ✓ | 换货申请 + 在途 | 拦截 + 协调重发 | 拦截 / 联系卖家 / 同意换货 | 见 §4.6 | 本地移除 |

### 3.2 待处理 · 逆向已到仓（2）

| signal_type | 阶段 | 阻塞 | 检测条件（目标） | 主操作目标动线 | 当前 |
|-------------|------|------|------------------|----------------|------|
| **REVERSE_RETURN_ARRIVED** | 逆向·已到仓 | ✓ | 退货 + 仓已签收 | 验货 → 退款 | 本地移除 |
| **REVERSE_EXCHANGE_ARRIVED** | 逆向·已到仓 | ✓ | 换货 + 货在仓 | 同意换货 → 补发 / 重打包 | 本地移除 |

### 3.3 需关注 / 观察（6）

| signal_type | 阶段 | 阻塞 | 检测条件（目标） | 主操作 | 目标动线要点 | 当前 |
|-------------|------|------|------------------|--------|--------------|------|
| **PRICE_CHANGE_PENDING** | 待支付 | ✗ | 备注/任务含改价中 | 挂起等待改价 | 挂起支付 + 改价任务状态 | 本地移除 |
| **SELLER_DELAY_REPLY** | 已订购 | ✗ | 已催单 >24h 无回复 | 再次催发货 | 创建催单任务 + 计时升级 | 本地移除 |
| **ZERO_MARGIN** | 待采购 | ✗ | 毛利 ≤ 0 | 人工确认放行 | 审计记录 + 放行 WritePort | 本地移除 |
| **REVERSE_RETURN_PENDING** | 逆向·未到货 | ✗ | 非质量退货待审 | 挽留 / 同意退货 | 站内信 + 拦截预备 | 本地移除 |
| **WAREHOUSE_DELAY** | 已到仓 | ✗ | 签收后 > N 天未入库 | 延后提醒 | 仓务工单 + snooze | 本地移除 |
| **LOGISTICS_SLOW** | 已发出 | ✗ | 国际段轨迹停滞 | 延后提醒 | 物流监控 + snooze | 本地移除 |

### 3.4 已登记未实例化（stage-subcategories 扩展）

以下在 `STAGE_STALL_REASONS` 有 label，**derive 尚未造信号**，接库后按同结构补：

`PAYMENT_FAILED`, `PAY_BLOCK`, `NOTE_REVIEW`, `DATA_MISSING`, `STOCKOUT`, `URGE_PENDING`, `FAKE_SHIP`, `LOGISTICS_STALL`, `INSPECT_PENDING`, `REPACK_PENDING`, `CUSTOMS_HOLD`, `DELIVERY_FAILED`, `INTERCEPT_PENDING`, `INSPECT_DEFECT`, `REFUND_PENDING`

---

## 4. action_key 目标动线手册

每个 key 应对应：**触发写操作 → 可观测状态 → 超时 escalation → 关闭信号条件**。

### 4.1 补款与支付

#### `request_topup`（发起补款）— 对应 PAY_AMOUNT_GAP

**触发条件**：实付 < 应付，子单 `ord_line_stat` ∈ 待支付类。

**目标动线**：

1. **写**：创建补款账单（金额 = 差额），关联 `ord_line_no` / `usr_id`
2. **触达买家**：站内信 + 补款链接（模板含订单号、差额、截止时间）
3. **监听**：支付回调 / 宽表 `pay_time`、补款状态字段更新
4. **成功**：自动 `payment_pass` 或人工确认支付 → 子单 → 已订购，**关闭信号**
5. **超时**（建议配置项，如 3 天未补）：
   - 升级 urgency → `today`
   - 若 `bd_usr_nm` 存在 → **通知 BD**：「客户 {usr_id} 订单 {ord_no} 已 {N} 天未补款」
   - 仍无补款 → 可选自动撤单 / 异常工单

**WritePort 草案**：`TopupWritePort.createBill` + `NotificationWritePort.toUser` + `NotificationWritePort.toBd`

**当前**：确认后信号消失，无账单、无站内信。

#### `price_hold`（挂起等待改价）— PRICE_CHANGE_PENDING / PAY_AMOUNT_GAP 备选

**目标动线**：子单标记「改价挂起」→ 阻止支付确认 Agent → 关联改价任务/备注 → 卖家改价完成后解除挂起。

#### `manual_confirm`（人工确认后继续）

**目标动线**：记录审计（操作人、理由）→ 跳过对应 Agent 拦截 → 写 `ord_line_stat` 推进；**必须**留痕供事后审计。

---

### 4.2 采购准入

#### `to_exception`（提交/提报异常工单）

**适用**：SKU_MISMATCH, MOQ_VIOLATION, ZERO_MARGIN, 发货/物流/仓异常等。

**目标动线**：

1. 创建异常工单（类型 = signal_type，关联 ord_line_no）
2. 子单 → 异常队列（`ord_line_stat` 异常态）
3. 指派 handler / 采购员
4. 工单关闭时同步关闭信号

**WritePort 草案**：`ExceptionWritePort.create`

#### `change_seller`（更换卖家）

**目标动线**：打开换源流程 → 商品中心/寻源 → 更新 `splr_item_id` / 链接 → 重新跑采购准入 Agent。

---

### 4.3 跟单 / 催发货

#### `urge_ship`（联系卖家催发货）— SHIP_OVERDUE, SELLER_DELAY_REPLY

**前置**：子单已 **已订购**（有 `pur_no` / 1688 订单号），**非**待采购/待支付。

**目标动线**：

1. 创建 `order_followup` 长程任务（牛顿云）或 B 层催单
2. 任务中心展示商家回复
3. 有回复 → 更新宽表发货/物流字段；无回复 → 按 SLA 升级（见 SELLER_DELAY_REPLY）

**当前**：与订单中心「催单」按钮规则一致（`order-followup.ts`：已订购 ordered 及 shipped / in_warehouse / dispatched 可点；待采购 / 待支付不可）

#### `contact_seller_ai`（智能联系卖家）

**目标动线**：同 `urge_ship`，走 `newton_consult` / 问商家模板，带商品链接与问题。

**当前**：抽屉内仅提示「即将上线」，不提交。

---

### 4.4 物流核实

#### `manual_verify`（人工核对单号）

**目标动线**：表单录入核实结果 → 写 `exprs_no` / 物流状态 → 若确认虚假发货转 `to_exception`。

#### `intercept_logistics`（物流拦截）

**目标动线**：调用 WMS/物流 API 发起拦截 → 跟踪拦截结果 → 同步客户与逆向单状态。

---

### 4.5 逆向 / 售后

处置前须确认 **责任归属**（采购 / 商家 / 用户），写入 `reverse_responsibility` 回库：

| 归属 | 典型场景 |
|------|----------|
| 商家责任 | 货不对板、瑕疵、卖家发错颜色尺码 |
| 采购责任 | 客户要 XL 采购单为 M 等下错规格 |
| 用户责任 | 无质量问题主动退货（买错、不想要） |

| action_key | 目标动线摘要 |
|------------|--------------|
| `approve_return` | 同意退货 → 退款流程 → 更新逆向状态 |
| `reject_return` | 驳回 + 理由 → 站内信客户 → 留审计 |
| `approve_exchange` | 同意换货 → 创建补发/换货子单 |
| `contact_supplier` | 卖家侧换货/补发协调（长程任务或工单） |
| `warehouse_inspect` | 仓务验货任务 → 上传凭证 → 驱动退款/换货 |
| `warehouse_repack` | 仓务重打包出库 |
| `partial_refund` | 部分退款方案 → 财务确认 → 写回 |
| `retain_customer` | 站内信挽留 + 预计到达时间 |

---

### 4.6 非业务 / 元操作

| action_key |  Intended | 当前问题 |
|------------|-----------|----------|
| `snooze` | 写 snooze_until，到期重新出信号 | 仅删除信号 |
| `acknowledge` | 标记已读，移入观察列表 | 仅删除信号 |
| `escalate` | urgency +1（与卡片「升级」一致） | 已统一 |
| `contact_seller_ai` | 跳转采购助手 + 信号标处理中 | 已接助手 |

---

## 5. Agent 决策域

分组规则：`src/lib/tasks/decision-groups.ts`

| 分组 key | 标签 | 纳入条件 | 页内动作 | 目标动线 | 当前 |
|----------|------|----------|----------|----------|------|
| **category_mapping** | 品类映射待确认 | 商品 `category_status` ∈ pending/mapping/needs_review/failed | **复核映射** | 映射写商品 + 宽表 HS 字段 | **真实** |
| **inquiry_1688** | 询盘可查询 | 任务 ready/completed | 一键查询 | CLI 查回复 → 任务 completed | **真实** |
| **sourcing_inquiry** | 寻源可查看 | 任务 ready | 查看报价 | 1688 外链；未来拉回报价 | 只读 |
| **order_followup** | 催单待跟进 | needs_review/completed | 刷新/看订单 | 牛顿轮询 → 人读回复 | **真实** |
| **newton_agent** | 智能咨询待补充 | needs_review | 刷新/看订单 | 补充信息或采纳回复 | **真实** |
| **auto_release** | 自动放行待复核 | needs_review | 去复核 → `/ai-decisions` | 确认/标记异常 → 写审计 | **内存** |
| *(未展示)* **supplychain_inquiry** | 供应链询盘 | 任务 ready | — | 应加入 decision-groups | 任务中心有，指挥中心无 |

### 5.1 自动放行条件（Agent 审计）

**采购准入** `procurement_pass`：channel, sku, category, moq, margin, price_note, stock, fields  
**支付确认** `payment_pass`：purchase_order, amount_cover, price_pending, pay_status, risk_tag  

**目标动线**：复核确认 → 写放行记录 + 推进 `ord_line_stat`；标记异常 → 回退阶段 + 生成 RiskSignal。

**当前**：`AgentReleaseMonitor` 仅改前端 state。

---

## 6. 建议实施顺序（接真实订单）

| 优先级 | 项 | 理由 |
|--------|-----|------|
| P0 | 定义 `DispositionWritePort` + action 注册表 | 所有订单点击的统一出口 |
| P0 | `request_topup` 全动线（站内信 + 账单 + BD 超时） | 待支付最高频阻塞 |
| P0 | `to_exception` 工单创建 + 异常态写回 | 多信号共用 |
| P1 | `urge_ship` / 催单与订单阶段门禁统一 | 已订购 vs 已发货规则 |
| P1 | `SKU_MISMATCH` / `MOQ_VIOLATION` 与采购准入联动 | 待采购核心 |
| P1 | 自动放行复核写库 | 与 Agent 放行闭环 |
| P2 | 逆向 action 组（拦截/验货/退款） | 依赖 WMS/逆向 API |
| P2 | snooze / acknowledge 持久化 | 避免刷新复活信号 |
| P3 | stage-subcategories 扩展信号 + 检测规则 | 清关、缺货等 |

---

## 7. 技术债清单（调整框架前先修）

1. **`submitDisposition`**：除 snooze/acknowledge/escalate/contact_seller_ai 外仍删信号 — 应改为「已派发动作，等待回调关闭」
2. **`target_route`** 未使用 — 可映射到 WritePort handler 或深链

已修（2026-07-05）：

- ~~抽屉「提升优先级」删信号~~ → 与卡片「升级」统一走 `escalateSignal`
- ~~`contact_seller_ai` 仅提示「即将上线」~~ → 跳转采购助手并标记信号处理中
- ~~`requires_confirm` 无二次确认~~ → 抽屉内 danger 动作两步确认

---

## 8. 附录：action_key 速查

| action_key | 中文标签（常见） | 动作域 |
|------------|------------------|--------|
| request_topup | 发起补款 | 支付 |
| price_hold | 挂起等待改价 | 支付 |
| manual_confirm | 人工确认后继续 | 通用 |
| to_exception | 提交异常工单 | 通用 |
| change_seller | 更换卖家 | 采购 |
| urge_ship | 催发货 | 跟单 |
| contact_seller_ai | 智能联系卖家 | 跟单 |
| manual_verify | 人工核对单号 | 物流 |
| intercept_logistics | 物流拦截 | 逆向 |
| approve_return | 同意退货 | 逆向 |
| reject_return | 驳回退货 | 逆向 |
| approve_exchange | 同意换货 | 逆向 |
| contact_supplier | 联系卖家 | 逆向/跟单 |
| warehouse_inspect | 安排验货 | 仓务 |
| warehouse_repack | 重新打包发出 | 仓务 |
| partial_refund | 部分退款 | 逆向 |
| retain_customer | 联系客户挽留 | 逆向 |
| snooze | 延后提醒 | 元 |
| acknowledge | 标记已读 | 元 |
| escalate | 提升优先级 | 元 |

---

*文档版本：2026-07-05 · 与 `derive.ts` SIGNAL_TEMPLATES 15 种信号对齐*
