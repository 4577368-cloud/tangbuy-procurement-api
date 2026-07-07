# 1688 开放平台接入（gw.open.1688.com / param2）

官方 SDK 只有 Java/PHP/.Net，本项目为 Node/TS + Python，因此**自研实现 param2 协议**（与官方 `com.alibaba.openapi.client` 的 `ApiExecutor` 等价），无需依赖 Java SDK。

## 实现文件

| 文件 | 职责 |
|------|------|
| `scripts/alibaba_open_cli.py` | param2 签名 + 通用调用 + OAuth（authorize/exchange/refresh）+ token 落盘 |
| `src/lib/agent/alibaba-open-cli.ts` | Node 桥接（`status / authorizeUrl / exchangeCode / refresh / call`） |
| `src/app/api/integrations/alibaba-open/callback/route.ts` | OAuth 回调，收 code 换 token |
| `data/integrations/alibaba-open-token.json` | access_token / refresh_token（**gitignore，勿提交**） |

## 凭证（`.env.local`，已被 gitignore）

```bash
ALIBABA_OPEN_APP_KEY=...
ALIBABA_OPEN_APP_SECRET=...
ALIBABA_OPEN_GATEWAY=https://gw.open.1688.com
ALIBABA_OPEN_REDIRECT_URI=http://localhost:3000/api/integrations/alibaba-open/callback
ALIBABA_OPEN_ACCESS_TOKEN=   # 一般留空，授权后由 token 文件提供；此处可手工覆盖
```

## 签名协议（param2，官方）

- URL：`{gateway}/openapi/param2/{version}/{namespace}/{apiName}/{appKey}`
- 签名因子：`signPath + 排序拼接(key+value)`，其中 `signPath = param2/{version}/{namespace}/{apiName}/{appKey}`（无 `/openapi/`、无前导 `/`）
- 算法：`HMAC-SHA1(appSecret, 签名因子)` → **大写十六进制**，参数名 `_aop_signature`
- 系统参数：`_aop_timestamp`（13 位毫秒）、`access_token`（多数接口必带）、`_aop_signature`
- 排序：除 `_aop_signature` 外全部参数按 key ASCII 升序，空值剔除

> 注：部分第三方代理商用 Base64 或 MD5+首尾 secret，那是非官方方案，本实现按官方 SDK 用 HEX。`sign_test` 同时输出 HEX/BASE64 便于与真实 SDK 结果比对，若官方环境校验为 Base64，改 `sign_param2(..., "base64")` 即可。

## 使用

```bash
# 自检（确定性，无需网络）
npm run open1688:status
python3 scripts/alibaba_open_cli.py sign_test

# 1) 生成授权链接（浏览器打开，用 1688 账号授权）
npm run open1688:auth
# 2) 授权后回调到 /api/integrations/alibaba-open/callback，自动换取并保存 token
#    或手工：python3 scripts/alibaba_open_cli.py exchange_code --code <CODE>
# 3) 通用调用（示例）
python3 scripts/alibaba_open_cli.py call \
  --namespace com.alibaba.product --name alibaba.product.get \
  --params '{"productId":"610947572360"}'
```

TS 端：

```ts
import { alibabaOpenCall } from "@/lib/agent/alibaba-open-cli";
const res = await alibabaOpenCall({
  namespace: "com.alibaba.agent",
  name: "newtoncloud.task.create",
  params: { /* ... */ },
});
// res.data.result 为接口原始 JSON
```

## 消息推送（已接收端，已用真实报文联调）

已订阅 `AUTHORIZATION_SUCCESS` / `AUTHORIZATION_CANCEL`。**真实格式**（2026-07 控制台测试推送实测，非文档假设）：

| 项 | 值 |
|----|----|
| 回调地址（登记到控制台） | `{站点域名}/api/integrations/alibaba-open/message`（生产需 HTTPS） |
| 通道 | 控制台「日常消息通道」选 **httpcallback**（低频授权消息够用；websocket 用于高频业务消息） |
| 推送方式 | POST，`Content-Type: application/x-www-form-urlencoded` |
| body | `message=<URL编码的JSON信封>` & `_aop_signature=<签名>` |
| 信封 | `{ data:{ appKey, loginId, memberId, userId, openUid, subAuth, reason?, refreshToken?, ... }, type, msgId, gmtBorn, userInfo }`，**`type` 在信封顶层** |
| 验签 | **param2**：除 `_aop_signature` 外的参数按 key 排序拼 `key+解码value`（即 `"message"+解码JSON`），`HEX(HMAC-SHA1(base, AppSecret))` 大写，与 `_aop_signature` 比对 |
| 回执 | 返回 HTTP 200 + 文本 `success` |
| 落地 | 全部消息追加到 `data/integrations/alibaba-open-messages.jsonl`（gitignore） |
| 撤销守卫 | 仅当「验签通过 + 信封 `data.appKey` == 本应用 AppKey」的 `AUTHORIZATION_CANCEL` 才标记 token 失效（避免测试样例的占位 appKey 误伤真实 token） |

实现：`src/lib/integrations/alibaba-open/messages.ts`（`parsePushBody` / `signPush` / `verifyPushSignature` / `classifyMessage` / `getMessageAppKey`）、`.../store.ts`（token/日志）、`src/app/api/integrations/alibaba-open/message/route.ts`（接收）。

已联调：cloudflared 隧道 → 本地 dev，控制台测试推送（`AUTHORIZATION_CANCEL`）落盘 `verified:true`、`kind:AUTHORIZATION_CANCEL`、回 `success`；占位 appKey 的取消消息不触发 token 撤销。

> ⚠️ 之前基于文档假设的 `Authorization` 头 + `HMAC-SHA256(AppKey+body)` 已废弃，真实为 body 内 `_aop_signature` + `HMAC-SHA1` param2。

## 待提供 / 待确认（阻塞项）

1. **消息回调地址登记**：把 `{站点域名}/api/integrations/alibaba-open/message` 填到控制台并「验证」（生产需公网 HTTPS + OV/EV 证书；localhost 通常无法通过控制台验证）。
2. **方案订购 + API 权限**：控制台订购方案、勾选目标 API 权限（商品详情/运费/`newtoncloud.task.*`）。
3. **具体 API 名单**：从方案的「API 及消息列表」给出 `namespace + apiName`，即可在 `alibaba-open-cli.ts` 上加薄封装并真调（签名与鉴权已通用、已验证）。

## 牛顿云 task 接口（已封装，`com.alibaba.agent` / v1）

薄封装见 `src/lib/integrations/alibaba-open/newton-task.ts`：

| 接口 | 入参 | 关键返回 |
|------|------|----------|
| `newtonTaskCreate` | `message`(必), `sessionId?`, `taskId?` | `taskId / sessionId / status` |
| `newtonTaskGet` | `taskId` | `status`；非终态 `content`(流式)；终态 `messages[]` |
| `newtonTaskList` | 无 | `data: taskItem[]` |
| `newtonTaskKill` | `taskId`, `reason` | `killed` |

状态机（END/KILL 为终态）：`INIT → RUNNING → WAIT_SKILL/WAIT_USER → END/KILL`。轮询 `get` 到终态后读 `messages`。

## 任务中心接入（面向采购员）

牛顿云长程咨询走 **采购助手对话**，由意图路由决定通道；**任务中心只负责承载与追踪**长程任务，不再有独立"新建咨询"表单。

**创建（在 agent 对话里，由意图分辨通道）**
- 采购助手（统一对话）新增工具 `newton_consult(message)`，归属 skill `newton-cloud`（"智能咨询"，`status: ready`）。
- 路由（见 `unified-assistant.ts` 系统提示）：明确关键词/链接/图 → 即时 `product_*`；模糊寻源要平台报价 → `procurement_inquiry`；**开放式"帮我搞定"或对具体商品链接问价/MOQ → `newton_consult`（长程异步）**。
- 底部意图标签新增「咨询」（`AgentIntent = "consult"`）。
- LLM 调 `newton_consult` → `orchestrator.executeToolByName` 调 `newtonTaskCreate` → `registerTaskFromTool` 落一条 `newton_agent` 任务。对话内即时提示"已发起、去任务中心看"，**本轮不返回最终结论**。
- **催单同源**：`order_inquiry_send(order_id, question)`（skill `order-followup`）也改走 `newtonTaskCreate`（A 层长程任务），orchestrator 把订单号+问题拼成 message，让牛顿云 `1688_supplychain_order_inquiry` 代问商家；`registerTaskFromTool` 落一条 `order_followup` 任务（携带 `newton_task_id`），**商家回复会被带回任务中心**。B 层 `NewtonOrderBatchInquiry`（只发不收）已停用、保留备用。

**追踪（任务中心 `/tasks`）**
- **进度**：选中未完结的长程任务（智能咨询 / 催单，判定依据 payload 带 `newton_task_id`）自动每 6s 轮询 `get`，「处理中」带脉冲；非终态展示流式 `content`；可手动「刷新进度」或「终止」。
- **催单展示**：`order_followup` 复用牛顿气泡区，标题「问商家 / 商家回复」；徽章仍为「催单」，状态 `等待商家 → 商家已回复`。
- **自动推进（无需点开）**：
  - 前台：任务中心开着且有进行中长程任务时，每 15s 调 `POST /api/tasks/refresh-active` 批量刷新，列表与「进行中/已完结」统计自动更新；选中任务另每 6s 快刷。
  - 服务端：`src/instrumentation.ts` 启动进程内定时器（`src/lib/tasks/auto-refresh.ts`），即使没人打开任务中心也在后台推进；间隔 `TASK_AUTO_REFRESH_MS`（默认 30000，设 0 关闭）。与 API 同进程共享 `globalThis` 内存任务库。**注意**：内存任务库进程重启即失（未落库），多实例部署不共享——需持久化时改存 DB。
- **结果**：终态 `messages[]` 以助手气泡渲染（复用 `MarkdownLite`，支持列表/加粗）。
- **积分不足**：命中「积分…不足/充值」时给出友好提示（引导充值），不暴露原始报错。
- **状态映射**：`INIT/RUNNING/WAIT_SKILL → 进行中`、`WAIT_USER → 待补充`、`END → 已完成`、`KILL → 失败`。

相关文件：
- 工具执行：`src/lib/agent/orchestrator.ts`（`newton_consult`）
- 技能/路由：`src/lib/agent/skills/registry.ts`（`newton-cloud`）、`unified-assistant.ts`、`agent-context.ts`（`consult` 意图）
- 任务库：`src/lib/tasks/task-store.ts`（`registerTaskFromTool` 落 `newton_agent`；`refreshNewtonAgentTask / killNewtonAgentTask / refreshTaskById`）
- API：`POST /api/tasks/[id]/refresh`（按类型分派轮询）、`POST /api/tasks/[id]/kill`（终止）
- UI：`src/components/tasks/TaskCenter.tsx`（`NewtonAgentSection` 结果渲染，无独立创建表单）

> 说明：任务当前存于内存态任务库（与既有询盘/放行任务一致）；`newton_task_id` 亦可通过 `newtoncloud.task.list` 恢复。

## 已就绪（已生产联调）

- ✅ AppKey/AppSecret 配置、param2 签名（HEX，自检通过）
- ✅ access_token（用户 `juxinggou` 已授权，写入 token 文件，`status` 显示已授权）
- ✅ 消息推送接收端 + 验签 + 授权取消处理（本地端到端验证通过）
- ✅ **`newtoncloud.task.list` 真实调用生产网关成功**（`success:true`，签名+token 全通）

> 联调命令：`python3 scripts/alibaba_open_cli.py call --namespace com.alibaba.agent --name newtoncloud.task.list`
