# 请求用量查询 API（前端接入）

用于后台查询每次 LLM 请求的模型、token、缓存读写、收费与商家利润。

线上地址示例：

```text
https://www.rabbitj.top/v1/usage
```

本地/直连代理示例：

```text
http://<host>:4000/v1/usage
```

---

## 鉴权

任意有效用户都可查询。请求头携带用户 API Key，且该用户在 `tb_user` 中需满足 `is_delete = false`。

密钥规则：

- `sk-` 开头：必须存在于 `tb_user_api_key`，且 `is_active = true`
- 手机号：仅 `register_time < 2026-10-01` 的账号仍可当作密钥；之后注册的账号无效

权限：

| 角色 | 可查询范围 |
|------|------------|
| 普通用户 | 仅自己的记录（自动按本人 `user_id` 过滤） |
| 管理员（`is_staff = true`） | 全部用户；可用 `user_id` 筛选指定用户 |

支持的请求头（任选其一）：

```http
Authorization: Bearer <API_KEY>
x-api-key: <API_KEY>
api-key: <API_KEY>
```

错误：

| HTTP | body | 含义 |
|------|------|------|
| 401 | `{"error":"missing API key"}` | 未传 key |
| 403 | `{"error":"invalid API key"}` | key 无效或用户已删除 |
| 403 | `{"error":"can only query own usage"}` | 普通用户查询他人 |
| 422 | FastAPI 校验错误 | `limit` / `offset` 超范围 |
| 503 | `{"error":"usage query unavailable"}` | 查询异常 |

---

## 接口

### `GET /v1/usage`

分页查询请求用量明细，按时间倒序。

#### Query 参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `limit` | int | 否 | `50` | 每页条数，范围 `1~200` |
| `offset` | int | 否 | `0` | 偏移量，从 `0` 开始 |
| `user_id` | int | 否 | 不传 | 管理员可按内部用户主键筛选；普通用户传了也只能是自己，否则 403 |
| `model` | string | 否 | 不限 | 精确匹配模型；与 `model_prefix` 同传时优先 `model` |
| `model_prefix` | string | 否 | 不限 | 模型前缀匹配 |
| `status` | string | 否 | 不限 | `pending` / `billed` / `unbilled` / `failed` |
| `source` | string | 否 | 不限 | 精确匹配协议来源 |
| `path` | string | 否 | 不限 | 精确匹配请求路径 |
| `request_id` | string | 否 | 不限 | 精确匹配请求 ID |
| `from` | string | 否 | 不限 | ISO 时间，含边界 |
| `to` | string | 否 | 不限 | ISO 时间，不含边界；与 `from` 最大跨度建议 93 天 |

说明：

- `user_id` 是数据库用户主键，不是手机号 / API Key。
- 普通用户不传 `user_id` 时，后端自动只返回本人记录。
- 普通用户响应里 `cost_cny`、`profit_cny` 固定为 `null`（不暴露成本与商家利润）；管理员可见完整金额。

#### 成功响应 `200`

```json
{
  "total": 128,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "request_id": "ac8cc133fb4f",
      "user_id": 123,
      "model": "gpt-6-luna",
      "path": "/v1/chat/completions",
      "source": "openai",
      "status": "billed",
      "http_status": 200,
      "input_tokens": 7,
      "output_tokens": 21,
      "cache_read_tokens": 0,
      "cache_write_tokens": 0,
      "charged_cny": 0.00008995,
      "cost_cny": 0.00004498,
      "profit_cny": 0.00004497,
      "created_at": "2026-09-27T04:25:22"
    }
  ]
}
```

#### 字段说明

| 字段 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `total` | number | 否 | 符合筛选条件的总条数（用于分页） |
| `limit` | number | 否 | 本次请求的 limit |
| `offset` | number | 否 | 本次请求的 offset |
| `items` | array | 否 | 明细列表 |
| `items[].request_id` | string | 否 | 请求 ID（12 位 hex） |
| `items[].user_id` | number \| null | 是 | 关联 `tb_user.user_id`；未匹配到用户时为 `null` |
| `items[].model` | string | 否 | 请求模型名；未知时可能为 `"-"` |
| `items[].path` | string | 否 | 请求路径，如 `/v1/chat/completions` |
| `items[].source` | string | 否 | 协议来源：`openai` / `anthropic` / `responses` / `images` 等 |
| `items[].status` | string | 否 | 记账状态，见下表 |
| `items[].http_status` | number \| null | 是 | 下游/本服务 HTTP 状态码 |
| `items[].input_tokens` | number \| null | 是 | 输入 token（不含 cache 时按计费口径） |
| `items[].output_tokens` | number \| null | 是 | 输出 token |
| `items[].cache_read_tokens` | number \| null | 是 | 缓存读取 token |
| `items[].cache_write_tokens` | number \| null | 是 | 缓存写入 token |
| `items[].charged_cny` | number \| null | 是 | 向用户收取金额（人民币，元） |
| `items[].cost_cny` | number \| null | 是 | 上游成本（人民币，元） |
| `items[].profit_cny` | number \| null | 是 | 商家利润 = `charged_cny - cost_cny`（人民币，元） |
| `items[].created_at` | string \| null | 是 | 创建时间，ISO 字符串（服务端本地时间） |

#### `status` 枚举

| 值 | 含义 | 前端建议展示 |
|----|------|--------------|
| `pending` | 请求已进入，尚未结束/尚未扣费 | 处理中 |
| `billed` | 已成功扣费并写入金额 | 已计费 |
| `unbilled` | 请求结束但未扣费（如 `/v1/models`、无 usage、鉴权失败前已落库等） | 未计费 |
| `failed` | HTTP 状态码 ≥ 400 | 失败 |

展示金额时建议只在 `status === "billed"` 时高亮 `charged_cny / profit_cny`；其它状态金额常为 `null`。

---

## `GET /v1/usage/summary`

按与明细相同的筛选条件一次返回请求数、各状态数量、token 合计和金额合计；不使用 `limit` / `offset`。普通用户的 `sum_cost_cny` 与 `sum_profit_cny` 固定为 `null`。

返回字段包括：`filters`、`request_count`、`billed_count`、`pending_count`、`unbilled_count`、`failed_count`、`sum_charged_cny`、`sum_cost_cny`、`sum_profit_cny`、`sum_input_tokens`、`sum_output_tokens`、`sum_cache_read_tokens`、`sum_cache_write_tokens`。

非法 `status` 或 `from >= to` 返回 422；查询异常返回 503。

```bash
curl -sS 'https://www.rabbitj.top/v1/usage/summary?status=billed&from=2026-09-01T00:00:00&to=2026-10-01T00:00:00' \\
  -H 'Authorization: Bearer <API_KEY>'
```

## 前端调用示例

### fetch

```ts
type UsageItem = {
  request_id: string;
  user_id: number | null;
  model: string;
  path: string;
  source: string;
  status: "pending" | "billed" | "unbilled" | "failed";
  http_status: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  charged_cny: number | null;
  cost_cny: number | null;
  profit_cny: number | null;
  created_at: string | null;
};

type UsageResponse = {
  total: number;
  limit: number;
  offset: number;
  items: UsageItem[];
};

async function fetchUsage(params: {
  apiKey: string;
  limit?: number;
  offset?: number;
  userId?: number;
}): Promise<UsageResponse> {
  const q = new URLSearchParams();
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  if (params.userId != null) q.set("user_id", String(params.userId));

  const res = await fetch(`https://www.rabbitj.top/v1/usage?${q}`, {
    headers: { Authorization: `Bearer ${params.apiKey}` },
  });
  if (!res.ok) {
    throw new Error(`usage query failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}
```

### curl

```bash
# 普通用户查自己
curl -sS 'https://www.rabbitj.top/v1/usage?limit=50&offset=0' \
  -H 'Authorization: Bearer <USER_API_KEY>'

# 管理员查全部
curl -sS 'https://www.rabbitj.top/v1/usage?limit=50&offset=0' \
  -H 'Authorization: Bearer <ADMIN_API_KEY>'

# 管理员查指定用户
curl -sS 'https://www.rabbitj.top/v1/usage?user_id=123&limit=20&offset=0' \
  -H 'Authorization: Bearer <ADMIN_API_KEY>'
```

---

## 页面建议

1. 列表列：时间、模型、路径、状态、输入/输出/缓存读/缓存写、收费；管理端可再加用户 ID、成本、利润。
2. 金额统一按「元」展示，建议保留 4~8 位小数（接口最多 8 位）。
3. 分页：`pageSize = limit`，`pageIndex` 从 0 开始时 `offset = pageIndex * limit`；总页数用 `Math.ceil(total / limit)`。
4. 利润汇总仅管理端有意义；当前接口未提供聚合字段。
5. CORS 已放开 `*`，浏览器可直接跨域请求；生产仍建议走同源网关。

---

## 数据写入说明（给前端对齐预期）

代理收到转发请求时会自动落库到 `tb_request_usage`，前端无需写入。

- 几乎所有经代理的请求都会先插入一条 `pending`。
- 扣费成功后更新为 `billed`，并写入 token 与金额。
- `/v1/models` 等不计费路径通常落成 `unbilled`。
- 上游/鉴权失败通常落成 `failed`。
- 当前全局费率存于 `tb_pricing_settings`；默认成本加价系数为 `1.5`，用户个人 `money_rate`、`discount` 也参与乘算，详见下方费率接口。

关联关系：

```text
tb_request_usage.user_id  →  tb_user.user_id
```

---

## 费率设置与前端模型计价

服务启动时会确保全局单行配置表 `tb_pricing_settings` 存在，并首次插入默认值：`usd_cny_rate=6.9`、`profit_multiplier=1.5`。运行时计费优先读取数据库全局参数；旧常量仅用于配置表缺失/数据库暂不可用时回退。当前请求按其计费时读取的参数完成收费。

公式（LLM 和图片）：

```text
用户价（元） = 上游价目美元金额 × usd_cny_rate × profit_multiplier × 用户.money_rate × 用户.discount
账面成本（元） = 上游价目美元金额 × usd_cny_rate
账面毛利 = 用户价 - 账面成本
```

默认用户两个个人系数均为 1 时，默认配置代表成本加价 50%；费用倍率均采用乘法。折扣以 `1` 为原价，`0.4` 为 4 折。`cost_cny` 是使用相同数据库汇率换算的配置价估算，不代表上游真实扣款。

### 管理员读写全局参数

- `GET /v1/admin/pricing`：管理员读取 `{ "usd_cny_rate": 6.9, "profit_multiplier": 1.5 }`。
- `PUT /v1/admin/pricing`：管理员更新全局参数，需使用用户 API Key。非管理员返回 403。

```http
PUT /v1/admin/pricing
Authorization: Bearer <ADMIN_API_KEY>
Content-Type: application/json

{"usd_cny_rate": 6.9, "profit_multiplier": 1.5}
```

请求值必须大于 0 且不超过 100。生产环境应以实际充值汇率和目标成本加价配置；此操作影响所有用户后续请求。

### 前端读取当前用户模型价

按供应商动态返回模型列表与**美元**价。详见完整文档：[`vendor-pricing-api.md`](./vendor-pricing-api.md)。

- `GET /v1/vendors`：供应商列表（含 `base_url`、`model_count`）
- `GET /v1/pricing/models?vendor=teamo`：该供应商下，按利润倍率及用户 `money_rate`/`discount` 计算后的 **USD** 价

也可 `?base_url=https://api.teamorouter.cn` 按上游地址选价目表。

响应示例（数值仅示例）：

```json
{
  "currency": "USD",
  "price_unit": "per 1M tokens",
  "vendor": {
    "code": "teamo",
    "name": "TeamoRouter",
    "base_url": "https://api.teamorouter.cn",
    "is_current": true
  },
  "usd_cny_rate": 6.9,
  "profit_multiplier": 1.5,
  "money_rate": 1,
  "discount": 1,
  "model_ids": ["gpt-6-sol"],
  "models": [
    {
      "model": "gpt-6-sol",
      "service_tier": "standard",
      "unit": "USD per 1M tokens",
      "input": 0.3,
      "output": 1.5,
      "cache_read": 0.03,
      "cache_write": 0.375
    }
  ],
  "image_models": []
}
```

`models` 含 `service_tier: standard|fast`。前端用 `model_ids` 做模型下拉，用 `models` 做价目表。单位均为 **美元 / 百万 tokens**。`usd_cny_rate` 仅供前端自行换算展示，不参与本接口返回价。

```ts
const response = await fetch(
  "https://www.rabbitj.top/v1/pricing/models?vendor=teamo",
  { headers: { Authorization: `Bearer ${apiKey}` } },
);
if (!response.ok) throw new Error(`pricing query failed: ${response.status}`);
const currentPricing = await response.json();
// currentPricing.model_ids
// currentPricing.models[].input / output / cache_read / cache_write
// 单位均为美元 / 百万 tokens
```

管理端还可：`PUT /v1/admin/vendors` 增改供应商，`PUT /v1/admin/vendors/{code}/models` 写美元成本价，`POST /v1/admin/vendors/{code}/refresh` 立即同步远程价目。

---

## 已知限制

- 普通用户只能看自己的记录；`cost_cny` / `profit_cny` 对普通用户隐藏。
- 无时间范围、模型、status 筛选参数；需要前端拉页后过滤，或后续扩展 query。
- `created_at` 无时区后缀，按服务器本地时间理解。
- 流式请求若异常中断，偶发会短暂停留在 `pending`。
- 无服务端金额聚合；前端若统计「总消费」只能分页扫全表（数据量大时很慢）。

筛选与汇总的后端设计见：[`usage-query-enhancement-design.md`](./usage-query-enhancement-design.md)。
