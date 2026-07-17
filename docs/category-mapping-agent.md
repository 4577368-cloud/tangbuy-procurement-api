# 品类映射 Agent（子 Agent）

## 业务目的

待采购订单中，若商品**首次采购**、数据库无 HS 映射，发往海外时需准确的海关编码与申报品类，否则关税计算错误。

本 Agent 是**自动放行 / 自动下单**主流程中的子环节：产出 6 个报关字段 → 人工或规则校验 → 写入商品 ID，供国际发货使用。

## 目标字段（与业务系统表单一致）

| 字段 | 数据源列 |
|------|----------|
| 分类中文名 | `cn_name` |
| 分类英文名 | `en_name` |
| 分类编号 | `cid` |
| 海关编码 | `hs_code` |
| 中文描述 | `dec_cn_name` |
| 英文描述 | `dec_en_name` |

## 训练数据

| 文件 | 行数 | 用途 |
|------|------|------|
| `all_hscode_category.xlsx` | 25,583 | 全量 HS 类目树 + 多语言名称 |
| `历史设置类目信息商品记录.xlsx` | 47,076 | 历史人工/运营已确认的商品→category_id |

构建命令：

```bash
npm run build:category-data
```

输出至 `data/category/`（含 `catalog-search-index.json` 供 Node 侧 HS 搜索）。

## 决策模型（2026-07 重构）

不再使用单一「融合分 ≥85%」规则。决策字段 `decision`：

| decision | 含义 |
|----------|------|
| `history_hit` | 1688 goods_id 在 Excel 历史表命中 → 标题相关可自动；**可疑则待复核** |
| `local_item_mapped` | 本地映射 **或人工复核覆盖历史** → 标题相关可自动 |
| `semantic_agreement` | 标题语义词与图片理解一致，单一强候选 → 可自动推荐 |
| `ambiguous_semantics` | 标题含多个品类词 → 6 维加权置信度 + 分离度（维度领先/分差）决定 agent 自动或人工选 |
| `manual_suggested` | 弱匹配或需人工搜索 HS |

**历史映射**：Excel goods_id 为门闩；人工确认/纠正后写入 `goods-id-soft` 硬覆盖，下次 suggest **优先用新类目**，不再捞旧历史。  
**平台类目**：仅当 1688 回传 `source_category_hint` 时作为辅助信号。  
**图片理解**：关键词并入 `matched_keywords`，与标题一致时提升 `semantic_agreement` 概率。

识图流程（多模态 LLM，`.env.local` 配置）：

1. 视觉模型描述商品 + 提取品类关键词  
2. 关键词传入 Python `suggest --vision-keywords`  
3. 多义候选时可选视觉重排  

## HS 人工搜索

`GET /api/category-mapping/search?q=凉鞋` → `catalog-search.ts` 读预构建索引，<500ms 返回；选中后自动填入 6 个报关字段。

## 接入点

- **Agent 对话** Skill：`category-mapping` → 工具 `category_map_suggest`
- **商品中心** → `POST /api/category-mapping/suggest`（与 Agent 同契约）
- **Agent 审计** → 品类映射 Tab
- **校验 UI**：「标题意图匹配」「非禁运受限品类」
  - 校验看**申报意图**而非字面完全一致：同义词群（如 双肩包/背包/书包、旅游/旅行）、复合意图（标题「旅游+背包」↔ 类目「旅行包」）
  - 优先采用 Agent 的 `matched_keywords` / `signal_scores.title` / 识图一致词

## 写回

### 本地

确认后写入 `data/products/center.json` 与 `data/category/local-mappings.json`。

### Tangbuy Admin（已接入）

复用 `TANGBUY_ADMIN_TOKEN`，与订单读接口相同。

| 步骤 | 接口 | 说明 |
|------|------|------|
| 读现状 | `POST /resource/goodsCategory/listByGoodsIds` | body: `{"goodsIds":["1688_offer_id"]}`，返回 `hsCodeDTO` |
| 写回 | `POST /order/changeItemCategory` | body: `{"ids":["TI…"],"cid":50010159,"updateGoodsCategory":true}` |

**可信判定**（跳过 AI，直接采纳 Admin）：

- `categoryId` / `hsCodeDTO.cid` 有效
- 类目名**不是**「其它」「其他」「待映射」等占位
- 订单行 `is_need_cfm = 0`，且 `hsCodeDTO.needConfirm = 0`

**流程**：订单同步 → 需映射 → `listByGoodsIds` → 不可信则 AI 预估 → `confirm_product_mapping` → `changeItemCategory`（`ids` = `ord_line_no`）。

实现：`app/integrations/tangbuy_admin/category_api.py`、`app/services/category_mapping/admin_sync.py`、`admin_writeback.py`。

配置：`rules.admin_category_writeback`（默认 true；false 时仅写本地）。

宽表字段 payload 仍由 `categoryWriteBackFromHs()` 组装；当前真实 WritePort 为 Admin `changeItemCategory`。
