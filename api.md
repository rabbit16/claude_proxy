# 账户余额与花费查询 API

本文档描述两个面向用户的只读接口：用 API Key 查询本人余额，以及按日期范围统计本人花费。

- 线上地址示例：`https://www.rabbitj.top`
- 本地/直连：`http://<host>:4000`

---

## 鉴权

两个接口都使用请求头中的用户 API Key 识别身份。支持的请求头（任选其一）：

```http
Authorization: Bearer <API_KEY>
x-api-key: <API_KEY>
api-key: <API_KEY>
```

密钥规则：

- `sk-` 开头：必须存在于 `tb_user_api_key`，且 `is_active = true`
- 手机号：仅 `register_time < 2026-10-01` 的账号仍可当作密钥；之后注册的账号无效
- 用户必须未被删除（`tb_user.is_delete = false`）

只能查询本人数据，接口不接受 `user_id` 参数。

通用错误：

| HTTP | body | 含义 |
|------|------|------|
| 401 | `{"error":"missing API key"}` | 未传密钥 |
| 403 | `{"error":"invalid API key"}` | 密钥无效或用户已删除 |
| 422 | `{"error":"..."}` | 日期格式/范围非法 |
| 503 | `{"error":"... unavailable"}` | 数据库查询异常 |

---

## `GET /v1/account/balance`

查询当前 API Key 所属用户的实时余额。

### Query 参数

无。

### 成功响应 `200`

```json
{
  "user_id": 123,
  "balance_cny": 128.5025,
  "currency": "CNY",
  "is_active": true
}
```

### 字段说明

| 字段 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `user_id` | int | 否 | 内部用户主键 |
| `balance_cny` | number | 否 | 当前余额，人民币，保留 8 位小数；无余额时为 `0` |
| `currency` | string | 否 | 固定 `CNY` |
| `is_active` | bool | 否 | 账号是否处于激活状态 |

### curl 示例

```bash
curl -H "Authorization: Bearer sk-xxxx" \
  https://www.rabbitj.top/v1/account/balance
```

---

## `GET /v1/account/spending`

按日期范围统计本人已计费请求的花费，返回逐日明细与区间汇总。

### Query 参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `from` | string | 否 | `to` 往前 29 天 | 起始日期，`YYYY-MM-DD`，含当天 |
| `to` | string | 否 | 今天 | 结束日期，`YYYY-MM-DD`，含当天 |

说明：

- 只统计 `status = billed` 的请求；失败/未计费请求不计入。
- 区间内没有请求的日期也会返回，数值为 `0`，方便前端直接画图。
- 日期按服务器本地时间（无时区）切分，单日区间为 `[00:00:00, 次日 00:00:00)`。
- 单次查询跨度最大 366 天；`from` 晚于 `to` 返回 422。
- 普通用户响应中的 `cost_cny`、`profit_cny` 固定为 `null`（不暴露成本与利润）；管理员可见。

### 成功响应 `200`

```json
{
  "user_id": 123,
  "currency": "CNY",
  "from": "2026-10-01",
  "to": "2026-10-03",
  "totals": {
    "request_count": 42,
    "charged_cny": 3.204916,
    "recharged_cny": 100.0,
    "input_tokens": 128400,
    "output_tokens": 31500,
    "cache_read_tokens": 20480,
    "cache_write_tokens": 1024
  },
  "balance_cny": 128.5025,
  "days": [
    {
      "date": "2026-10-01",
      "request_count": 10,
      "charged_cny": 0.8123,
      "input_tokens": 30100,
      "output_tokens": 7200,
      "cache_read_tokens": 4096,
      "cache_write_tokens": 0
    },
    {
      "date": "2026-10-02",
      "request_count": 0,
      "charged_cny": 0.0,
      "input_tokens": 0,
      "output_tokens": 0,
      "cache_read_tokens": 0,
      "cache_write_tokens": 0
    },
    {
      "date": "2026-10-03",
      "request_count": 32,
      "charged_cny": 2.392616,
      "input_tokens": 98300,
      "output_tokens": 24300,
      "cache_read_tokens": 16384,
      "cache_write_tokens": 1024
    }
  ]
}
```

管理员额外字段：

```json
{
  "totals": { "cost_cny": 1.6024, "profit_cny": 1.602516 },
  "days": [{ "date": "2026-10-01", "cost_cny": 0.4061, "profit_cny": 0.4062 }]
}
```

### 字段说明

| 字段 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `user_id` | int | 否 | 内部用户主键 |
| `currency` | string | 否 | 固定 `CNY` |
| `from` / `to` | string | 否 | 实际生效的日期区间（`YYYY-MM-DD`，含边界） |
| `totals` | object | 否 | 区间汇总 |
| `totals.request_count` | int | 否 | 已计费请求数 |
| `totals.charged_cny` | number | 否 | 区间总花费，人民币 |
| `totals.recharged_cny` | number | 否 | 区间内充值金额（`tb_bill.money`，不含已删除账单） |
| `totals.input_tokens` | int | 否 | 输入 token 合计 |
| `totals.output_tokens` | int | 否 | 输出 token 合计 |
| `totals.cache_read_tokens` | int | 否 | 缓存读 token 合计 |
| `totals.cache_write_tokens` | int | 否 | 缓存写 token 合计 |
| `totals.cost_cny` | number | 管理员可空 | 上游成本合计；普通用户不返回 |
| `totals.profit_cny` | number | 管理员可空 | 利润合计；普通用户不返回 |
| `balance_cny` | number | 否 | 查询时的当前余额 |
| `days` | array | 否 | 逐日明细，按日期升序，区间内每天都有一条 |
| `days[].date` | string | 否 | `YYYY-MM-DD` |
| `days[].request_count` | int | 否 | 当日已计费请求数 |
| `days[].charged_cny` | number | 否 | 当日花费 |
| `days[].cost_cny` | number | 普通用户为 `null` | 当日上游成本 |
| `days[].profit_cny` | number | 普通用户为 `null` | 当日利润 |
| `days[].input_tokens` | int | 否 | 当日输入 token |
| `days[].output_tokens` | int | 否 | 当日输出 token |
| `days[].cache_read_tokens` | int | 否 | 当日缓存读 token |
| `days[].cache_write_tokens` | int | 否 | 当日缓存写 token |

### curl 示例

```bash
# 默认：最近 30 天（含今天）
curl -H "Authorization: Bearer sk-xxxx" \
  "https://www.rabbitj.top/v1/account/spending"

# 指定日期范围（含首尾）
curl -H "Authorization: Bearer sk-xxxx" \
  "https://www.rabbitj.top/v1/account/spending?from=2026-10-01&to=2026-10-31"
```

### 前端调用示例

```javascript
async function fetchSpending(apiKey, from, to) {
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const resp = await fetch(`/v1/account/spending?${params}`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (!resp.ok) throw new Error((await resp.json()).error);
  return resp.json();
}
```

---

## 数据口径

| 指标 | 来源 |
|------|------|
| 余额 | `tb_user.token`（人民币） |
| 单笔花费 | `tb_request_usage.charged_cny`，仅 `status = billed` |
| 成本 / 利润 | `tb_request_usage.cost_cny` / `profit_cny` |
| 充值 | `tb_bill.money`，按 `generate_time` 落在区间内且未软删除 |

注意：

- 余额是实时值，不由区间明细累加得出；充值、后台调整都会影响余额。
- 请求落库与计费完成之间有短暂延迟，刚完成的请求可能尚未计入统计。
