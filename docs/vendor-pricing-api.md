# 多供应商模型价目 API（前端接入）

按 `vendor` / `base_url` 切换上游后，模型列表和价格都来自该供应商自己的价目表。  
前端应：**先拉供应商列表 → 再按选中供应商拉价目/模型**。

线上示例：

```text
https://www.rabbitj.top
```

本地示例：

```text
http://<host>:4000
```

---

## 数据模型（后端表）

| 表 | 用途 |
|----|------|
| `tb_upstream_vendor` | 供应商：`code`、`name`、`base_url`、可选 `pricing_url`、`pricing_mode`、`cost_rate` |
| `tb_vendor_model_price` | 某供应商下的模型价（USD / 1M tokens，含 standard/fast） |
| `tb_pricing_settings` | 全局汇率 + 成本加价（对所有供应商生效） |

关系：`vendor 1 —— N model_price`。**同一 `base_url` 可挂多个 `code`**（如 `yunxin-02` / `yunxin-03`），用不同 `cost_rate` 区分计价组；路由可重复，计价不唯一。

`pricing_mode`：

| 值 | 含义 |
|----|------|
| `manual`（默认） | 手工维护价目，或走 `pricing_url` 远程同步 |
| `catalog_rate` | 成本价 = OpenAI 官方标准目录（`MODEL_MAP_MONEY_DICT`）× `cost_rate`，物化写入 `tb_vendor_model_price` |

扣费 / 前台展示仍走原公式（在成本价之上）：

```text
用户价 = 成本价 × profit_multiplier × money_rate × discount
```

默认种子供应商：

| code | 说明 |
|------|------|
| `teamo` | TeamoRouter，可从官网 `pricing_url` 定时同步 |
| `legacy` | 本地 LiteLLM |
| `yunxin` | 号商 Hub |

## 热切换（不用重启）

当前生效上游以 MySQL `tb_upstream_vendor.is_default=1` 为准，**不是**只读启动环境变量。

| 操作 | 接口 | 是否重启 |
|------|------|----------|
| 切换当前供应商 | `POST /v1/admin/vendors/{code}/activate` | 否（本进程立即生效；多 worker ≤30s） |
| 改某供应商 base_url / 倍率 | `PUT /v1/admin/vendors`（可带 `is_default:true`） | 否 |
| 读官方标准价目录 | `GET /v1/admin/pricing/catalog?cost_rate=0.2` | 否 |
| 按标准价×倍率重算 | `POST /v1/admin/vendors/{code}/reapply-catalog` | 否 |
| 列出某供应商原始美元价 | `GET /v1/admin/vendors/{code}/models` | 否 |
| 读单个模型价 | `GET /v1/admin/vendors/{code}/models/{model}?service_tier=standard` | 否 |
| 单独改某个模型价 | `PUT /v1/admin/vendors/{code}/models/{model}` | 否 |
| 批量改/补模型价 | `PUT /v1/admin/vendors/{code}/models` | 否 |
| 删除某个模型价 | `DELETE /v1/admin/vendors/{code}/models/{model}?service_tier=standard` | 否 |
| 拉远程公开价目 | `POST /v1/admin/vendors/{code}/refresh` | 否 |

查看当前运行时：

```http
GET /v1/admin/runtime
Authorization: Bearer <ADMIN_API_KEY>
```

激活示例：

```http
POST /v1/admin/vendors/teamo/activate
Authorization: Bearer <ADMIN_API_KEY>
```

改 base_url 并立即切过去：

```http
PUT /v1/admin/vendors
Authorization: Bearer <ADMIN_API_KEY>
Content-Type: application/json

{
  "code": "teamo",
  "name": "TeamoRouter",
  "base_url": "https://api.teamorouter.com",
  "pricing_url": "https://teamorouter.com/v1/models/pricing",
  "is_active": true,
  "is_default": true
}
```

后台每约 30 秒会从 MySQL 重载供应商与价目缓存，因此你在库里直接改 `is_default` / `base_url` / 价格行，也会被进程跟上（推荐仍走管理接口）。

前端浏览价目时可指定任意 `vendor`，不必等于当前生效上游。

---

## 鉴权

与用量接口相同，请求头任选其一：

```http
Authorization: Bearer <API_KEY>
x-api-key: <API_KEY>
api-key: <API_KEY>
```

`API_KEY` 为 `tb_user_api_key` 中启用的 `sk-` 密钥。`register_time < 2026-10-01` 的账号仍可用手机号。用户须 `is_delete = false`。

| HTTP | 含义 |
|------|------|
| 401 | 未传 key |
| 403 | key 无效，或管理接口非管理员 |

---

## 前端接口

### 1. `GET /v1/vendors`

供应商切换器数据源。

```ts
const res = await fetch(`${BASE}/v1/vendors`, {
  headers: { Authorization: `Bearer ${apiKey}` },
});
const data = await res.json();
// data.current_vendor: 当前计费/转发使用的供应商 code
// data.vendors: [{ code, name, base_url, model_count, is_current, is_default, last_synced_at }]
```

响应示例：

```json
{
  "current_vendor": "teamo",
  "vendors": [
    {
      "id": 1,
      "code": "teamo",
      "name": "TeamoRouter",
      "base_url": "https://api.teamorouter.com",
      "pricing_url": "https://teamorouter.com/v1/models/pricing",
      "is_default": true,
      "is_current": true,
      "model_count": 42,
      "last_synced_at": "2026-09-28T14:00:00"
    }
  ]
}
```

### 2. `GET /v1/pricing/models`

按供应商返回**当前用户有效美元价** + 模型列表。

Query：

| 参数 | 说明 |
|------|------|
| `vendor` | 供应商 code，如 `teamo`。不传 = 当前活跃供应商 |
| `base_url` | 按上游根地址匹配（可带或不带 `/v1`）。与 `vendor` 二选一，优先 `vendor` |

价格公式（单位：USD / 1M tokens）：

```text
用户单价(USD/1M) = 供应商成本价(USD/1M)
                 × profit_multiplier
                 × user.money_rate
                 × user.discount
```

`usd_cny_rate` 只作为参考字段返回，**不参与**价目换算；需要人民币展示时由前端自行 × 汇率。

```ts
const qs = new URLSearchParams({ vendor: selectedVendorCode });
// 或：qs.set("base_url", selectedBaseUrl);

const res = await fetch(`${BASE}/v1/pricing/models?${qs}`, {
  headers: { Authorization: `Bearer ${apiKey}` },
});
const pricing = await res.json();

// 模型下拉：pricing.model_ids
// 价目表：pricing.models（含 service_tier=standard|fast）
// 图片模型：pricing.image_models
```

响应关键字段：

```json
{
  "currency": "USD",
  "price_unit": "per 1M tokens",
  "vendor": {
    "code": "teamo",
    "name": "TeamoRouter",
    "base_url": "https://api.teamorouter.com",
    "is_current": true,
    "last_synced_at": "2026-09-28T14:00:00"
  },
  "usd_cny_rate": 6.9,
  "profit_multiplier": 1.5,
  "money_rate": 1,
  "discount": 1,
  "model_ids": ["claude-opus-5-5", "gpt-6-sol"],
  "models": [
    {
      "model": "gpt-6-sol",
      "display_name": "gpt-6-sol",
      "owned_by": "openai",
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

前端推荐渲染：

1. 顶部用 `/v1/vendors` 做供应商 Tab / 下拉。
2. 切换时带 `vendor` 重拉 `/v1/pricing/models`。
3. 模型选择器用 `model_ids`；详细价用 `models` 按 `model + service_tier` 分组。
4. 表格列：模型、档位、输入/输出/缓存读/缓存写（单位：**美元 / 百万 tokens**）。

`404`：`{"error":"vendor not found"}`。

---

## 管理接口（`is_staff = true`）

### 全局汇率 / 加价

- `GET /v1/admin/pricing`
- `PUT /v1/admin/pricing` body: `{ "usd_cny_rate": 6.9, "profit_multiplier": 1.5 }`

### 供应商 CRUD

```http
PUT /v1/admin/vendors
Authorization: Bearer <ADMIN_API_KEY>
Content-Type: application/json

{
  "code": "yunxin-02",
  "name": "号商 0.2x",
  "base_url": "https://ai.yunxinapi.com/hub",
  "pricing_mode": "catalog_rate",
  "cost_rate": 0.2,
  "is_active": true,
  "is_default": false,
  "sort_order": 40
}
```

`pricing_mode=catalog_rate` 时服务会立即把官方目录 × `cost_rate` 写入该供应商价目；同一 `base_url` 再建 `yunxin-03`（`cost_rate=0.3`）即可形成另一计价组。

手工/远程模式：

```http
PUT /v1/admin/vendors
Authorization: Bearer <ADMIN_API_KEY>
Content-Type: application/json

{
  "code": "new-vendor",
  "name": "新供应商",
  "base_url": "https://api.example.com",
  "pricing_url": null,
  "pricing_mode": "manual",
  "is_active": true,
  "is_default": false,
  "sort_order": 40
}
```

有 `pricing_url` 时（如 Teamo 的 `https://teamorouter.com/v1/models/pricing`），服务会约每 5 分钟自动拉取并更新该供应商价目。

### 官方标准价目录

```http
GET /v1/admin/pricing/catalog
GET /v1/admin/pricing/catalog?cost_rate=0.2
Authorization: Bearer <ADMIN_API_KEY>
```

不带 `cost_rate` 返回官方原价；带则返回「目录 × 倍率」预览（写入供应商前可核对）。

### 按标准价重算

```http
POST /v1/admin/vendors/yunxin-02/reapply-catalog
Authorization: Bearer <ADMIN_API_KEY>
```

在官方目录更新后，对已有 `catalog_rate` 供应商执行一次即可刷新物化成本价。

### 管理端价目 CRUD（后台必用）

单位一律 **USD / 1M tokens**，返回的是供应商成本价（不含用户 `money_rate` / `discount`）。需管理员 API Key。

#### 列出某供应商全部模型价

```http
GET /v1/admin/vendors/teamo/models
Authorization: Bearer <ADMIN_API_KEY>
```

#### 读单个模型价

```http
GET /v1/admin/vendors/teamo/models/gpt-6-sol?service_tier=standard
```

#### 单独改某个模型价（新增或覆盖）

```http
PUT /v1/admin/vendors/teamo/models/gpt-6-sol
Authorization: Bearer <ADMIN_API_KEY>
Content-Type: application/json

{
  "service_tier": "standard",
  "kind": "chat",
  "input": 0.2,
  "output": 1.0,
  "cache_read": 0.02,
  "cache_write": 0.25
}
```

Fast 档：

```http
PUT /v1/admin/vendors/teamo/models/gpt-5.5
Content-Type: application/json

{
  "service_tier": "fast",
  "input": 0.8,
  "output": 4.8,
  "cache_read": 0.08,
  "cache_write": 0
}
```

成功响应含更新后的 `price` 对象。立即写入 MySQL 并刷新本进程缓存；其它 worker ≤30s 跟上。

#### 删除某个模型价（软删）

```http
DELETE /v1/admin/vendors/teamo/models/gpt-6-sol?service_tier=standard
```

#### 批量写入

```http
PUT /v1/admin/vendors/{code}/models
Content-Type: application/json

{
  "models": [
    {
      "model": "gpt-6-sol",
      "service_tier": "standard",
      "kind": "chat",
      "input": 0.2,
      "output": 1.0,
      "cache_read": 0.02,
      "cache_write": 0.25
    }
  ]
}
```

后台管理页建议：

1. `GET /v1/admin/vendors` 选供应商
2. `GET /v1/admin/vendors/{code}/models` 渲染可编辑表格
3. 行内保存调 `PUT .../models/{model}`
4. 删除行调 `DELETE .../models/{model}?service_tier=...`

### 立即同步远程价目

```http
POST /v1/admin/vendors/{code}/refresh
```

成功 `200`：`{ "ok": true, "updated": true, "count": 42, ... }`  
失败 `502`：`{ "ok": false, "error": "..." }`（本地缓存仍可用）。

---

## 接入流程（前端）

```ts
async function loadPricingPage(apiKey: string, vendor?: string) {
  const headers = { Authorization: `Bearer ${apiKey}` };

  const vendorsRes = await fetch(`${BASE}/v1/vendors`, { headers });
  if (!vendorsRes.ok) throw new Error("vendors failed");
  const { current_vendor, vendors } = await vendorsRes.json();

  const selected = vendor || current_vendor || vendors[0]?.code;
  const pricingRes = await fetch(
    `${BASE}/v1/pricing/models?vendor=${encodeURIComponent(selected)}`,
    { headers },
  );
  if (!pricingRes.ok) throw new Error("pricing failed");
  const pricing = await pricingRes.json();

  return {
    vendors,
    selectedVendor: selected,
    modelIds: pricing.model_ids as string[],
    rows: pricing.models as Array<{
      model: string;
      service_tier: string;
      input: number;
      output: number;
      cache_read: number;
      cache_write: number;
    }>,
    imageRows: pricing.image_models,
    fx: {
      usd_cny_rate: pricing.usd_cny_rate,
      profit_multiplier: pricing.profit_multiplier,
      money_rate: pricing.money_rate,
      discount: pricing.discount,
    },
  };
}
```

用户切换供应商时，只改 `vendor` 参数重新调用 `loadPricingPage` 即可；不要把多供应商价目混在一张静态表里。

---

## 注意

- 返回给前端的是**用户应付美元价**（已含利润倍率与用户倍率），单位 USD / 1M tokens。
- `usd_cny_rate` 仅参考；扣费入账仍可在服务端换算人民币，与价目展示货币无关。
- 请求级倍率（长上下文、部分 Fast 推导）可能导致单次账单与目录价略有差异。
- Teamo 等有 `pricing_url` 的供应商会后台同步；无公开价目接口的供应商请走管理端手工 `PUT .../models`。
- 当前进程的实际转发上游由 MySQL `is_default` 决定，可用 `POST /v1/admin/vendors/{code}/activate` 热切换；环境变量 `UPSTREAM_PROVIDER` 仅作首次种子默认值。
- 价目查询可独立于当前上游浏览各供应商报价。
