# 用量查询增强设计（给后端）

> 状态：待实现  
> 关联现有接口：[`usage-api.md`](./usage-api.md) · `GET /v1/usage`  
> 目标：解决「百页以上数据时，前端无法低成本做筛选 / 汇总」的问题

---

## 1. 背景与问题

当前 `GET /v1/usage` 仅支持：

| 能力 | 现状 |
|------|------|
| 分页 | `limit` / `offset` |
| 用户范围 | 普通用户自动本人；管理员可选 `user_id` |
| 模型 / 状态 / 时间筛选 | **无** |
| 金额 / token 聚合 | **无** |

前端已知痛点：

1. **总消费**：只能循环 `offset` 拉全部分页再 `SUM(charged_cny)`。用户有一百多页时会打大量请求，打开页或统计极慢。
2. **搜索 / 筛选**：文档已写明「无时间范围、模型、status 筛选；需前端拉页后过滤」。数据一大即不可用。
3. **管理端汇总**：本页收费 / 成本 / 利润只能对本页求和，无法得到全站或指定条件下的真实汇总。

前端已临时改为「总消费按需点击统计」，但根治必须在服务端做过滤与聚合。

---

## 2. 目标

1. **筛选下沉到 SQL**：列表查询按条件过滤，`total` 为过滤后的条数。
2. **一次请求出汇总**：金额与 token 用 `SUM`，不再让前端扫页。
3. **权限与现有一致**：普通用户只能看自己；管理员可看全部或指定 `user_id`；普通用户响应不暴露 `cost_*` / `profit_*`。
4. **兼容现有调用**：不传新参数时，行为与现在 `GET /v1/usage` 一致。

非目标（本期不做）：

- 导出 CSV / 异步报表任务
- 按天 / 按模型的多维报表（可作为后续 `group_by` 扩展）
- 改变扣费写路径或 `tb_request_usage` 表结构（仅建议加索引）

---

## 3. 方案概览

| 接口 | 作用 |
|------|------|
| `GET /v1/usage` **扩展 query** | 明细列表 + 服务端筛选 + 分页 |
| `GET /v1/usage/summary` **新增** | 同一套筛选条件下的聚合统计 |

两接口共用同一套过滤语义与权限逻辑，避免列表与汇总对不齐。

建议内部抽公共函数，例如：

```text
resolve_usage_scope(caller) -> user_id 强制范围
build_usage_filters(query) -> WHERE 条件
```

---

## 4. 共用筛选参数

以下参数同时适用于 `GET /v1/usage` 与 `GET /v1/usage/summary`。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | int | 否 | 同现接口。管理员可筛；普通用户只能为自己，否则 403 |
| `model` | string | 否 | 精确匹配 `model` 字段。空 / 不传 = 不限 |
| `model_prefix` | string | 否 | 前缀匹配，如 `gpt-6` → `model LIKE 'gpt-6%'`。与 `model` 互斥，同时传时优先 `model` |
| `status` | string | 否 | `pending` \| `billed` \| `unbilled` \| `failed`。不传 = 不限 |
| `source` | string | 否 | 精确匹配，如 `openai` / `anthropic` |
| `path` | string | 否 | 精确匹配请求路径；若实现成本高可改为前缀 `path_prefix` |
| `request_id` | string | 否 | 精确匹配（管理排障常用） |
| `from` | string | 否 | 起始时间（含），ISO 本地时间，如 `2026-09-01T00:00:00` |
| `to` | string | 否 | 结束时间（不含），ISO 本地时间，如 `2026-10-01T00:00:00` |

校验：

- `from` / `to` 须可解析；`from >= to` → `422`
- `status` 不在枚举内 → `422`
- 时间跨度建议上限 **93 天**（可配置）；超出 → `422` 并提示缩小范围。不传时间则不限制（管理端全量汇总需注意性能，见 §7）

权限错误码与现接口保持一致（401 / 403 / 503）。

---

## 5. 扩展：`GET /v1/usage`

### 5.1 行为

在现有 `limit` / `offset` / `user_id` 基础上叠加 §4 筛选。

- 排序：仍按 `created_at DESC`（建议二级排序 `id DESC` 保证稳定分页）
- `total`：满足筛选条件的总行数（`COUNT(*)`）
- `items`：当前页明细，字段与现接口完全一致
- 普通用户：`cost_cny` / `profit_cny` 仍为 `null`

### 5.2 示例

```http
GET /v1/usage?status=billed&model=gpt-6-luna&from=2026-09-01T00:00:00&to=2026-10-01T00:00:00&limit=50&offset=0
Authorization: Bearer <API_KEY>
```

```http
GET /v1/usage?user_id=123&status=billed&limit=20&offset=0
Authorization: Bearer <ADMIN_API_KEY>
```

### 5.3 兼容

未传任何新筛选参数时，响应必须与当前线上行为一致，便于前端灰度。

---

## 6. 新增：`GET /v1/usage/summary`

### 6.1 用途

一次返回「当前筛选范围」下的条数与金额 / token 汇总，供：

- 用户端「总消费」卡片
- 管理端「收费 / 成本 / 利润」汇总
- 后续仪表盘

### 6.2 Query

仅使用 §4 筛选参数（**不要** `limit` / `offset`）。

### 6.3 成功响应 `200`

```json
{
  "filters": {
    "user_id": 123,
    "model": null,
    "model_prefix": null,
    "status": "billed",
    "source": null,
    "path": null,
    "request_id": null,
    "from": "2026-09-01T00:00:00",
    "to": "2026-10-01T00:00:00"
  },
  "request_count": 1280,
  "billed_count": 1200,
  "pending_count": 10,
  "unbilled_count": 50,
  "failed_count": 20,
  "sum_charged_cny": 12.34567890,
  "sum_cost_cny": 8.23000000,
  "sum_profit_cny": 4.11567890,
  "sum_input_tokens": 1200000,
  "sum_output_tokens": 340000,
  "sum_cache_read_tokens": 50000,
  "sum_cache_write_tokens": 8000
}
```

### 6.4 字段约定

| 字段 | 说明 |
|------|------|
| `filters` | 回显实际生效的筛选（便于前端核对）；未传为 `null` |
| `request_count` | 筛选范围内总请求数（含各 status） |
| `*_count` | 各 status 计数；若请求已带 `status=`，则其它 status 计数可为 `0` 或省略，**推荐始终返回四类计数**（在同一 user/时间范围内再按 status 分组），实现更直观 |
| `sum_charged_cny` | 仅对 `status = billed` 且金额非空的行求和；其它状态不计入 |
| `sum_cost_cny` / `sum_profit_cny` | 同上；**普通用户固定为 `null`** |
| `sum_*_tokens` | 对筛选范围内非空 token 求和（建议含全部 status；若只想 billed，文档需写死并与前端对齐） |

金额精度：与明细一致，最多 8 位小数。空结果时计数为 `0`，金额为 `0`（管理员）或 `null`（普通用户的成本/利润）。

### 6.5 普通用户 vs 管理员

| 字段 | 普通用户 | 管理员 |
|------|----------|--------|
| `request_count` / status 计数 | 可见 | 可见 |
| `sum_charged_cny` | 可见（本人） | 可见 |
| `sum_cost_cny` / `sum_profit_cny` | **`null`** | 可见 |
| token 汇总 | 可见 | 可见 |

### 6.6 示例

```bash
# 用户：本人本月已计费总消费
curl -sS 'https://www.rabbitj.top/v1/usage/summary?status=billed&from=2026-09-01T00:00:00&to=2026-10-01T00:00:00' \
  -H 'Authorization: Bearer <USER_API_KEY>'

# 管理员：指定用户全量汇总
curl -sS 'https://www.rabbitj.top/v1/usage/summary?user_id=123' \
  -H 'Authorization: Bearer <ADMIN_API_KEY>'
```

---

## 7. 实现建议

### 7.1 SQL 形态（示意）

明细：

```sql
SELECT ...
FROM tb_request_usage
WHERE
  (:scope_user_id IS NULL OR user_id = :scope_user_id)
  AND (:model IS NULL OR model = :model)
  AND (:model_prefix IS NULL OR model LIKE CONCAT(:model_prefix, '%'))
  AND (:status IS NULL OR status = :status)
  AND (:source IS NULL OR source = :source)
  AND (:path IS NULL OR path = :path)
  AND (:request_id IS NULL OR request_id = :request_id)
  AND (:from IS NULL OR created_at >= :from)
  AND (:to IS NULL OR created_at < :to)
ORDER BY created_at DESC, id DESC
LIMIT :limit OFFSET :offset;
```

汇总（金额只计 billed）：

```sql
SELECT
  COUNT(*) AS request_count,
  SUM(status = 'billed') AS billed_count,
  SUM(status = 'pending') AS pending_count,
  SUM(status = 'unbilled') AS unbilled_count,
  SUM(status = 'failed') AS failed_count,
  COALESCE(SUM(CASE WHEN status = 'billed' THEN charged_cny END), 0) AS sum_charged_cny,
  COALESCE(SUM(CASE WHEN status = 'billed' THEN cost_cny END), 0) AS sum_cost_cny,
  COALESCE(SUM(CASE WHEN status = 'billed' THEN profit_cny END), 0) AS sum_profit_cny,
  COALESCE(SUM(input_tokens), 0) AS sum_input_tokens,
  COALESCE(SUM(output_tokens), 0) AS sum_output_tokens,
  COALESCE(SUM(cache_read_tokens), 0) AS sum_cache_read_tokens,
  COALESCE(SUM(cache_write_tokens), 0) AS sum_cache_write_tokens
FROM tb_request_usage
WHERE <同上筛选>;
```

（`SUM(status = 'billed')` 为 MySQL 布尔求和写法；其它方言可改 `SUM(CASE ...)`。）

### 7.2 索引

按现有查询模式建议至少：

```sql
-- 用户时间线（最常用）
INDEX idx_usage_user_created (user_id, created_at DESC);

-- 管理端按时间扫 + status
INDEX idx_usage_created_status (created_at, status);

-- 排障
INDEX idx_usage_request_id (request_id);
```

若 `model` 筛选频繁，可再评估 `(user_id, model, created_at)`。以 `EXPLAIN` 为准，避免过多冗余索引。

### 7.3 性能护栏

| 场景 | 建议 |
|------|------|
| 无时间范围的全表 summary | 管理端允许，但应有慢查询日志；可对无 `from`/`to` 的请求默认限制最近 31 天（**若加默认窗口，必须在响应 `filters` 里回显**，并在文档写明） |
| 超大 offset | 保持现有 offset 分页即可；后续若深分页成为问题再改 keyset |
| 超时 | 与现接口一致返回 `503` `usage query unavailable` |

本期**不强制**默认时间窗，以免 silent 截断误导对账；若生产全表 summary 过慢，再加默认窗或强制 `from`。

---

## 8. 前端接入计划（后端完成后）

1. `GET /v1/usage`：筛选项（模型、状态、时间）直接带 query，去掉「拉页后本地 filter」。
2. `GET /v1/usage/summary`：替换当前「按需扫全部分页求总消费」；管理端卡片改为调 summary。
3. 列表与顶部汇总使用**相同**筛选参数，保证数字一致。

伪代码：

```ts
const qs = new URLSearchParams({ status: 'billed', from, to });
const [list, summary] = await Promise.all([
  fetch(`${BASE}/v1/usage?${qs}&limit=20&offset=0`, { headers }),
  fetch(`${BASE}/v1/usage/summary?${qs}`, { headers }),
]);
// summary.sum_charged_cny → 总消费
```

---

## 9. 验收清单

- [ ] 不传新参数时，`GET /v1/usage` 与线上一致
- [ ] 普通用户无法通过 `user_id` 查他人（403）
- [ ] 普通用户 summary / 明细均不返回成本与利润数值
- [ ] `status=billed` 时，summary 的 `sum_charged_cny` 与手工 `SUM` 明细一致（允许浮点 1e-8 误差）
- [ ] `from`/`to` 边界：`created_at = from` 计入，`created_at = to` 不计入
- [ ] 非法 `status` / 颠倒时间 → 422
- [ ] 十万级行 + `(user_id, created_at)` 条件下 summary P95 可接受（目标 &lt; 500ms，按机器调整）

---

## 10. 优先级建议

| 优先级 | 项 | 理由 |
|--------|----|------|
| P0 | `GET /v1/usage/summary`（至少支持 `user_id` + 可选 `from`/`to` + `status`） | 立刻去掉前端全表扫描 |
| P0 | `GET /v1/usage` 增加 `status` / `from` / `to` | 覆盖最常见筛选 |
| P1 | `model` / `model_prefix` / `request_id` | 排障与按模型分析 |
| P2 | `source` / `path`、默认时间窗、按日 group_by | 体验增强 |

---

## 11. 修订记录

| 日期 | 说明 |
|------|------|
| 2026-09-28 | 初稿：筛选扩展 + summary 聚合，供后端实现与联调 |
