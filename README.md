# claude_proxy

面向 Claude Code / Codex / Cursor 等客户端的多协议 LLM 反向代理。对外暴露 Anthropic、OpenAI Chat Completions、OpenAI Responses、Gemini 原生与 OpenAI Images 五类接口，对内统一转发到可热切换的上游供应商（默认 TeamoRouter），并完成鉴权、计费、配额与路由改写。

## 功能概览

- **多协议入口**：`/v1/messages`（Anthropic）、`/v1/chat/completions`、`/v1/responses`、`/v1beta/models/{model}:generateContent`（Gemini 原生）、`/v1/images/*`、`/v1/audio/*`。
- **协议互转**：客户端与上游协议不一致时自动桥接，例如 Anthropic → OpenAI Chat、OpenAI Chat ⇄ Responses。
- **多上游供应商**：TeamoRouter（默认）、自建 LiteLLM（`legacy`）、第三方号商（`yunxin`）。供应商、价目、密钥存在数据库里，管理接口写入后内存缓存立即刷新，**无需重启**。
- **模型路由**：支持模型别名、模型改写、按模型/用户覆盖上游，以及上游协议指定。
- **按量计费**：按上游价目表 × 汇率 × 加价倍率结算，区分文本 / 图片 / 音频 / 缓存 token，支持 service tier 与长上下文档位。
- **用量查询**：请求级用量落库（`tb_request_usage`），提供汇总、延迟分位、账户余额与花费接口。
- **任务归并**：把同会话内连续的 `tool_use` / `function_call` 归并为一个代理任务，区分进行中、完成、失败。
- **可选压缩**：Headroom 请求压缩（默认关闭，开启会改写历史工具结果并打断上游 prompt cache）。

## 目录结构

```
main.py                 FastAPI 应用：中间件管线、路由、计费落库
api/                    预留包（接口实现集中在 main.py）
settings/
  macro.py              上游地址、开关、计价常量、模型价目表
  capacity.py           按 CPU/内存自动选择并发档位
  database.py           异步 SQLAlchemy 引擎与会话
  models.py             ORM 模型（用户、密钥、价目、路由、用量、账单…）
utils/
  common_utils.py       路径识别、用量采集、请求清洗
  check_user.py         鉴权、计价查找、上游鉴权头注入
  api_key_auth.py       API Key / 手机号凭据解析
  model_route.py        按模型覆盖上游
  model_alias.py        对外模型别名
  model_rewrite.py      旧模型名改写
  model_protocol.py     模型 → 上游协议规则
  prompt_injection.py   按模型注入系统提示
  anthropic_bridge.py   Anthropic ⇄ OpenAI 协议转换
  openai_responses_bridge.py  Chat Completions ⇄ Responses
  protocol_adapt.py     通用协议适配
  cursor_adapter.py     Cursor 专用请求清洗
  vendor_catalog.py     多供应商价目目录与热刷新
  teamo_billing.py      TeamoRouter 计费
  image_billing.py      图片按张/按 token 计费
  unbilled_billing.py   未结算请求的后台补偿
  request_usage.py      请求用量读写
  agent_task.py         任务归并
  headroom_compress.py  请求压缩
  get_log.py            access / process / billing 日志
docs/                   接口与设计文档
deploy.sh               同步到远端并重启
restart.sh              本地/远端重启（优先 systemd）
gunicorn.conf.py        gunicorn 配置（可选部署方式）
claude-code-proxy.service  systemd unit 示例
```

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

复制模板并填写：

```bash
cp .env.example .env
chmod 600 .env
```

必需项：

| 变量 | 说明 |
|---|---|
| `TEAMO_API_KEY` | 上游 API Key |
| `DATABASE_URL` | `mysql+aiomysql://user:pass@host:3306/dbname` |

常用可选项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `UPSTREAM_PROVIDER` | `teamo` | `teamo` / `legacy` / `yunxin` |
| `TEAMO_UPSTREAM_BASE` | `https://api.teamorouter.com` | TeamoRouter 根地址，**不要带 `/v1`** |
| `TEAMO_HTTP_PROXY` | 空 | 直连受阻时填本机代理，如 `http://127.0.0.1:7890` |
| `TEAMO_API_KEY_FILE` | `/etc/claude_code_proxy/teamo_api_key` | 密钥文件（需 `chmod 600`），`TEAMO_API_KEY` 为空时兜底 |
| `HEADROOM_ENABLED` | `0` | 是否启用请求压缩 |
| `LEGACY_UPSTREAM_BASE` | `http://127.0.0.1:4001` | `legacy` 模式的 LiteLLM 地址 |

> 项目**未使用 python-dotenv**，代码不会自动读取 `.env`。生效方式：
> - 通过 `restart.sh` 启动 —— 脚本会加载 `.env`，已存在的环境变量优先、`.env` 只补空缺；
> - 通过 systemd 启动 —— 在 unit 中加 `EnvironmentFile=/path/to/.env`。
>
> 直接在 shell 里 `python main.py` 时，需要自行 `export` 这些变量。

### 3. 启动

```bash
# 开发
python main.py                     # 监听 0.0.0.0:4000

# 生产（systemd）
sudo cp claude-code-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-code-proxy

# 或 gunicorn（需另外安装：pip install gunicorn）
gunicorn -c gunicorn.conf.py main:app
```

验证：

```bash
curl -fsS http://127.0.0.1:4000/health       # {"status":"ok"}
```

首次启动会自动建表、灌入默认价目目录、拉取远端价目，并启动价格刷新与未结算补偿两个后台任务（多实例并发时用 MySQL 命名锁保护 DDL）。

## 使用方式

### 客户端接入

把客户端的 base URL 指向本服务即可，凭据填用户自己的 API Key（不是上游 Key）。

```bash
# Anthropic / Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=sk-xxxx

# OpenAI 兼容 / Codex
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
export OPENAI_API_KEY=sk-xxxx
```

```bash
curl http://127.0.0.1:4000/v1/messages \
  -H "x-api-key: sk-xxxx" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

鉴权头支持 `Authorization: Bearer <key>`、`x-api-key`、`api-key` 三种。

### 请求处理流程

```
读取 body
  → 识别客户端协议（anthropic / openai / responses / images / audio / gemini）
  → 应用模型别名
  → 手机号凭据拦截
  → 鉴权（tb_user_api_key 或手机号）
  → 应用模型改写
  → 解析运行时上游（按用户/模型路由规则，未命中回落全局默认）
  → 价格校验（未公布价格的模型直接 400）
  → 可选压缩 → 提示注入 → 上游模型名映射（如号商的 -hc 后缀）
  → 上游请求清洗（如 max_tokens / stream_options 归一）
  → 协议桥接（按需）
  → 注入上游密钥并转发
  → 流式收集用量 → 计费落库
```

## 接口

### 代理接口

| 路径 | 协议 | 计费 |
|---|---|---|
| `POST /v1/messages` | Anthropic | 是 |
| `POST /v1/messages/count_tokens` | Anthropic | 否（固定走 Teamo） |
| `POST /v1/chat/completions` | OpenAI | 是 |
| `POST /v1/responses` | OpenAI Responses | 是 |
| `POST /v1/embeddings` | OpenAI | 是 |
| `GET /v1/models` | OpenAI | 否（转发上游并注入别名） |
| `POST /v1/images/{generations,edits,variations}` | OpenAI Images | 是 |
| `POST /v1/audio/*` | OpenAI Audio | 是（仅 `gpt-audio*`，走 `legacy`） |
| `POST /v1beta/models/{model}:{generateContent,streamGenerateContent}` | Gemini 原生 | 是 |

不带 `/v1` 前缀的等价路径（`/messages`、`/chat/completions` 等）同样可用。

### 查询与管理接口

```bash
# 健康检查
GET  /health
GET  /headroom/stats                  # 压缩统计（每进程独立）

# 用户级（需 API Key）
GET  /v1/vendors                      # 可用供应商
GET  /v1/pricing/models               # 当前价目（含别名）
GET  /v1/usage                        # 请求用量列表
GET  /v1/usage/summary                # 用量汇总
GET  /v1/usage/latency                # 延迟分位
GET  /v1/account/balance              # 本人余额
GET  /v1/account/spending             # 本人花费
GET  /v1/tasks                        # 任务列表
POST /v1/tasks/{status,reevaluate,complete-all,delete,delete-done}

# 管理员（tb_user.is_staff = true）
GET|PUT /v1/admin/pricing             # 汇率 / 加价倍率
GET|PUT /v1/admin/vendors             # 供应商 CRUD
GET  /v1/admin/pricing/catalog        # 官方标准价目录
GET|PUT|DELETE /v1/admin/vendors/{code}/models[/{model_id}]
POST /v1/admin/vendors/{code}/{refresh,activate}
GET  /v1/admin/vendors/{code}/balance
GET|PUT|DELETE /v1/admin/model-routes
POST /v1/admin/model-routes/replace-key
GET|PUT|DELETE /v1/admin/model-rewrites
GET|PUT|DELETE /v1/admin/model-protocols
GET|PUT|DELETE /v1/admin/model-aliases
GET|PUT|DELETE /v1/admin/prompt-injections
GET  /v1/admin/runtime                # 当前运行时上游
```

完整的参数、响应结构与错误码见：

- [docs/usage-api.md](docs/usage-api.md) — 用量查询
- [docs/vendor-pricing-api.md](docs/vendor-pricing-api.md) — 多供应商价目
- [api.md](api.md) — 余额与花费
- [docs/API.md](docs/API.md) — 对外 API 设计
- [docs/concurrency-tuning.md](docs/concurrency-tuning.md) — 并发模型与调优

## 日志

按类别写入工作目录下的 `logs/`（已忽略入库），均为轮转文件：

| 文件 | 内容 |
|---|---|
| `access.log` | 每个请求的来源、模型、状态、耗时 |
| `process.log` | 启动、鉴权、转发、后台任务 |
| `billing.log` | 计费结果 |
| `billing_audit.log` | 计费审计流水 |

## 数据库

使用 MySQL + `aiomysql`，启动时自动建表。主要表：

| 表 | 用途 |
|---|---|
| `tb_user` / `tb_user_api_key` | 用户与 API Key |
| `tb_pricing_settings` | 汇率、加价倍率 |
| `tb_upstream_vendor` | 上游供应商与其密钥 |
| `tb_vendor_model_price` | 各供应商模型价目 |
| `tb_model_route` / `tb_model_rewrite` / `tb_model_alias` / `tb_model_protocol` | 路由与模型改写规则 |
| `tb_prompt_injection` | 系统提示注入规则 |
| `tb_request_usage` | 请求级用量 |
| `tb_bill` | 账单 |
| `tb_agent_task` | 归并后的代理任务 |

规则类表（route / alias / rewrite / protocol / injection）在进程内维护内存缓存，管理接口写入后立即重载。

## 计费说明

- 售价 = 官方美元成本 × `PROFIT_MULTIPLIER`（默认 1.5）× `DOR2CHY`（默认 6.9）× 折扣。
- 文本按 1M tokens；图片按张（分 1k/2k/4k 档）或按 token；音频按自有费率表。
- 价目表是**快照**，上游调价后需通过管理接口或定时刷新重新核价。
- 未结算请求由 `unbilled_billing` 后台任务补偿。

## 部署

### 同步到远端

```bash
cp .deploy.env.example .deploy.env   # 填 REMOTE_HOST / REMOTE_USER / REMOTE_DIR / DEPLOY_SSH_PASS
chmod 600 .deploy.env
./deploy.sh --dry-run                # 先看会传哪些文件
./deploy.sh                          # 同步 + 重启 + 冒烟检查
```

`deploy.sh` 会备份远端代码、按哈希增量同步、重启并验证 `/health` 与计费辅助函数。`.deploy.env`、`.env` 之外的本地配置不会入库。

### 重启

```bash
./restart.sh          # 有 systemd unit 时走 systemctl，否则回退 nohup
```

## 配置优先级

1. 进程环境变量（systemd / shell 导出）
2. `.env`（仅由 `restart.sh` 或 systemd `EnvironmentFile` 加载，只补空缺）
3. `settings/macro.py` 中的默认值

密钥解析顺序：`TEAMO_API_KEY` 环境变量 → `macro.py` 手填值 → `TEAMO_API_KEY_FILE` 指向的文件。

## 安全注意事项

- **不要把密钥写进源码**。仓库中的 `settings/macro.py`、`settings/database.py` 均为空值/占位值，真实凭据只放 `.env` 或 `TEAMO_API_KEY_FILE`。
- `.env`、`.deploy.env`、`*.key`、`*.pem`、`test_*.py`、抓包样本等已在 `.gitignore` 中忽略。
- 密钥文件权限需为 `600`，否则启动时会直接报错（`TEAMO_API_KEY_FILE must not be accessible to group/others`）。
- 密钥一旦泄露应立即轮换：改 `.env` 后重启即可，无需改代码。

## 注意事项

- 上游 base URL 必须是 API 根地址，**不能带 `/v1`**，否则启动即报错。
- 未在价目表中的模型会被拒绝（400），避免"调用成功但无法计费"。
- `legacy` 模式下 Audio 与 `gpt-audio*` 固定走 `LEGACY_UPSTREAM_BASE`，不受供应商切换影响。
- `count_tokens` 固定走 Teamo，且不套用号商的 `-hc` 模型后缀。
- 多 worker 时 `/headroom/stats` 各进程独立计数，重启清零。
- 服务按 CPU / 内存自动选择并发档位（`small` / `medium` / `desktop`），可用 `PROXY_*` 环境变量覆盖。
