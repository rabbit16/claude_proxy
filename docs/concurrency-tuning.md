# 代理并发优化说明

面向学习：这次改动解决什么问题、为什么这么改、每块代码在干什么。

## 1. 先建立正确的容量模型

这个服务不是短连接 API 网关，而是 **LLM 反向代理**：

```
客户端 (Cursor / Claude Code)
    → FastAPI 代理 (4000)
        → LiteLLM (4001)
            → 模型供应商
```

请求几乎全是 **SSE 流式**。一路对话会把连接挂住 20–60 秒，Agent 场景一个人常占 2–3 路。

因此容量公式是：

```
完成 QPS ≈ 同时在飞的流 / 平均耗时
同时在线人数 ≈ 同时在飞的流 / 每人占用路数
```

不要按「这个服务能抗多少 QPS」规划。抗的是 **同时挂着的 SSE**。

举例：平均一路占 30 秒、同时 32 路流：

- 完成 QPS ≈ 32 / 30 ≈ **1 QPS**
- 每人 1 路 ≈ 32 人；每人 2 路 ≈ 16 人

QPS 看起来很低是正常的，因为连接一直占着。

## 2. 改之前卡在哪

| 环节 | 旧行为 | 后果 |
|---|---|---|
| gunicorn | `workers=2`，没配 `UvicornWorker` | 默认 sync worker，流式场景大约同时只能处理 2 个请求 |
| `python main.py` | `uvicorn.run(app)` 单进程 | 本机还能撑，但 httpx 默认最多约 100 条上游连接 |
| httpx | 未设 `Limits` | 默认 `max_connections=100`，打满后新请求卡 `pool=30s` |
| 鉴权 | 每次请求查远程 MySQL 整行 `User` | 突发短请求先把连接池打满 |
| FastAPI | `debug=True` | 额外校验和 traceback，生产不该开 |
| 压缩 | Headroom 走默认线程池 | 大 context 压缩会堵事件循环 |
| 云主机 | 按本机 20C/32G 的数字去跑 | 2C/1.8G 开 4 worker 会 OOM |

真正的天花板几乎一定是 **LiteLLM + 模型供应商配额**。代理只是把请求转出去。优化目标是：别让代理自己先成为瓶颈，也别在小机器上把自己打爆。

## 3. 机器差异，所以要自动选档

本机和云主机差一个数量级：

| | 本机 | 云主机 `60.204.149.187` |
|---|---|---|
| CPU | i7-12700KF，20 线程 | 2 核 |
| 内存 | 32G，可用约 12G | 1.8G，可用约 777MB |
| 已占 | 桌面进程 | LiteLLM ~300MB + MySQL/nginx |
| Swap | 几乎空 | 1G 已用一半 |

同一套硬编码数字，本机合适，云上会内存爆掉。所以抽了 `settings/capacity.py`，启动时读 CPU / `/proc/meminfo`。

### 三档

| 条件 | 档位 | worker | 上游同时流 | httpx 连接 | 压缩线程 | MySQL 池 | 入口并发 |
|---|---|---|---|---|---|---|---|
| CPU≤2 或 内存<3G | `small` | 1 | 32 | 48 | 2 | 4+4 | 40 |
| CPU≥8 且 内存≥16G | `desktop` | 4 | 80 | 120 | 4 | 8+8 | 100 |
| 其余 | `medium` | 2 | 48 | 80 | 3 | 6+6 | 64 |

云主机走 `small`，本机走 `desktop`。环境变量可覆盖，一般不用动：

```bash
PROXY_WORKERS=1
PROXY_UPSTREAM=24
PROXY_HTTP_MAX=48
PROXY_COMPRESS_THREADS=2
PROXY_DB_POOL=4
PROXY_DB_OVERFLOW=4
PROXY_LIMIT_CONCURRENCY=40
PROXY_PROFILE=small
```

启动日志应类似：

```text
proxy started profile=small cpus=2 mem_mb=1800 workers=1 upstream=32 http_max=48 compress=2 db_pool=4+4
```

云上看到 `profile=small workers=1` 就对了。如果看到 `desktop` 或 `workers=4`，说明没读到新代码或环境变量被强制覆盖。

## 4. 请求生命周期（改完之后）

```
1. 客户端进来
2. 鉴权 check_user_message
      命中 5 秒缓存 → 直接换 master key
      未命中 → 只查 telephone/token
3. 可选：Headroom 压缩（线程池，不堵事件循环）
4. 拿 upstream_sema（同时打上游的上限）
5. httpx 转发到 LiteLLM（连接池有上限）
6. SSE 边读边计费
7. 余额扣到 ≤0 → 立刻清鉴权缓存
```

第 4、5 步是保护本机 LiteLLM 和内存的闸门，不是为了「尽量多转发」。

## 5. 分项说明

### 5.1 ASGI worker：流式必须异步

`gunicorn.conf.py` 关键行：

```python
worker_class = "uvicorn.workers.UvicornWorker"
workers = CAPACITY["workers"]
timeout = 0
```

- **sync worker**：一个 worker 处理一个请求直到结束。SSE 挂 30 秒，这个 worker 就闲置不了。2 个 sync worker ≈ 同时 2 路流。
- **UvicornWorker**：一个进程里事件循环并发处理很多挂起的流。适合 IO 等待（读上游、写客户端）。
- `timeout = 0`：LLM 流可能超过 gunicorn 默认 30 秒，不能按短请求切连接。

`python main.py` 也按档启动：多 worker 必须传 `"main:app"` 字符串（子进程重新 import）；单进程直接传 `app`，少一次 import。云上是单进程。

`post_fork` 里 `async_engine.sync_engine.dispose()`：父进程建过的连接不能带到子进程，否则连接串台。

### 5.2 httpx 连接池 + 信号量

```python
httpx.Limits(max_connections=..., max_keepalive_connections=..., keepalive_expiry=30)
asyncio.Semaphore(UPSTREAM_CONCURRENCY)
```

两层限制含义不同：

| 限制 | 作用 |
|---|---|
| `max_connections` | 进程内 TCP 连接上限，防止 fd / 内存涨死 |
| `max_keepalive_connections` | 复用空闲连接，减少反复握手 |
| `Semaphore` | 业务并发上限，比连接池更紧，先排队而不是把 LiteLLM 打满 |

流式路径：

```python
async with sema:
    async with client.stream(...) as resp:
        ...
```

信号量包住整个流的生命周期，不是只包「发起请求」那一下。否则 32 个流都会同时占着上游。

`read=None` 仍然保留：SSE 结束前没有读超时。`pool=60` 是「等连接池空位」的时间。

### 5.3 鉴权短缓存

文件：`utils/check_user.py`

旧逻辑：每个请求 `select(User).filter_by(telephone=...)` 拉整行。

新逻辑：

1. 进程内 dict：`{user_id: (过期时间, "ok"|"no money"|"not exist")}`
2. 成功缓存 **5 秒**，失败 **2 秒**
3. 未命中只查 `telephone, token` 两列
4. 扣费后余额 ≤0，调用 `invalidate_user_auth` 立刻失效

为什么 TTL 这么短：

- 鉴权只关心「有没有这个人、还有没有余额」
- 5 秒内同一用户连发（Agent 多工具）少打几次远程 MySQL
- 不缓存金额本身，避免长时间透支
- 这是进程内缓存，多 worker 不共享；云上只有 1 个 worker，正好合适

这不是 Redis，也不是会话系统。挂了重启缓存就没了，可接受。

### 5.4 数据库连接池

`settings/database.py`：

```python
pool_size=MAX_POOL_SIZE          # 常驻连接
max_overflow=MAX_OVERFLOW        # 额外突发
pool_timeout=10                  # 等不到连接就失败，别无限排队
pool_pre_ping=True               # 用前探测断线
pool_use_lifo=True               # 优先复用刚还回的热连接
connect_args={"connect_timeout": 5}
```

LIFO：高并发时让少数连接反复工作，其余可以慢慢被回收，适合「很多请求、连接用得很短」的鉴权路径。

注意：**数字是每 worker**。4 worker × (8+8) = 64 条。云上 1 worker × (4+4) = 8 条，避免打爆远端 MySQL。

### 5.5 压缩线程池、关 debug

Headroom 压缩是 CPU 活，不能直接在 asyncio 协程里跑。`maybe_compress_request` 已经 `asyncio.to_thread(...)`。这里把默认 executor 换成有上限的 `ThreadPoolExecutor`：

- 云上 2 个压缩线程，避免和 2 核抢
- 本机 4 个

`FastAPI(debug=False)`：debug 会多做校验、保留更多 traceback，生产浪费 CPU/内存。

入口 `limit_concurrency`：uvicorn 层再加一道闸，请求堆积时直接拒绝，而不是把内存吃光。

## 6. 改了哪些文件

| 文件 | 作用 |
|---|---|
| `settings/capacity.py` | **新文件**。读机器规格，算出档位 |
| `settings/macro.py` | 连接池大小改从 CAPACITY 来 |
| `settings/database.py` | LIFO、connect_timeout |
| `gunicorn.conf.py` | UvicornWorker、worker 数、fork 后丢掉继承的 DB 连接 |
| `main.py` | httpx limits、信号量、压缩线程池、按档启动、关 debug |
| `utils/check_user.py` | 鉴权短缓存、精简查询、余额耗尽失效 |
| `utils/image_billing.py` | 图片扣费同样在余额 ≤0 时清缓存 |

## 7. 重启后怎么确认

1. 日志出现 `proxy started profile=...`
2. 云主机必须是 `profile=small workers=1`
3. 进程数：`ps aux | grep main.py`，云上应只有 1 个业务进程（不要 4 个 uvicorn worker）
4. 内存：LiteLLM + 代理加起来别把 1.8G 打穿；Swap 继续涨说明并发还是太大
5. 同一用户连发时，鉴权日志不应每条都有 `user key extracted`

云上经验值（`small`）：

- 同时流式约 **30 路**
- 同时在用约 **15–30 人**（Agent 占连接多就偏下限）

再往上不是这段 FastAPI 的问题，是 1.8G 内存、LiteLLM 单进程、上游 RPM。要稳到 50+ 人，先把云主机升到 4C/8G。

## 8. 学习时可以记住的几条

1. **LLM 代理看并发连接，不看 QPS。**
2. **流式必须 ASGI / async worker。** sync gunicorn 会把容量砍到 worker 个数。
3. **两层限流**：连接池保护自己，信号量保护上游。
4. **鉴权缓存只缓存「能不能过」，不缓存余额。** TTL 短，扣光立刻失效。
5. **数字按最弱的那台机器收缩。** 本机 32G 的配置不能原样上 1.8G 云主机。
6. **真正的上限在上游。** 代理优化只是别自己先死。

## 9. 回滚

云上备份目录：

```text
/mnt/data/claude_code_proxy/.bak_20260918_040636
```

把里面的文件拷回项目根即可回到改动前。`settings/capacity.py` 是新文件，回滚时可删。
