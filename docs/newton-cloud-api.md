# 牛顿云 / ClawHub API

## 当前接入方式

**ClawHub 网关直调（B 层）** — 已跑通，现阶段继续用此方式扩展。

- 网关：`https://skills-gateway.1688.com`
- 鉴权：[clawhub.1688.com](https://clawhub.1688.com/) AK
- CLI：`scripts/newton_cli.py` + 各官方 Skill CLI

| Agent Skill | 网关 API | 状态 |
|-------------|----------|------|
| 1688 智能选品 | `/api/find_product/1.0.0` | ✅ 商品卡片 UI |
| 选品比价 | `/api/find_product/1.0.0` | ✅ 商品卡片 UI |
| 1688 寻源询盘 | `/api/1688_procurement_digital_human_tool/1.0.0` | ✅ |
| 催单 | ~~`/api/NewtonOrderBatchInquiry/1.0.0`~~ → 改用 A 层 `newtoncloud.task.*` | ✅ 长程任务，商家回复带回任务中心（B 层实现保留备用） |
| 1688 商品询盘 | 牛顿云 A 层 `1688-supplychain-procurement` | 📋 待白名单接入（不用遨虾） |
| 牛顿云 task（A 层） | `com.alibaba.agent:newtoncloud.task.*` | ✅ 已接入并生产联调（create→get→END 出结果），见 `alibaba-open-api.md` |

## 后续计划

开放平台 param2 通用客户端已就绪（见 [`alibaba-open-api.md`](./alibaba-open-api.md)：`scripts/alibaba_open_cli.py` + `src/lib/agent/alibaba-open-cli.ts`），签名/鉴权/OAuth 已打通。等 **牛顿云白名单 + 方案权限 + access_token** 就绪后，A 层 `newtoncloud.task.*` 直接用 `alibabaOpenCall({ namespace:"com.alibaba.agent", name:"newtoncloud.task.create", ... })` 调用即可。

按 PDF 接入 **A 层** `newtoncloud.task.create / .get`：

- **商品询盘**（对链接问 MOQ/价格）→ `1688-supplychain-procurement`
- **找品+询盘一体** → 同上
- 催发货长程任务也可统一到 A 层 `1688-supplychain-order-inquiry`（当前 B 层 `NewtonOrderBatchInquiry` 已可用）

📄 [`牛顿云-开放API使用说明.pdf`](../牛顿云-开放API使用说明.pdf)  
📋 [`procurement_newton_api_integration_v1.md`](../../procurement_newton_api_integration_v1.md)

## 积分 / apiKey（长程任务必读）

`newtoncloud.task.*` 的积分**不走**开放平台「应用流量 5000 次/天」，而是走**牛顿云网站侧订阅账号**（juxinggou，登录赠送 10,300 起）的积分池。

- **必须**在 `newtoncloud.task.create` 里带 **`apiKey`** 参数（牛顿云网站的 apiKey），把消耗绑定到该订阅账号。
- **不带** apiKey 时 create 会成功返回 `taskId`，但 `get` 时任务落到空积分池，报 **「积分余额不足，请充值后继续使用。」**——这是最初误判为需充值的根因。
- 配置：`.env.local` 的 `ALIBABA_NEWTON_APIKEY`；由 `newtonTaskCreate()`（`src/lib/integrations/alibaba-open/newton-task.ts`）自动注入，业务代码无需感知。
- 校验：`isNewtonApiKeyConfigured()`。

## 国内运费（重要）

牛顿两层 API **都不返回结构化运费**：

- B 层 `find_product` 只回 `currentPrice / soldOut / company / yxIndex …`，无运费/邮费/收货地字段。
- A 层开放 API 只有 `newtoncloud.task.*` 长程任务 + skill-code（`1688-supplychain-procurement` 等），无「offerId + 收货城市 → 运费」的同步查询接口。

因此**锁定城市实时取真实运费无法通过牛顿 API 完成**。真实运费只能走**询盘**（A 层 `1688-supplychain-procurement`，如「发到深圳运费/能否包邮」，异步约 10 分钟），且需开放平台白名单 + AppKey 就绪。

当前落地（B 层阶段）：

1. **停止伪造运费**：入库不再用 `unitPrice × 12%` 估算，运费默认 `null`（待确认）。`estimate1688Shipping()` 仅保留供人工参考。
2. **可空 + 来源标记**：`ProductCenterItem.original_shipping: number | null`，`shipping_source: unknown | manual | inquiry | api`，`shipping_to_city`。
3. **人工填写**：商品中心「运费」列可直接填真实运费，填后重建阶梯价与 Tangbuy 运费（`PATCH /api/products/:id { action:"set_shipping", shipping, to_city }`）。
4. **询盘回写（待白名单）**：A 层就绪后，锁定收货城市发运费询盘任务，回写 `source=inquiry` 的真实运费。

## 本地调试

```bash
cd procurement-demo  # 产品仓库目录名，npm 包名 tangbuy-procurement
AGENT_WORK_ROOT=$(pwd) python3 scripts/newton_cli.py status
AGENT_WORK_ROOT=$(pwd) python3 scripts/newton_cli.py text_search --query "蓝牙耳机" --limit 5
```
