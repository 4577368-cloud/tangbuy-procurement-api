# 配置中心 & 权限（RBAC）

配置中心承载业务参数与角色权限，均为**应用层新实体**（数仓/业务库尚无表）。当前用本地文件持久化，后续按契约换 Port 对接库。

## 角色

| 角色 | id | 定位 |
|---|---|---|
| BD（运营） | `bd` | AI 助手运营（寻源/催单/图搜/加入大店）+ 商品/订单只读 |
| 采购员 | `buyer` | 催单 + 订单卡点处置等人工判断操作；不做加入大店 |
| 管理员 | `admin` | 全部权限，含配置中心与权限分配 |

## 权限模型（`src/lib/auth/permissions.ts`）

- 结构：**大类（category）→ 子项（item）**；每个子项按需暴露 `view` / `edit` 两种动作。
- 授权：`PermissionMatrix = Record<Role, Record<itemKey, {view?, edit?}>>`。
- 勾选大类 = 勾选其下全部子项的全部可用动作（配置中心 UI 的「全选（含下级）」）。
- 默认矩阵 `DEFAULT_MATRIX` 可在配置中心编辑并持久化，缺失项回落默认（`mergeMatrix`）。

大类：采购助手 / 商品中心 / 订单中心 / 任务中心 / 配置中心。

## 业务参数（`src/lib/config/business-config.ts`）

毛利阈值(%) · MOQ 规则(启停+默认起订量) · 未发货超时(小时) · AI 置信度阈值(0-1) · 规则启停清单。写入经 `normalizeBusinessConfig` 归一化防越界。

## 用户目录（`src/lib/auth/users.ts`）

- 种子用户：BD = jody/lydia/kevin；采购员 = 孙玉田；管理员 = admin/雪芝。
- 登录：**账号 + 密码**（模拟阶段默认密码 `tangbuy123`）。
- 唯一键 = `account`（使用人）。后续对接库时，以账号字段分配角色，替换种子即可。

## 会话（`src/lib/auth/session.ts`）

签名 httpOnly cookie（`tangbuy_session` = `account.HMAC`）。密钥 `AUTH_SESSION_SECRET`（缺省用 dev 值）。
- `getCurrentUser()` / `getAuthContext()` / `userCan(user, itemKey, action)`。
- 客户端：`SessionProvider` + `useSession()`，提供 `can(itemKey, action)`、`canModule(category)`、`isAdmin`、`logout`。

## 落地管控点（已接）

- 导航：侧边栏按 `canModule` 显隐（配置中心默认仅管理员）。
- 加入大店：`ProductCardActions` 按钮 + `POST /api/products` 服务端 `product.add_to_store/edit` 双重校验。
- 订单卡点处置：`OrderDetailPanel` 主操作按钮按 `order.disposition/edit` 显隐。
- 配置中心：`config.business/view|edit`、`config.permission/view|edit`；`GET/PUT /api/config` 服务端校验。
- **Agent 工具（`POST /api/agent/chat`）**：按当前角色对工具做**双层**控制——
  1. 过滤：只把有权限的工具暴露给 LLM（无权限工具 LLM 看不到、不会调）；
  2. 拦截：即便被误调，`executeToolByName` 前二次校验，无权限直接返回「请联系管理员开通」，不执行、不落任务。
  映射见 `unified-assistant.ts` 的 `TOOL_PERMISSION`（选品/比价→`assistant.image_search`，寻源/询盘→`assistant.sourcing`，催单→`assistant.order_followup`，智能咨询→`assistant.consult`，品类映射→`product.category_mapping`）。
- 商品编辑 / 品类映射写回：`POST /PATCH /api/products/[id]`（映射决策→`product.category_mapping/edit`；改运费等商品信息→`product.catalog/edit`）、`POST /api/category-mapping/feedback`（`product.category_mapping/edit`）。
- 任务终止：`POST /api/tasks/[id]/kill`（`task.control/edit`）。

## 持久化 & 接库

- 文件：`data/config/config-center.json`（已 gitignore，含模拟密码/角色分配）。
- 预留 Port（`integration-contract.ts`）：`ConfigReadPort` / `ConfigWritePort` / `UserDirectoryPort`。
- 目标表（待建）：`cfg_business_param`、`cfg_role_grant`、`sys_user_role`。接库时换 Port 的 HTTP 实现，UI 契约不变。

## 待接（后续）

- 真实用户打通后按 `account` 分配角色，去掉种子用户；把本地文件持久化换成 Port 的 HTTP 实现。
- 会话升级为强鉴权（当前签名 cookie 为模拟阶段方案）。
