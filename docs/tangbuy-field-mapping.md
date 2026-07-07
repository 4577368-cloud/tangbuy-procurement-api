# Tangbuy 字段映射（ads_ops_ord_line_rel_td）

## 原则

- **数仓 / DB 字段名**（如 `ord_line_no`、`pur_prc`）是唯一契约。
- 前端、Agent 上下文可以用不同展示名，但必须通过 `src/lib/tangbuy/` 映射层转换。
- 暂未使用的宽表字段也在 `field-catalog.ts` 登记，接库时直接启用。

## 代码入口

| 模块 | 路径 | 作用 |
|------|------|------|
| 宽表行类型 | `src/lib/tangbuy/ord-line-row.ts` | 全字段 TypeScript 类型 |
| 字段目录 | `src/lib/tangbuy/field-catalog.ts` | 字段描述 + UI/商品中心映射状态 |
| 状态枚举 | `src/lib/tangbuy/status-enums.ts` | `ord_stat` / `ord_line_stat` / `ds_ord_stat` / `pkg_stat` |
| 适配器 | `src/lib/tangbuy/app-mappers.ts` | 订单视图 ↔ 宽表、商品中心 ↔ `item_*` |
| 统一导出 | `src/lib/tangbuy/index.ts` | |

## 粒度

表：`ads_ops_ord_line_rel_td`  
粒度：`ord_line_no`（Tangbuy 内部订单子单号）

## 订单中心 UI 映射（摘要）

| UI 字段 | Tangbuy 字段 | 说明 |
|---------|--------------|------|
| `order_id` | `ord_line_no` | 子单主键 |
| `external_order_no` | `ord_no` / `out_ord_no` | 待拆分主站/三方 |
| `product_title` | `item_nm` | |
| `product.platform_order_no` | `pur_no` | 1688 采购单 |
| `quantity` | `ord_cnt` | |
| `purchase_product_amount` | `pur_prc` | |
| `purchase_shipping_amount` | `post_fee` | |
| `customer_paid_amount` | `ds_ord_amt` | 近似 |
| `queue` | `ord_line_stat` | 见 `status-enums.ts` |
| `pay_time` | `pay_time` | |

## 商品中心映射（摘要）

| 商品中心字段 | Tangbuy 字段 |
|--------------|--------------|
| `tangbuy_product_id` | `item_id` |
| `product_name` | `item_nm` |
| `original_unit_price` | `pur_prc` |
| `tangbuy_unit_price` | `prc` |
| `source_product_id` | `splr_item_id` |
| `shop_name` | `splr_shop_nm` |
| `category` / HS | `lvl1_ctgy_nm` / `cstm_hs_cd` / `dcl_*` |

## 接宽表 API 时

1. API 返回 `TangbuyOrdLineRow` 或子集，不要再造一套 camelCase 真相字段。
2. 列表队列用 `resolveOrderQueueFromOrdLine(row)` 或直接用 `ord_line_stat_nm` 展示。
3. 采购助手 URL / 上下文用 `mapOrdLineToAgentContext(row)`。
4. 新页面字段先登记 `field-catalog.ts`，再写 UI。
