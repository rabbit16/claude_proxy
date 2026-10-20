# 后端 API 设计文档（FastAPI）

面向「适老化语音问答 App」的 RESTful API 约定。前端已按本约定预留 `src/services/*`。

- Base URL：`{API_BASE_URL}/api/v1`
- 前端配置文件：`config/api.json` 的 `apiBaseUrl`
- 机器可读规范：[`docs/openapi.yaml`](./openapi.yaml)（可直接导入 FastAPI / Swagger UI）

---

## 1. 通用约定

### 1.1 REST 原则

| 方法 | 用途 |
|------|------|
| `GET` | 查询资源，幂等 |
| `POST` | 创建资源，或触发非幂等动作（登录、OCR、分享） |
| `PUT` | 全量替换资源 |
| `PATCH` | 部分更新资源 |
| `DELETE` | 删除资源 |

- 资源用名词复数：`/archives`、`/family/contacts`
- 子资源：`/archives/{id}/share`
- 动作型接口仅在无法用资源语义表达时使用：`/auth/login/sms`、`/voice/recognize`

### 1.2 认证

- 登录成功后返回 JWT：`access_token` + `refresh_token`
- 受保护接口请求头：

```http
Authorization: Bearer <access_token>
```

### 1.3 统一响应

成功：直接返回资源 JSON（不包一层 `{data:...}`，便于 FastAPI 原生模型）。

失败：

```json
{
  "code": "invalid_sms_code",
  "message": "验证码错误或已过期",
  "details": {}
}
```

| HTTP | 含义 |
|------|------|
| 400 | 参数错误 |
| 401 | 未登录 / token 无效 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 409 | 冲突（如手机号已注册逻辑冲突） |
| 422 | 校验失败 |
| 429 | 限流（短信发送等） |
| 500 | 服务端错误 |

### 1.4 时间与语言

- 时间一律 ISO 8601 UTC：`2026-07-29T04:00:00Z`
- `lang` / `preferred_lang`：`zh` | `en`

### 1.5 CORS / Android 明文 HTTP

开发期若用 `http://局域网IP:8000`：

1. FastAPI 开启 CORS，允许 App 来源或 `*`
2. Android 已允许明文流量时才能用 HTTP（本项目若未开，需在 `AndroidManifest` / `networkSecurityConfig` 配置）

---

## 2. 接口清单

### 2.1 健康检查

#### `GET /health`

无需鉴权。

```json
{ "status": "ok", "version": "0.1.0" }
```

> 注意：健康检查可挂在 `/api/v1/health`，也可额外提供根路径 `/health`。

---

### 2.2 认证 Auth

#### `POST /auth/sms/send`

发送登录/注册验证码。

请求：

```json
{ "phone": "13800138000", "purpose": "login" }
```

`purpose`：`login` | `register` | `reset_password`（默认 `login`）

响应 `200`：

```json
{ "ok": true, "expire_in": 300 }
```

错误示例：`429` + `code=sms_rate_limited`

---

#### `POST /auth/register`

新用户注册（对齐 `users` 表：手机号、**邮箱**、密码、可选昵称）。  
未接短信网关：验证码仍可本地固定；**推送子女改走 QQ 邮箱 SMTP**，不要发短信。

请求：

```json
{
  "phone": "13800138000",
  "email": "elder@qq.com",
  "code": "123456",
  "password": "******",
  "display_name": "可选昵称",
  "preferred_lang": "zh"
}
```

| 字段 | 说明 |
|------|------|
| `email` | **必填**。存 `users.email`，小写规范化。用于账号联系；推送子女用的是子女自己的邮箱 |

响应：同短信登录（返回 token + user，含 `email`）。  
错误：`409` `phone_conflict` 手机号已注册；`409` `email_conflict` 邮箱已注册；`401` 验证码无效；`400` `invalid_email`。

---

#### `POST /auth/login/sms`

验证码登录；可选同时设置密码（对应登录页「验证码登录 + 设置密码」）。

请求：

```json
{
  "phone": "13800138000",
  "code": "123456",
  "password": "可选，首次设置"
}
```

响应 `200`：

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer",
  "expires_in": 7200,
  "user": {
    "id": "usr_xxx",
    "phone": "13800138000",
    "email": "elder@qq.com",
    "display_name": null,
    "preferred_lang": "zh",
    "created_at": "2026-07-29T04:00:00Z"
  }
}
```

---

#### `POST /auth/login/password`

请求：

```json
{ "phone": "13800138000", "password": "******" }
```

响应：同短信登录。

---

#### `POST /auth/password`（需登录）

修改/设置密码。

```json
{ "old_password": "可选", "new_password": "新密码" }
```

响应：`{ "ok": true }`

---

#### `POST /auth/logout`（需登录）

使当前 `access_token` / `refresh_token` 失效（若做服务端会话黑名单）。

响应：`{ "ok": true }`

---

#### `POST /auth/token/refresh`

```json
{ "refresh_token": "..." }
```

响应：新的 `TokenPair`（可不含 `user`）。

---

#### `GET /me`（需登录）

返回当前用户 `UserProfile`（含 `email`，未填则为 `null`）。

#### `PATCH /me`（需登录）

改自己的邮箱 / 昵称（个人中心「修改邮箱」）。

```json
{ "email": "elder@qq.com", "display_name": "可选" }
```

响应 `200` + 更新后的 `UserProfile`。  
错误：`400` `invalid_email`；`409` `email_conflict`。

---

### 2.3 语音与问答 QA

适老化约定：**前端文字提问不必传 session_id**。  
服务端用 Redis（或本地内存回退）按用户缓存当前 `context_id`（即 `qa_sessions.id`）。  
**固定 30 天过期**（自上下文创建起算）：期内继续提问**不会**刷新过期时间；到期后自动换新上下文。

首页语音/文字问询会进入**症状追问**：模型先把不舒服问清楚（一次只问 1～2 个问题），认为信息足够后再给出**初步判断**（含「不能替代医生诊断」提醒）。危险信号会直接进入急救提示。同一上下文内继续说话即可多轮追问；点「新问题」传 `new_context=true`。

初步判断出现后，首页会显示「就医推荐」按钮，调用 `POST /qa/sessions/{session_id}/recommendations`。路径里的 `{session_id}` **就是** `/qa/ask` 的 `context_id`（`qa_sessions.id`）。

`/qa/ask`、`/qa/ask/audio` 为实时 SSE：必须尽早发送 `meta`，收到模型片段就逐个发送 `token`，每帧后 flush，不得先收齐模型全文再发送；响应设置 `Content-Type: text/event-stream`、`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`，关闭 gzip/反向代理缓冲，连接保持 HTTP 流式透传。首个事件应在问诊/模型启动时立即返回，不要等 LLM 全文生成后发 `meta`。

#### `POST /qa/ask`（需登录）——文字输入（SSE 流式）

`Content-Type: text/event-stream`

请求：

```json
{
  "question": "我有点头疼",
  "lang": "zh",
  "new_context": false
}
```

| 字段 | 说明 |
|------|------|
| `question` | 用户输入的文字 |
| `lang` | `zh` \| `en` |
| `new_context` | `true` 时强制新开上下文（如点「新问题」） |

事件顺序（每行 `data: {json}`，空行分隔）：

1. **meta** — 上下文信息（先返回，便于 UI 绑定），含 `intake_round`（当前是第几轮用户陈述）
2. **phase** — `followup`（还要追问）/ `diagnosis`（已给出初步判断）/ `emergency`（需急救）
3. **token** — 对老人说的话的增量，可多次：`{"type":"token","delta":"..."}`（不含阶段标记）
4. **done** — 完整结果（已落库），含 `phase`、`intake_complete`
5. 失败时 **error**：`{"type":"error","code":"...","message":"..."}`

`phase` 可能出现在部分 token 之前或之后，前端以 `done.phase` 为准。`intake_complete=true` 表示本轮已给出初步判断或急救提示。

示例：

```text
data: {"type":"meta","context_id":"...","context_continued":false,"lang":"zh","question_text":"我有点头疼","turn_index_user":1,"turn_index_assistant":2,"intake_round":1,"input_mode":"text"}

data: {"type":"phase","phase":"followup"}

data: {"type":"token","delta":"您头疼几天了？"}

data: {"type":"done","context_id":"...","lang":"zh","question_text":"我有点头疼","answer_text":"您头疼几天了？","phase":"followup","intake_complete":false,"turn_index_user":1,"turn_index_assistant":2,"context_continued":false,"created_at":"2026-07-29T04:01:00Z"}
```

`context_continued=true` 表示沿用未过期的旧上下文（多轮追问）。默认最多 6 轮用户陈述，达到上限后模型必须给出初步判断，可用环境变量 `QA_MAX_FOLLOWUP_TURNS` 调整。

#### `POST /qa/ask/audio`（需登录）——语音输入 → 文本 SSE

`multipart/form-data`，服务端用 OpenAI SDK 以 `input_audio` 调用音频模型，**只流式返回文本**（`modalities=["text"]`）。问诊规则与 `/qa/ask` 相同：先追问症状，清楚后再给初步判断。

| 字段 | 类型 | 说明 |
|------|------|------|
| `file` | file | wav / mp3 |
| `lang` | string | `zh` \| `en` |
| `new_context` | bool | 是否强制新上下文 |
| `prompt` | string | 可选提示语（默认引导模型听录音并按问诊规则作答） |
| `audio_format` | string | 可选 `wav` \| `mp3`（不传则按文件名推断） |

事件顺序同 `/qa/ask`（meta → phase/token → done）。

#### `POST /qa/ask/audio/json`（需登录）——base64 语音

```json
{
  "audio_base64": "...",
  "audio_format": "wav",
  "prompt": "请听录音里老人说的话，按问诊规则继续追问或给出初步判断。",
  "lang": "zh",
  "new_context": false
}
```

#### `POST /qa/context/clear`（需登录）

主动结束当前上下文；下次 `/qa/ask` 会新建。

响应：`{ "ok": true, "context_id": "旧id或null" }`

---

#### `POST /voice/recognize`（需登录，`multipart/form-data`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `file` | file | 音频，建议 `audio/m4a` / `audio/wav` |
| `lang` | string | `zh` \| `en` |

响应：

```json
{ "text": "今天天气怎么样", "duration_ms": 3200 }
```

---

#### `POST /qa/sessions`（需登录）

创建一次问答会话（显式管理 session 时使用；适老化文字输入请优先用 `/qa/ask`）。

```json
{
  "question": "今天天气怎么样",
  "lang": "zh",
  "audio_url": "可选，若已上传音频"
}
```

响应：

```json
{
  "session_id": "qa_xxx",
  "question_text": "今天天气怎么样",
  "answer_text": "今天晴，气温 18～26℃……",
  "lang": "zh",
  "created_at": "2026-07-29T04:01:00Z"
}
```

---

#### `GET /qa/sessions/{session_id}`（需登录）

查询单次问答。

---

### 2.3.1 就医推荐怎么拼（给后端）

对应首页「就医推荐」按钮。前端**只在**本轮已给出初步判断（`phase=diagnosis` 或 `intake_complete=true`）后调用；急救阶段不调。

| 项 | 约定 |
|----|------|
| 接口 | `POST /qa/sessions/{session_id}/recommendations` |
| `{session_id}` | `/qa/ask` / `/qa/ask/audio` 返回的 `context_id`（即 `qa_sessions.id`） |
| 表 | `qa_recommendations`，与会话 **1:1**（`UNIQUE(session_id)`） |
| 鉴权 | 需登录；只能操作本人会话 |

请求体可选：

```json
{ "force": false }
```

| 字段 | 说明 |
|------|------|
| `force` | 默认 `false`：已有推荐且此后没有新的 `qa_messages`，直接返回缓存。`true` 时强制按当前整段对话重新生成 |

行为：

1. 校验会话存在且属于当前用户，否则 `404` `qa_session_not_found`
2. 会话里还没有任何消息：`400` `qa_empty_session`
3. 读该会话全部 `qa_messages`（`ORDER BY turn_index`）作为上下文，生成结构化就医建议
4. upsert 到 `qa_recommendations`（已有则更新 `department` / `care_hint` / `body` / `risk_level` / `updated_at`）
5. **不要**因为仍在追问阶段就 400：前端不会在 `followup` 时点这个按钮；若被调用，按已有对话给**保守**建议即可
6. 对话里已有危险信号：`department` 用「急诊」，`care_hint` 写清「拨打 120 或去急诊」，`risk_level=high`
7. 不要给出确诊病名；用「可能需要…排查」。不要推荐具体医院名、药名、剂量

生成内容要求（适老化）：

- 短句、口语、少术语
- `department`：1～2 个科室，中文全称（如「呼吸内科或全科」），不要英文缩写
- `care_hint`：一句话说去哪看（社区门诊 / 专科门诊 / 急诊）
- `body`：2～4 句解释为什么；**不要**再重复标题和科室名
- `title`：中文固定「推荐就诊方向」，英文可用 `Recommended care direction`
- `disclaimer`：中文固定「本建议不能替代医生诊断」

响应 `200`：

```json
{
  "id": "qr_xxx",
  "session_id": "qa_xxx",
  "title": "推荐就诊方向",
  "department": "呼吸内科或全科",
  "care_hint": "建议先去社区医院门诊；如胸痛或呼吸困难请急诊",
  "body": "您提到胸闷和咳嗽，需要先排查呼吸道和心肺相关问题。如果出现胸痛、喘不上气或意识不清，请立即就医。",
  "risk_level": "medium",
  "disclaimer": "本建议不能替代医生诊断",
  "created_at": "2026-07-29T04:05:00Z",
  "updated_at": "2026-07-29T04:05:00Z"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | `qa_recommendations.id` |
| `session_id` | 是 | 与路径参数相同，等于问询的 `context_id` |
| `title` | 是 | 卡片标题 |
| `department` | 是 | 推荐科室，前端大字展示 |
| `care_hint` | 是 | 去哪看 |
| `body` | 是 | 原因说明 |
| `risk_level` | 是 | `low` 可常规就诊 / `medium` 尽快就诊 / `high` 尽早去医院 |
| `disclaimer` | 是 | 免责声明 |
| `created_at` | 是 | 首次生成时间，ISO 8601 UTC |
| `updated_at` | 是 | 最近一次生成时间 |

错误：

| HTTP | `code` | 何时 |
|------|--------|------|
| 400 | `qa_empty_session` | 会话还没有消息 |
| 401 | （通用） | 未登录 |
| 404 | `qa_session_not_found` | 会话不存在或不是本人 |
| 500 | `recommend_failed` | 模型或落库失败 |

前端：`src/services/qaApi.ts` 的 `requestMedicalRecommendation`；超时按至少 60 秒预留（生成可能走大模型）。

**不要**另做 `GET /timeline` 式的推荐列表；**不要**把推荐写入档案表。同一会话点第二次按钮，前端会先用本地缓存，后端也应返回已有行（除非对话又增加了或 `force=true`）。

---

### 2.4.0 档案展示页怎么拼（给后端）

前端**不要求**统一时间线接口。展示页只读下面 5 个 GET，时间轴由前端把体检列表和就诊列表按日期倒序合并。

| 页面 | 接口 |
|------|------|
| 档案首页总结卡片 | `GET /health-summaries`（查看父母时附 `owner_user_id`） |
| 时间轴 · 体检 | `GET /health-reports?page=1&page_size=100`（查看父母时附 `owner_user_id`） |
| 时间轴 · 就诊 | `GET /archives?page=1&page_size=100`（查看父母时附 `owner_user_id`） |
| 详情 · 体检 | `GET /health-reports/{id}` |
| 详情 · 就诊 | `GET /archives/{id}` |

约定：

- 只返回当前用户、`deleted_at IS NULL` 的数据
- 列表为空返回 `{ "items": [] }`（可带 `total: 0`），不要 404
- 详情找不到或不是本人：`404`
- 日期字段：`YYYY-MM-DD`；时间字段：ISO 8601 UTC
- 两个列表接口前端用 `Promise.allSettled`：一边失败另一边仍展示
- **不要**做 `GET /timeline`；**不要**把就诊写入 `health_reports` 或把体检写入 `medical_archives`
- 拍照 OCR 只调 `POST /archives/ocr`：后端会分类并入库；成功后刷新两个列表即可

`GET /report-glossaries` 前端不调用。术语请内嵌在 `GET /health-reports/{id}` 的 `glossary`。

---

### 2.4.1 健康问题总结（档案首页卡片）

对应表：`health_summaries` + `health_summary_items`

#### `GET /health-summaries`（需登录）

按 `updated_at` 倒序返回当前用户的总结列表；每条带 `items`（按 `sort_order ASC`）。

`exam_date`、`exam_no`、`severity` 均可为 `null`。

响应：

```json
{
  "items": [
    {
      "id": "hs_xxx",
      "title": "健康问题总结",
      "exam_date": "2025-11-03",
      "exam_no": "312101033225",
      "summary_text": "综合近期体检与就诊记录……",
      "items": [
        {
          "id": "hsi_1",
          "content": "体重指数偏低（BMI 18.2），需加强营养与适量运动",
          "severity": "medium",
          "sort_order": 0
        }
      ],
      "created_at": "2026-07-29T04:02:00Z",
      "updated_at": "2026-07-29T04:02:00Z"
    }
  ]
}
```

前端：第一条含 `items` 的记录展示为「健康问题总结」主卡片；其余展示为普通总结卡片。

---

### 2.4.2 健康档案报告（体检时间轴 / 详情）

对应表：`health_reports` + `health_report_findings`；术语来自 `report_glossaries`（全局，无 `user_id`）

前端时间轴会把本列表与 `GET /archives` **合并后按日期倒序**。本接口只返回体检。

#### `GET /health-reports`（需登录）

| 参数 | 说明 |
|------|------|
| `page` | 默认 1 |
| `page_size` | 默认 20，最大 100；前端展示页传 `100` |

`ORDER BY exam_date DESC, created_at DESC`。  
列表**不要**带 `findings` / `full_text` / `glossary`。

```json
{
  "items": [
    {
      "id": "hr_xxx",
      "patient_name": "毕小雪",
      "exam_date": "2025-11-03",
      "org_name": "瑞慈体检上海静安机构",
      "voucher_no": "312101033225",
      "report_type": "体检报告"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 100
}
```

#### `GET /health-reports/{id}`（需登录）

`findings` 按 `sort_order ASC`。  
`full_text` 从 `health_reports.raw_payload.full_text` 取出（不要把整份 JSON 返回前端）；没有则 `null`，前端用占位文案。  
`glossary`：**请内嵌返回**（`report_glossaries WHERE enabled = 1 ORDER BY sort_order`）。

```json
{
  "id": "hr_xxx",
  "patient_name": "毕小雪",
  "exam_date": "2025-11-03",
  "org_name": "瑞慈体检上海静安机构",
  "voucher_no": "312101033225",
  "report_type": "体检报告",
  "findings": [
    {
      "id": "hrf_1",
      "title": "【1】体重过低。体重指数 BMI 值偏低（18.2）。",
      "suggestion": "建议平衡膳食，适量运动，定期复查体重。",
      "risk_level": "medium",
      "sort_order": 0
    }
  ],
  "full_text": "完整报告正文……",
  "glossary": [
    {
      "id": "g1",
      "term": "随诊",
      "definition": "如有不适，及时就诊。",
      "sort_order": 0
    }
  ]
}
```

#### `GET /report-glossaries`（需登录，可选）

前端展示页不调用。若已在报告详情内嵌 `glossary`，本接口可暂缓。

```json
{
  "items": [
    {"id": "g1", "term": "随诊", "definition": "如有不适，及时就诊。", "sort_order": 0}
  ]
}
```

---

### 2.4 档案 Archives（就诊单列表 / OCR）

对应表：`medical_archives`、`archive_ocr_jobs`（识别中间态）、体检写入 `health_reports`

展示页只读 `GET /archives`、`GET /archives/{id}`。  
**拍照/相册识别请只调 `POST /archives/ocr`**（后端分类并入库）。`POST /archives` 仅用于用户手工补录就诊单，不要在 OCR 成功后再用同一张单去创建。

#### `POST /archives/ocr`（需登录，`multipart/form-data`）

前端传一张图即可。后端用视觉模型判断类型并**立刻写入对应表**，返回已落库的 `id`。

| 字段 | 说明 |
|------|------|
| `file` | 图片，jpg / png / webp / gif，建议不超过 10MB |
| `source` | `camera` \| `album` |

建议超时 **≥ 60s**（识图比普通接口慢）。`config/api.json` 的 `timeoutMs` 若仍是 `30000`，OCR 请求请单独加长。

识别规则：

| `document_type` | 含义 | 写入表 | 返回的 `id` | 之后读哪个接口 |
|-----------------|------|--------|-------------|----------------|
| `visit` | 就诊单 / 处方 / 病历 / 出院小结 | `medical_archives` | 就诊单 id | `GET /archives`、`GET /archives/{id}` |
| `exam` | 体检报告 / 健康体检 | `health_reports` + findings | 体检报告 id | `GET /health-reports`、`GET /health-reports/{id}` |

前端流程：

1. `POST /archives/ocr`（`file` + `source`）
2. 看 `document_type` + `id` 决定跳转详情或刷新时间轴
3. **不要**再 `POST /archives` 把本次结果存一遍（同一 `visit_no` / `voucher_no` 会 `409`）
4. 时间轴仍用 `Promise.allSettled` 拉 `GET /health-reports` + `GET /archives`

响应字段（两种类型同一 JSON 形状；就诊单时体检字段为 `null` / `[]`）：

| 字段 | 就诊单 `visit` | 体检单 `exam` |
|------|----------------|---------------|
| `document_type` | `"visit"` | `"exam"` |
| `id` | 就诊单 id | 体检报告 id |
| `diagnosis` | 诊断白话 | 首条异常标题（兼容旧字段） |
| `medicine` | 用药/医嘱白话 | 多为「见体检建议」 |
| `visit_date` | 就诊日 `YYYY-MM-DD` | 等于 `exam_date` |
| `visit_no` | 就诊号 | 等于 `voucher_no` |
| `raw_ocr_text` | 全文 | 全文（详情 `full_text` 同源） |
| `patient_name` | `null` | 姓名 |
| `org_name` | `null` | 机构 |
| `voucher_no` | `null` | 体检凭证号 |
| `report_type` | `null` | 如「体检报告」 |
| `findings` | `[]` | 异常项列表 |

就诊单 `200` 示例：

```json
{
  "document_type": "visit",
  "id": "arc_xxx",
  "diagnosis": "支气管炎倾向，建议复查",
  "medicine": "按医嘱服用止咳药，注意饮水",
  "visit_date": "2026-07-27",
  "visit_no": "MZ202607270018",
  "raw_ocr_text": "原始 OCR 全文……",
  "patient_name": null,
  "org_name": null,
  "voucher_no": null,
  "report_type": null,
  "findings": []
}
```

体检单 `200` 示例：

```json
{
  "document_type": "exam",
  "id": "hr_xxx",
  "diagnosis": "体重过低 BMI 18.2",
  "medicine": "见体检建议",
  "visit_date": "2025-11-03",
  "visit_no": "312101033225",
  "raw_ocr_text": "完整报告正文……",
  "patient_name": "毕小雪",
  "org_name": "瑞慈体检上海静安机构",
  "voucher_no": "312101033225",
  "report_type": "体检报告",
  "findings": [
    {
      "title": "体重过低 BMI 18.2",
      "suggestion": "建议平衡膳食，适量运动，定期复查体重。",
      "risk_level": "medium",
      "sort_order": 0
    }
  ]
}
```

`findings[].risk_level`：`low` \| `medium` \| `high`，可 `null`。OCR 返回的 findings **没有** `id`；详情 `GET /health-reports/{id}` 才有 `id`。

错误：

| HTTP | `code` | 何时 |
|------|--------|------|
| 400 | `empty_file` | 空文件 |
| 400 | `image_too_large` | 超过约 10MB |
| 400 | `unsupported_image_type` | 非 jpg/png/webp/gif |
| 401 | （未登录） | 缺 token / token 无效 |
| 409 | `visit_no_conflict` | 该就诊号已存在 |
| 409 | `voucher_no_conflict` | 该体检凭证号已存在 |
| 422 | `invalid_source` | `source` 不是 camera/album |
| 502 | `ocr_invalid_json` / `ocr_empty_response` / `openai_upstream_error` | 模型识别失败 |

`409` 时识别可能已成功但未写入（号冲突）。前端提示「该单已在档案中」并刷新列表即可，不要换号强行再存同一张图。

---

#### `GET /archives`（需登录）

就诊单列表。前端与 `GET /health-reports` 合并成「健康档案报告」时间轴。  
时间轴卡片会用到：`diagnosis`、`medicine`、`visit_date`、`visit_no`。

`ORDER BY visit_date DESC, created_at DESC`。

查询参数：

| 参数 | 说明 |
|------|------|
| `q` | 关键词搜索；前端展示页不传，可暂缓 |
| `page` | 默认 1 |
| `page_size` | 默认 20，最大 100；前端展示页传 `100` |

响应：

```json
{
  "items": [ /* ArchiveRecord */ ],
  "total": 2,
  "page": 1,
  "page_size": 100
}
```

`ArchiveRecord`：

```json
{
  "id": "arc_xxx",
  "diagnosis": "...",
  "medicine": "...",
  "visit_date": "2026-07-27",
  "visit_no": "MZ202607270018",
  "raw_ocr_text": "...",
  "image_url": "https://...",
  "created_at": "2026-07-29T04:02:00Z",
  "updated_at": "2026-07-29T04:02:00Z"
}
```

列表里 `raw_ocr_text` / `image_url` 可空；详情接口必须能返回 `raw_ocr_text`（没有则 `null`）。

---

#### `POST /archives`（需登录）

**手工补录就诊单**（不是 OCR 成功后的下一步）。同一用户下 `visit_no` 未删除时唯一。  
不要用来保存体检单。

```json
{
  "diagnosis": "...",
  "medicine": "...",
  "visit_date": "2026-07-27",
  "visit_no": "MZ202607270018",
  "raw_ocr_text": "可选",
  "image_url": "可选"
}
```

响应：`201` + `ArchiveRecord`

---

#### `GET /archives/{id}`（需登录）

就诊单详情。前端「完整报告」Tab 使用 `raw_ocr_text`。校验所属用户，否则 `404`。

响应：`200` + `ArchiveRecord`（同上）。

#### `PATCH /archives/{id}`（需登录）

```json
{ "diagnosis": "...", "medicine": "...", "visit_date": "2026-07-27", "visit_no": "MZ202607270018" }
```

#### `DELETE /archives/{id}`（需登录）

响应：`{ "ok": true }`

---

#### `POST /archives/{id}/share`（需登录）

把**就诊单**推给已添加的子女。只推当前用户、未删除的联系人。  
前端会先 `GET /family/contacts`，用户勾选后再调用本接口。

请求：

```json
{
  "contact_ids": ["ctc_1", "ctc_2"],
  "message": "可选留言，给子女看",
  "attach_pdf": true
}
```

| 字段 | 说明 |
|------|------|
| `contact_ids` | 必填，至少 1 个；必须是本人联系人 |
| `message` | 可选，最长 200 字 |
| `attach_pdf` | 可选，默认 `true`：邮件附带该单 PDF（或正文里放下载链接） |

响应 `200`：

```json
{
  "ok": true,
  "shared_count": 2,
  "failed_count": 0
}
```

`shared_count` = 成功写入 `archive_shares` 的条数（含 `queued`）。部分失败时仍 `200`，`failed_count > 0`。

错误：

| HTTP | `code` | 何时 |
|------|--------|------|
| 400 | `empty_contact_ids` | 没选人 |
| 400 | `invalid_contact` | id 不属于当前用户 / 已删除 |
| 404 | — | 就诊单不存在或不是本人 |

后端行为：

1. 校验档案与联系人归属
2. 按人写 `archive_shares`（`status=queued`）
3. 异步发送（短信 / 推送）；成功改 `sent`，失败改 `failed`
4. 手工推送**不受** `family_push_rules` 拦截（规则只管自动推）
5. `attach_pdf=true` 时生成或复用该单 PDF，**用 QQ 邮箱 SMTP 发到每位联系人的 `email`**（不要发短信）。联系人没有邮箱则该条记 `failed`，计入 `failed_count`。

---

#### `GET /archives/{id}/export`（需登录）

生成就诊单 PDF，返回**限时下载链接**（不要把 PDF 字节直接塞进 JSON，避免老机型卡死）。

同步生成，建议在 **30s** 内返回。前端 `timeoutMs` 对导出请求单独加长到 60s。

响应 `200`：

```json
{
  "download_url": "https://.../tmp/visit-MZ202607270018.pdf",
  "expires_in": 600,
  "filename": "就诊单-MZ202607270018.pdf",
  "status": "ready"
}
```

| 字段 | 说明 |
|------|------|
| `download_url` | 可 GET 的绝对 URL（HTTPS；开发期可用 HTTP） |
| `expires_in` | 秒，建议 600 |
| `filename` | 建议文件名，前端作下载名 |
| `status` | 固定 `ready`（本契约不做异步轮询） |

错误：`404` 档案不存在；`502` + `code=pdf_generate_failed` 生成失败。

PDF 内容最低要求（就诊单）：就诊号、日期、诊断、用药、原始识别摘要。

---

### 2.4.3 体检报告推送 / 导出

与就诊单同一套交互：档案确认页、报告详情都可以点「推送子女」「下载 PDF」。

#### `POST /health-reports/{id}/share`（需登录）

请求/响应/错误码与 `POST /archives/{id}/share` **完全相同**。  
写入表 `report_shares`（不要写进 `archive_shares`）。

#### `GET /health-reports/{id}/export`（需登录）

响应形状与 `GET /archives/{id}/export` **完全相同**。  
写入表 `report_exports`。

PDF 内容最低要求（体检）：姓名、机构、检查号、日期、异常项+建议、完整正文可截取前若干页。

---

### 2.5 个人中心 Preferences / Family

#### `GET /me/preferences`（需登录）

```json
{ "preferred_lang": "zh" }
```

#### `PATCH /me/preferences`（需登录）

```json
{ "preferred_lang": "en" }
```

---

### 2.5.1 子女联系人

对应表：`family_contacts`。前端个人中心「人员配置」。

`relation`：`daughter` \| `son` \| `other`  
`notify_enabled`：是否接收**自动推送**（规则触发时）。手工点「推送子女」仍可选此人。

同一用户、未删除联系人中，`phone`、`email` 分别唯一。最多 **10** 人。  
**推送通道：QQ 邮箱 SMTP → 子女 `email`。** `phone` 仅作展示/备用，不发短信。

#### `GET /family/contacts`（需登录）

只返回 `deleted_at IS NULL`，按 `created_at ASC`。空列表返回 `{ "items": [] }`，不要 404。

```json
{
  "items": [
    {
      "id": "ctc_1",
      "name": "女儿 王女士",
      "phone": "13900000001",
      "email": "daughter@qq.com",
      "relation": "daughter",
      "notify_enabled": true,
      "created_at": "2026-07-29T04:00:00Z"
    }
  ]
}
```

#### `POST /family/contacts`（需登录）

```json
{
  "name": "儿子",
  "phone": "13900000002",
  "email": "son@qq.com",
  "relation": "son",
  "notify_enabled": true
}
```

| 字段 | 说明 |
|------|------|
| `name` | 必填，1～64 字 |
| `phone` | 必填，大陆 11 位手机号 `1XXXXXXXXXX` |
| `email` | **必填**，推送发到此地址；存小写 |
| `relation` | 可选，默认 `other` |
| `notify_enabled` | 可选，默认 `true` |

响应 `201` + `FamilyContact`。

错误：

| HTTP | `code` | 何时 |
|------|--------|------|
| 400 | `invalid_phone` | 手机号格式不对 |
| 400 | `invalid_email` | 邮箱格式不对 |
| 400 | `too_many_contacts` | 已有 10 个未删除联系人 |
| 409 | `contact_phone_conflict` | 该手机号已添加 |
| 409 | `contact_email_conflict` | 该邮箱已添加 |

#### `PATCH /family/contacts/{id}`（需登录）

部分更新。可只改 `notify_enabled`（个人中心「接收推送」开关）。

```json
{
  "name": "女儿",
  "phone": "13900000001",
  "email": "daughter@qq.com",
  "relation": "daughter",
  "notify_enabled": false
}
```

响应 `200` + 更新后的 `FamilyContact`。  
`404`：不是本人或已删除。`409`：改手机号或邮箱冲突。

#### `DELETE /family/contacts/{id}`（需登录）

软删除：`deleted_at` 置值。响应 `{ "ok": true }`。如果联系人已关联子女账号，删除后立即撤销该子女查看父母报告的权限。

---

### 2.5.2 子女查看父母报告（只读授权）

目标：老人添加家属联系人并将关系设置为 `daughter` 或 `son` 后，若该联系人的手机号已注册 App 账号，子女登录后可在报告页选择父母，查看父母的健康总结、体检报告和就诊单时间轴及详情。列表/详情字段、时间轴排序和报告格式沿用老人本人报告，不复制或迁移数据。

**授权规则（服务端强制，不能由客户端自行判定）：**

建议直接从未删除的 `family_contacts` 关系和双方规范化手机号实时计算授权，无需另建容易与联系人状态不一致的权限表；如采用缓存/独立授权记录，更新联系人或删除时必须在同一事务内同步撤销。

1. 仅当父母账号 `family_contacts` 中存在未删除联系人，且 `relation` 为 `daughter` / `son`、联系人手机号与已登录子女账号手机号规范化后完全一致时，建立有效授权。添加联系人本身即代表老人授予该子女只读查看报告的权限。
2. 联系人暂未注册时，仍可先保留这条家属关系；子女注册/登录后按手机号匹配即可生效，无需老人重复添加。
3. 父母将关系改为 `other`、删除联系人，或手机号改为不匹配值时，立即撤销关联。所有跨账号读请求均需实时校验有效关联（或使用保证即时撤销的授权记录）；旧 JWT 不得延续已撤销的权限。
4. `GET /family/parents` 只返回当前登录子女有权查看的父母账号。返回的 `id` 是父母 `user_id`，用于后续请求的 `owner_user_id`。
5. 跨账号范围仅包括 `GET /health-summaries`、`GET /health-reports`、`GET /archives` 和对应的报告/就诊详情 GET。`POST/PATCH/DELETE`、OCR、分享、导出、问答和个人资料仍只能操作登录者本人；不得因家庭关联扩大写权限。
6. 未授权的父母、资源不存在或不属于该父母时，统一返回 `404`，避免通过响应探测账号或报告是否存在。未登录仍返回 `401`。

#### `GET /family/parents`（需登录，子女账号）

获取当前账号有权查看的父母列表。空列表返回 `{ "items": [] }`。按授权联系人创建时间排序；手机号应脱敏展示。

```json
{
  "items": [
    {
      "id": "usr_parent_1",
      "display_name": "王阿姨",
      "phone": "138****8000",
      "relation": "daughter"
    }
  ]
}
```

#### 家庭报告列表参数

子女先选定 `GET /family/parents` 返回的父母，再在下列接口传 `owner_user_id`。老人本人调用时不传，默认当前用户。服务端必须验证 `owner_user_id` 与当前用户间存在上述有效关系；分页、排序和响应格式均与本人查询完全一致。

- `GET /health-summaries?owner_user_id=usr_parent_1`
- `GET /health-reports?owner_user_id=usr_parent_1&page=1&page_size=100`
- `GET /archives?owner_user_id=usr_parent_1&page=1&page_size=100`

访问详情时仍使用现有详情路由（`GET /health-reports/{report_id}`、`GET /archives/{archive_id}`）。服务端先定位记录所属 `user_id`，再确认它是本人或 `owner_user_id` 授权范围内的记录；详情响应格式不变。详情路由不接受客户端传入的 owner 覆盖资源真实所属人。

---

### 2.5.3 推送权限与异常指标

对应表：`family_push_rules`，与用户 1:1。没有行时 **GET 返回默认值并建行**：

```json
{
  "on_record_saved": true,
  "on_abnormal": false,
  "on_visit": true,
  "abnormal_metrics": ["bmi", "blood_pressure", "blood_lipid", "blood_glucose", "liver", "kidney", "other"]
}
```

三个开关 = **自动推送权限**（什么时候推）：

| 字段 | 个人中心文案 | 后端何时自动推 |
|------|----------------|----------------|
| `on_record_saved` | 就诊单保存后推送 | OCR/保存就诊单成功后，推给 `notify_enabled=true` 的联系人 |
| `on_abnormal` | 异常指标提醒推送 | 体检入库且 findings 命中下方指标时推送 |
| `on_visit` | 就诊复查提醒推送 | 就诊单含复查/随诊语义，或到达建议复查日时推送（可先做「保存时若文案含复查则推」） |

`abnormal_metrics` = **推哪些指标**（仅当 `on_abnormal=true` 时生效）：

| 取值 | 含义 | 匹配建议（finding 标题/建议含） |
|------|------|--------------------------------|
| `bmi` | 体重 / BMI | BMI、体重、体重指数 |
| `blood_pressure` | 血压 | 血压、收缩压、舒张压 |
| `blood_lipid` | 血脂 | 血脂、胆固醇、甘油三酯、LDL、HDL |
| `blood_glucose` | 血糖 | 血糖、葡萄糖、HbA1c |
| `liver` | 肝功能 | 肝、ALT、AST、转氨酶 |
| `kidney` | 肾功能 | 肾、肌酐、尿素 |
| `other` | 其他异常 | 未命中以上类别的 findings |

约定：

- 必须是上表枚举；未知值 `422`
- `on_abnormal=true` 且数组为空：视为**全部指标**
- `on_abnormal=false`：忽略 `abnormal_metrics`，不要自动推异常
- 自动推只发给 `notify_enabled=true` 的联系人；0 个联系人则静默跳过（不要 4xx）
- **手工**「推送子女」忽略本规则

#### `GET /family/rules`（需登录）

返回当前用户规则（无则默认值）。

#### `PUT /family/rules`（需登录）

全量替换四个字段（前端开关一改就 PUT 整份）。

```json
{
  "on_record_saved": true,
  "on_abnormal": true,
  "on_visit": false,
  "abnormal_metrics": ["bmi", "blood_glucose"]
}
```

响应 `200` + 与请求相同形状的规则对象。

---

### 2.5.4 后端本轮必做（给实现者）

前端已接好。后端按下面清单即可联调：

1. **子女**：`GET/POST /family/contacts`、`PATCH/DELETE /family/contacts/{id}`（含 `email`、`notify_enabled`，最多 10 人，手机号/邮箱未删除唯一）
2. **权限与指标**：`GET/PUT /family/rules`（三个开关 + `abnormal_metrics`）；无行时 GET 要建默认行
3. **手工推送**：`POST /archives/{id}/share`、`POST /health-reports/{id}/share`。**用 QQ 邮箱 SMTP 发到联系人 `email`**，不要发短信。写 shares 表；成功 `sent`，SMTP 失败 `failed`
4. **下载 PDF**：`GET /archives/{id}/export`、`GET /health-reports/{id}/export`（同步生成，返回 `download_url` + `filename`，链接约 10 分钟过期）
5. **自动推送**（可与 3 同套发信）：保存就诊单看 `on_record_saved`；体检 findings 看 `on_abnormal` + 指标；复查语义看 `on_visit`。只发给 `notify_enabled=true` 且有 `email` 的人
6. **账号邮箱**：注册 `POST /auth/register` 必填 `email`；个人中心 `PATCH /me` 可改。存 `users.email`
7. 表结构以 [`docs/database/schema.sql`](./database/schema.sql) 为准。若库已建过，请 `ALTER` 增加 `users.email` / `family_contacts.email`（及对应生成列、唯一键），不要只改代码。
8. **子女只读查看父母报告**：手机号精确关联 + 家庭关系为儿子/女儿；实现 `GET /family/parents`，并为健康总结、体检列表和就诊列表支持可选 `owner_user_id`。详情 GET 根据记录真实 owner 做授权校验；授权撤销后即时失效。不得放开写接口。
9. OpenAPI：[`docs/openapi.yaml`](./openapi.yaml)

发信（QQ 邮箱 SMTP）环境变量建议：

```env
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的QQ号@qq.com
SMTP_PASS=QQ邮箱SMTP授权码
SMTP_FROM=你的QQ号@qq.com
```

QQ 邮箱需在网页版开启 SMTP，并用**授权码**而不是 QQ 密码。Python 可用 `aiosmtplib` 或 `fastapi-mail`。`attach_pdf=true` 时把 PDF 作为附件（或正文放 `download_url`）。

PDF 可用 weasyprint / reportlab，文件进 `media_files` 或对象存储。

---

## 3. FastAPI 落地建议

目录建议：

```text
backend/
  app/
    main.py
    api/v1/
      router.py
      auth.py
      qa.py
      archives.py
      family.py
    schemas/
    services/
    models/
  docs/  # 可直接引用本仓库 openapi.yaml
```

最小挂载示例：

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Senior Voice API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# include_router(..., prefix="/api/v1")
```

开发时可把本仓库的 `docs/openapi.yaml` 作为契约：先按 schema 写 Pydantic 模型，再实现业务。

档案页展示接口可直接参考：

```text
docs/backend/archive_routes_example.py   # 已有总结/体检；就诊 GET /archives 需按契约补
docs/database/seed_archive.sql
docs/backend/README.md
docs/openapi.yaml                        # 已含 health-summaries / health-reports / archives
```

---

## 4. 与前端页面对应关系

| App 页面 | 主要接口 |
|----------|----------|
| 注册 | `POST /auth/sms/send`（purpose=register）、`POST /auth/register`（必填 `email`） |
| 登录（验证码） | `POST /auth/sms/send`、`POST /auth/login/sms` |
| 登录（密码） | `POST /auth/login/password` |
| 首页文字输入 | `POST /qa/ask`（服务端托管上下文；模型追问症状至足够后给初步判断） |
| 首页按住说话 | `POST /qa/ask/audio`（同样走症状追问；也可先 `POST /voice/recognize` 再 `/qa/ask`） |
| 首页就医推荐 | `POST /qa/sessions/{id}/recommendations`（`{id}` = `context_id`） |
| 结束当前对话 | `POST /qa/context/clear` |
| 档案 OCR（拍照/相册） | **只调** `POST /archives/ocr`（`file`+`source`）。`visit`→就诊表，`exam`→体检表；用返回的 `id` 进详情。不要再 `POST /archives` |
| 档案首页总结 | `GET /health-summaries`（表 `health_summaries` / `health_summary_items`） |
| 健康档案报告时间轴 | 前端合并 `GET /health-reports` + `GET /archives`（不要单独做 timeline 接口） |
| 体检详情 | `GET /health-reports/{id}`（`findings` + `full_text` + 内嵌 `glossary`） |
| 就诊详情 | `GET /archives/{id}`（`raw_ocr_text` 作完整报告） |
| 报告术语 | 详情内嵌 `glossary`；`GET /report-glossaries` 可选 |
| 推送子女（就诊单） | `GET /family/contacts` → 勾选 → `POST /archives/{id}/share` |
| 下载 PDF（就诊单） | `GET /archives/{id}/export` → 打开 `download_url` |
| 推送子女（体检） | `GET /family/contacts` → 勾选 → `POST /health-reports/{id}/share` |
| 下载 PDF（体检） | `GET /health-reports/{id}/export` → 打开 `download_url` |
| 添加 / 编辑 / 删除子女 | `GET/POST /family/contacts`、`PATCH/DELETE /family/contacts/{id}` |
| 推送权限与指标 | `GET/PUT /family/rules`（开关 + `abnormal_metrics`）；联系人 `notify_enabled` |
| 子女查看父母报告 | `GET /family/parents` → 选择父母 → 总结/体检/就诊列表加 `owner_user_id`；详情 GET 服务端校验真实归属 |
| 个人中心其它 | `GET/PATCH /me`（含邮箱）、`/me/preferences`、`POST /auth/password`、`POST /auth/logout` |

前端调用入口：

```text
src/services/authApi.ts
src/services/qaApi.ts
src/services/archiveApi.ts
src/services/profileApi.ts
src/services/http.ts
```

---

## 5. 改 IP 打包（前端）

1. 编辑 `config/api.json`：

```json
{
  "apiBaseUrl": "http://10.0.0.8:8000",
  "apiPrefix": "/api/v1",
  "timeoutMs": 30000
}
```

2. 重新打包：

```bash
npm run build:android
```

或临时覆盖：

```bash
API_BASE_URL=http://10.0.0.8:8000 npm run build:android
```

真机调试不要用 `localhost` / `127.0.0.1`，请用电脑局域网 IP。
