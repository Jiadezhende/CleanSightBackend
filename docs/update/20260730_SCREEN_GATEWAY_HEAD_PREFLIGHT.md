# 大屏接入止血：playlist 补 HEAD、清单与回放路径进 Gateway 宽松档

> **变更状态**：生效中（2026-07-30）
> **知识库**：待沉淀
>
> 承接 [20260729_SCREEN_TASK_LISTS.md](20260729_SCREEN_TASK_LISTS.md)（大屏清单端点）。Gateway 三档语义见 [app/utils/gateway.py](../../app/utils/gateway.py)。

## 概述

- **改了什么**：两条 `playlist.m3u8` 路由由 `@router.get` 改为 `@router.api_route(methods=["GET","HEAD"])`；`gateway_relaxed_prefixes` 默认值加入 `/task/live`、`/task/history`、`/traceback`。
- **为什么改**：大屏是**跨 origin 的独立前端**，接入后日志出现两类请求，单看是噪音，合起来会让大屏周期性把自己封禁一小时。
- **影响面**：对外响应契约不变（HEAD 是新增可用方法，GET 行为一字未动）；Gateway 对三个前缀的配额与计数策略放宽。无 schema 变更。

## 症状

```
"HEAD /traceback/task/1785394055951/playlist.m3u8?step_id=2&track=processed" 405
"OPTIONS /task/history" 200
```

## 成因一：FastAPI 不给 GET 路由补 HEAD

Starlette 原生 `Route` 在 `methods` 含 GET 时会自动加 HEAD，**FastAPI 的 `APIRoute` 不会**。实测对照：

```
/fa     (FastAPI @app.get)  -> methods = ['GET']          HEAD -> 405
/native (starlette Route)   -> methods = ['GET','HEAD']   HEAD -> 200
```

于是本仓库**任何路由被 HEAD 都是 405**。而原生 HLS 播放栈（Safari / AVPlayer / iOS WebView）取 playlist 前会自动发 HEAD 探可用性——这是浏览器媒体栈行为，前端 JS 拦不住，不能要求大屏「别发」。返 405 本身也不合 RFC 9110（通用服务器须支持 GET 与 HEAD）。

**放大器**：405 落在 `AntiScanStore._TRACKED_CODES = {404, 405}`，而 `/traceback` 当时既不在 relaxed 也不在 bypass。`scan_threshold=10 / scan_window=300` → 300 秒内 10 次即封 IP 3600 秒。`/task/history` 一次最多返回 10 个任务（`_HISTORY_LIMIT`），逐个探一遍正好撞线。

补 HEAD 后 body 由 h11 在传输层抑制，`Content-Length` 保留真值（真 uvicorn 实测：整个响应 139 字节纯头，`content-length: 108`）。**注意 `PlainTextResponse` 自身不做 HEAD 抑制**——只有 `FileResponse` 在 [responses.py](../../.venv/lib/python3.12/site-packages/starlette/responses.py) 里处理了；靠的是传输层，不是响应层。用 `TestClient` 验这件事会得到假阳性（httpx 自己会剥 body）。

handler 对 HEAD 照常执行（扫段 + 拼 playlist 后丢弃），换来正确的 200/404 语义——而这正是探测方要的答案。

## 成因二：跨 origin 预检双倍计数

`OPTIONS /task/history` 返 200 是 `CORSMiddleware` 处理的**真实 CORS 预检**（路由表里该路径只有 GET，走到路由会是 405）。预检是浏览器行为，后端改不掉。

无参数 GET 本属 simple request、不该预检，触发说明大屏在 GET 上挂了非安全清单的请求头（多半是 axios 全局塞的 `Content-Type: application/json`，GET 没 body，这头本就无意义）。但即便前端清干净，Gateway 仍把**预检与实际请求各计一次数**，而 `/task/history` 当时吃 60/60s 的普通配额（relaxed 里只有 `/task/message` 前缀，匹配不上）——大屏 3 秒轮一次即 40 次/分，叠加其他接口极易破线 429，连续 5 次同样封一小时。

## 为什么是 relaxed 而不是 bypass

Gateway 三档，优先级 `bypass > relaxed > normal`：

| 档位 | 限流 | 反扫描计数 | IP 白名单/封禁 |
|---|---|---|---|
| normal | 60/60s，超限升级封禁 | 计 | 查 |
| relaxed | 600/60s，不升级封禁 | 不计 | 查 |
| bypass | 完全跳过 | 不计 | 查 |

`/media` 独占 bypass：段请求路径内嵌 HMAC token，验不过即 403，**没有可枚举面**，限流作为防滥用手段是冗余的；而其频次（每 10s 一段 × 并发客户端 × 拖进度条回溯）会真打爆 600/60s。

`/traceback` 只给 relaxed：参数是明文可枚举的 `task_id`/`step_id`/`track`，给 bypass 等于放开任意频次的 task_id 扫描。它需要的只是「404 不计入封禁」——因为 **404 在这里是正常业务态**：只落了 raw 的 step 按默认 `track=processed` 查即 404（见 `/task/history` docstring 中「track 必须从 `steps[].tracks` 里挑」）。配额上限保留。

## 配置

默认值改在 `app/settings.py` 而非仅 `.env`——大屏自封是生产正确性问题，不该依赖部署时手工配置。

```
gateway_relaxed_prefixes = "/health,/task/message,/task/live,/task/history,/traceback,/admin-f3m8,/metrics"
```

`.env.example` 同步，并补上此前**完全缺失**的 `CLEANSIGHT_GATEWAY_BYPASS_PREFIXES` 文档（三档里最宽的一档没写，易让人把 `/media` 往 relaxed 里塞，那其实是降级）；同时订正原注释里「relaxed 默认覆盖 …/media」的错误说法——`/media` 从来在 bypass。

## 未覆盖

`/task/{task_id}/alarms` 仍在 60/60s 普通配额。未加入是因为 `/task` 整个前缀放宽会波及全部 task 路由；待确认大屏实际轮询清单后再定。

## 踩坑记录

`.env.dev` 里临时加 `CLEANSIGHT_GATEWAY_ENABLED=false` 调试会**静默挂掉 `tests/test_gateway.py` 的 7 个用例**（测试默认 `CLEANSIGHT_ENV=dev`，照样读 `.env.dev`）。且 `_load_env_files()` 是 `os.environ[k] = v` **无条件覆盖**，命令行传 `CLEANSIGHT_GATEWAY_ENABLED=true` 也盖不回去。排查时还要注意：`.env.dev` 是未跟踪文件，`git stash` 不动它，靠 stash 做「改动前后」对照会得到污染的结论。
