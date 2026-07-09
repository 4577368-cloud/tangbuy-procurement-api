# Tangbuy Procurement API

Tangbuy 智能采购履约系统 — **独立 Python 后端**，对接正式环境数仓与公司基础设施。

## 分层架构

```
app/
├── main.py                 # 应用入口、CORS、生命周期
├── api/deps.py             # HTTP 依赖（认证、权限）
├── routers/                # HTTP 路由层（仅编排，不写业务）
├── auth/                   # 会话、用户、RBAC
├── config/                 # 配置中心读写
├── services/               # 领域服务（任务、商品、品类、审计）
│   ├── agent/              # 采购 Agent 框架（编排、工具、路由、LLM）
│   ├── tasks/
│   ├── products/
│   ├── category_mapping/
│   └── skill_audit/
├── integrations/           # 外部系统适配（牛顿、1688 Open、Skill CLI）
├── core/                   # 配置、路径
scripts/                    # CLI 与品类数据构建（可独立部署）
data/                       # 运行时数据（接库前本地持久化）
workspace/                  # AK / OAuth 密钥目录
docs/                       # 集成与字段契约文档
```

### Agent 框架（`app/services/agent/`）

| 模块 | 职责 |
|------|------|
| `orchestrator.py` | 多轮 LLM + 工具调用主循环 |
| `registry.py` (`skills.py`) | Skill 定义、工具 schema、权限映射 |
| `routing.py` | 确定性路由（催单、寻源、选品标签） |
| `tools.py` | 工具执行器（牛顿、寻源、品类、供应链） |
| `llm.py` | OpenAI 兼容 LLM 客户端 |
| `followup.py` | 催单补全与网关兜底 |

任务登记：`services/tasks/register.py` — 工具成功后写入 `data/agent/tasks.json`。

## 启动

```bash
cd tangbuy-procurement-api
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local   # 配置 LLM、1688、CORS
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 线上部署（Render）

- Blueprint：仓库根目录 [`render.yaml`](../render.yaml)
- 步骤与环境变量：[`docs/deploy-render.md`](docs/deploy-render.md)
- 前端（Vercel）通过 `API_PROXY_TARGET` 指向 Render 服务 URL

## 环境变量

| 变量 | 说明 |
|------|------|
| `AUTH_SESSION_SECRET` | 与前端共享的 Cookie 签名密钥 |
| `BACKEND_CORS_ORIGINS` | 前端源（Vercel 域名），逗号分隔 |
| `TANGBUY_ADMIN_TOKEN` | Admin 订单读接口 Token |
| `TANGBUY_PORTAL_TOKEN` | Portal 商品详情 Token |
| `LLM_MODEL_*` | 采购助手 LLM |
| `AGENT_WORK_ROOT` | 数据根目录（默认项目根） |

## API 清单

- `GET /api/health`
- `/api/auth/*` — 登录、会话
- `/api/tasks/*` — 任务中心
- `/api/agent/*` — 采购助手、Skill 审计
- `/api/products/*` — 商品中心
- `/api/category-mapping/*` — HS 品类映射
- `/api/config` — 配置中心
- `/api/data-center` — 指标聚合
- `/api/integrations/alibaba-open/*` — 1688 OAuth / 推送
- `/api/orders/*` — 订单中心、处置、补款
- `/api/evolution/*` — AI 自进化引擎

## 接正式环境

1. 实现 `docs/data-integration.md` 中的 `OrdLineReadPort` / `WritePort` HTTP 适配，替换 `services/` 内文件读写。
2. 任务、商品、配置迁移到公司业务库表。
3. `scripts/` 可保留为 Sidecar 或逐步内联为 `integrations/` 包。

原单体仓库 `procurement-demo` 保留作迁移参考，新开发以本仓库为准。
