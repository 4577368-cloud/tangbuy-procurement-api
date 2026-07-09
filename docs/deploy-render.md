# Render 部署指南

将 `tangbuy-procurement-api` 部署到 [Render](https://render.com)，供 Vercel 前端通过 `/api` 代理或直连调用。

## 架构

```
采购员浏览器
    → Vercel（tangbuy-procurement-web）
        → /api/* 代理
    → Render（本仓库 FastAPI）
        → Tangbuy Admin / Portal API
        → LLM 服务
```

仓库内已包含：

- 品类目录 `data/category/*.json`
- 商品中心种子 `data/products/center.json`
- 进化 / 审计等 JSONL（首次运行可写）

## 方式一：Blueprint（推荐）

1. 登录 [Render Dashboard](https://dashboard.render.com)
2. **New → Blueprint**
3. 连接 GitHub 仓库 `4577368-cloud/tangbuy-procurement-api`
4. Render 读取根目录 `render.yaml` 并创建 Web Service
5. 在创建向导或服务的 **Environment** 中补全 Secret 变量（见下表）
6. 部署完成后记下服务 URL，例如 `https://tangbuy-procurement-api.onrender.com`

## 方式二：手动创建 Web Service

| 项 | 值 |
|----|-----|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/api/health` |
| Region | Singapore（离国内访问较近，可按需改） |

## 环境变量

### 必填（否则核心功能不可用）

| 变量 | 说明 |
|------|------|
| `AUTH_SESSION_SECRET` | 与 **Vercel 前端** 相同的会话签名密钥（`render.yaml` 可自动生成） |
| `BACKEND_CORS_ORIGINS` | 前端域名，逗号分隔，如 `https://xxx.vercel.app` |
| `TANGBUY_ADMIN_TOKEN` | Admin 后台 Bearer Token（订单列表/详情） |
| `LLM_MODEL_BASE_URL` | OpenAI 兼容 LLM 地址 |
| `LLM_MODEL_API_KEY` | LLM API Key |
| `LLM_MODEL_MODEL_ID` | 模型 ID |

### 建议配置

| 变量 | 说明 |
|------|------|
| `TANGBUY_PORTAL_TOKEN` | Portal `itemGet` 商品详情补全 |
| `TANGBUY_ADMIN_BASE_URL` | 默认 `https://admin.tangbuy.cc/prod-api` |
| `TANGBUY_PORTAL_BASE_URL` | 默认 `https://www.tangbuy.cc/gateway` |
| `PRODUCT_AUTO_PIPELINE` | `true` — 入库后自动详情补全 |
| `PRODUCT_AUTO_SCAN_MS` | `0` 关闭周期扫；生产可按需开启 |

### 本地 Skill CLI（Render 上通常不可用）

以下依赖本机路径，**云端一般留空**，对应 Agent 工具会降级或跳过：

- `SKILL_1688_SOURCING_CLI`
- `SKILL_INQUIRY_1688_SCRIPT`
- `SKILL_SUPPLYCHAIN_PROCUREMENT_CLI`

1688 开放平台、AlphaShop 等若已配置 HTTP API，可在环境变量中单独填写（见 `.env.example`）。

完整字段列表见仓库根目录 `.env.example`。

## 对接 Vercel 前端

在 **tangbuy-procurement-web** 的 Vercel 项目环境变量：

```bash
API_PROXY_TARGET=https://tangbuy-procurement-api.onrender.com
NEXT_PUBLIC_API_BASE_URL=   # 留空，走同源 /api 代理
```

`AUTH_SESSION_SECRET` 必须与 Render 上 **完全一致**。

部署 Vercel 后，把真实域名写回 Render 的 `BACKEND_CORS_ORIGINS`。

## 部署后验证

```bash
# 健康检查
curl https://你的-render-域名.onrender.com/api/health

# 应返回 status: ok，admin_configured 取决于 Token 是否配置
```

浏览器：打开 Vercel 站点 → 登录 → 订单中心有数据 → 采购助手可对话。

## 数据持久化说明

| 数据 | 来源 | Render 默认行为 |
|------|------|-----------------|
| 品类目录 | Git 仓库 | 只读，随部署更新 |
| 商品种子 `center.json` | Git 仓库 | 冷启动有 78 条种子 |
| 运行时写入（任务、审计、商品更新） | `data/` 目录 | **重新部署可能丢失** |

测试阶段通常够用：种子在 Git 里，拉 Admin 订单可再次同步商品。

若需长期保留运行时写入，任选其一：

1. **Render Persistent Disk** — 挂载到 `data/` 后需一次性把仓库内 `data/` 复制进磁盘（空盘会盖住镜像内文件）
2. **MySQL（推荐终态）** — 与公司阿里云一致，见 `docs/data-integration.md`

## 常见问题

### Admin Token 失效

`/api/health` 中 `admin_configured: false`，或订单列表报错。  
在 admin.tangbuy.cc 重新复制 Token，更新 Render 环境变量并 **Manual Deploy**。

### 登录后 401 / Cookie 不生效

- 检查 `AUTH_SESSION_SECRET` 前后端是否一致
- 前端是否走同源 `/api` 代理（`NEXT_PUBLIC_API_BASE_URL` 留空）
- `BACKEND_CORS_ORIGINS` 是否包含 Vercel 域名且带 `https://`

### Render 免费档休眠

长时间无请求会冷启动，首屏可能慢 30–60 秒。测试可用 Starter 计划避免休眠。

### CORS 报错

在 Render 把 Vercel 预览域名也加入 `BACKEND_CORS_ORIGINS`（含 `https://`，逗号分隔）。

## 相关仓库

- 前端：[tangbuy-procurement-web](https://github.com/4577368-cloud/tangbuy-procurement-web)
- 数据集成契约：`docs/data-integration.md`
