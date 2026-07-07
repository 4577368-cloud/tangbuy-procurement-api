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
| `history_hit` | 1688 goods_id 在历史表命中 → **100% 自动** |
| `local_item_mapped` | Tangbuy item_id 或本地 `local-mappings.json` 已有映射 → **100% 自动** |
| `semantic_agreement` | 标题语义词与图片理解一致，单一强候选 → 可自动推荐 |
| `ambiguous_semantics` | 标题含多个品类词 → 6 维加权置信度 + 分离度（维度领先/分差）决定 agent 自动或人工选 |
| `manual_suggested` | 弱匹配或需人工搜索 HS |

**历史映射**：仅 goods_id / 本地 item 映射为二元门闩（0/1），不参与百分比融合。  
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

确认后写入 `data/products/center.json` 与 `data/category/local-mappings.json`；`categoryWriteBackFromHs()` 组装宽表写回 payload（对接真实库前为 stub 日志）。
