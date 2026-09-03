# `/traceback` — 告警证据与回放

按**告警**或**任务步骤**定位磁盘上的 HLS 段，返回三种东西：证据 clip 列表（`evidence`）、可直接喂播放器的 VOD playlist（`playlist.m3u8`），或进度条打点的时间轴（`timeline`）。数据源是**磁盘落盘的 HLS 段**（`{base_dir}/{task_id}/{step_id}/`）；告警元数据来自 `clean_alarm` 表（DB）。所有媒体 URL 都是本组端点当场签发的 token 化 `/media/*` 绝对地址（消费见 [media.md](media.md)）。通用约定（Base URL、Gateway、错误模型、时间戳单位）见 [README](README.md)。

一次请求 = **一个 `(task_id, step_id)`**，不做跨 step 聚合——一个 task 的完整录像分散在各 step 目录里。

```
  告警侧：alarm_id ──→ evidence / alarm/playlist.m3u8 （从 alarm 自带的 (task_id, step_id) 定位）
  任务侧：task_id + step_id ──→ task/playlist.m3u8 / timeline
```

几处贯穿全组的约定，下面各端点不再重复：

- **`track`**：`raw`（原始画面）| `processed`（带检测框）。默认 `processed`。非 `raw`/`processed` → **422**（FastAPI Query 校验，`pattern` 拦截）。
- **`n_before` / `n_after`**：触发段前 / 后要带的上下文段数。int，范围 `-1..20`，默认 `-1`。`-1` = 用配置默认（`settings.traceback_context_before` / `_after`），**不是 0**。超范围 → **422**。
- **`step_id`**（playlist / timeline 必填）：洗消步骤 id，仅返回该 step 的数据。缺失 → **422**。
- **段 URL 的 host 取自当前请求**（`request.base_url`）：走 Nginx 等反代时若不透传 `X-Forwarded-Proto` / `X-Forwarded-Host`，签出来的就是内网地址——m3u8 能拉到但所有段请求全失败。属部署配置问题。
- **段 URL 的 token 会过期**（默认 TTL 见 [media.md](media.md)）：播放时长超过 TTL 时后段 token 在播放途中失效 → 段请求 **403**。正确处理是**重拉一次 playlist**换新 token，别在前端续签或缓存旧 token。

---

## GET /traceback/alarm/{alarm_id}/evidence

**用途**：给单条告警取「触发段 ± 上下文」的双轨 clip 列表，前端拿到 `url` 列表逐个播放或下载。直接用 alarm 表自带的 `(task_id, step_id)` 定位文件（不查 `clean_task.source_ip`——该字段会被业务侧覆写、不可靠）。段来自磁盘，告警元数据来自 DB。

**路径参数**：`alarm_id`（int）。
**查询参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `n_before` | int | 否 | -1 | 触发段前上下文段数，`-1..20`；`-1` = 配置默认 |
| `n_after` | int | 否 | -1 | 触发段后上下文段数，`-1..20`；`-1` = 配置默认 |

（此端点**无 `track` 参数**——raw 与 processed 两轨一次全返回。）

### 响应 `200`

```jsonc
{
  "alarm": {
    "alarm_id": 1001,
    "task_id": 123,
    "step_id": 10,
    "step_name": "泄漏检测",       // 可为 null（DB 列可空）
    "alarm_type": "流程违规",       // 可为 null；全称，非 /message 的短名 metric
    "severity": "high",            // 可为 null；low | medium | high | critical
    "message": "...",              // 可为 null
    "detected_at": 1751800000000,  // epoch 毫秒（DB 存秒/微秒时后端已归一化）；理论可为 null
    "resolved": false,             // DB 该列为 null 时归一化为 false，永不返回 null
    "resolved_by": null,           // 未处理时 null
    "resolved_at": null            // epoch 毫秒；未处理时 null
  },
  "task_id": 123,                  // = alarm.task_id，顶层冗余出一份方便直用
  "step_id": 10,                   // = alarm.step_id
  "raw_clips": [
    {
      "url": "http://<host>:8000/media/segment/<token>",
      "filename": "raw_segment_...mp4",
      "ts_us": 1751800000000000,   // 段起点，epoch 微秒
      "ts_ms": 1751800000000,      // 段起点，epoch 毫秒
      "is_trigger": true           // 是否为命中告警的那一段（其余为上下文）
    }
  ],
  "processed_clips": [ /* 同结构 */ ]
}
```

| 字段 | 类型 | 说明（含 null / 空条件） |
|------|------|------------------------|
| `alarm` | object | 告警对象。字段名用**全称**（`alarm_type`/`severity`），与 `/task/{id}/alarms` 一致，与 `/task/message` 的短名（`metric`/`level`）**不是同一套** |
| `alarm.step_name` / `alarm.alarm_type` / `alarm.severity` / `alarm.message` | string \| null | 对应 DB 列可空时为 null |
| `alarm.detected_at` | int \| null | epoch **毫秒**（后端把秒/微秒统一归一到毫秒） |
| `alarm.resolved` | bool | DB null → 归一化 `false`，**不返回 null** |
| `alarm.resolved_by` / `alarm.resolved_at` | int \| null | 未处理时 null |
| `task_id` / `step_id` | int | 顶层回显（= alarm 里同名字段），省得前端再从 `alarm` 里挖 |
| `raw_clips` / `processed_clips` | array | 该轨的段列表，按时序。**段已被清理 / 还没落盘时为 `[]`**（不是 404，见下） |
| `[].url` | string | token 化绝对地址，直接可播；host 取自请求 |
| `[].filename` | string | 段文件名 |
| `[].ts_us` | int | 段起点，epoch **微秒** |
| `[].ts_ms` | int | 段起点，epoch **毫秒**（= `ts_us / 1000`） |
| `[].is_trigger` | bool | 命中告警的那一段为 `true`，前后上下文为 `false` |

**段全被清理 vs 告警不存在——两码事**：告警存在但两轨段都不在了（已清理 / 还未落盘），返回 **200** 且 `raw_clips` / `processed_clips` **都是空数组**（后端只记一条 warning 日志）；只有告警本身查不到、或该告警 `step_id` 为空无法定位，才是 **404**。前端不能凭空数组判 404。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `404` | `alarm_id` 不存在；或告警存在但 `step_id` 为 null（无法定位段） | `{"error":"Resource not found","detail":"...","resource_type":"Alarm","resource_id":"..."}` |
| `422` | `n_before` / `n_after` 超 `-1..20` | `{"detail":[...]}`（FastAPI 校验格式） |
| `503` | 拉告警时 DB 不可用 | `{"error":"Database unavailable","detail":"...","retryable":true}` |
| `400` | `alarm.detected_at` 为 null 或 ≤ 0（脏数据兜底，正常不出现） | `{"error":"...","detail":"...","field":"detected_at",...}` |

### 前端坑点

- **空数组 ≠ 404**：段被清理返 200 空数组，只有告警/step_id 缺失才 404。判分支只认 status code。
- **两轨可能一空一有**：某 step 只落了 raw，`processed_clips` 就是 `[]`——按轨各自兜底，别假设两轨对称。
- **`is_trigger` 用来高亮**：列表里恰有（通常）一段是 `true`，其余是上下文；不要靠数组下标猜触发段。
- **要直接喂 HLS 播放器用 playlist 端点**：`evidence` 给的是**裸段 URL 列表**，fMP4 裸段浏览器解不了（缺 init）。要播放走下面的 `/alarm/{id}/playlist.m3u8`。

---

## GET /traceback/task/{task_id}/playlist.m3u8

**用途**：某 `(task_id, step_id, track)` 的**完整回放**，返回动态生成的 HLS VOD playlist，直接喂 hls.js / 原生 MSE 播放。相比 serve 落盘的 LIVE playlist：保证 VOD 完整性（即使任务未封档）、URL 走 token 化 `/media/*` 不暴露文件系统路径。

**方法**：`GET` / `HEAD`（同一 handler）。原生 HLS 播放栈（Safari / AVPlayer 等）取 playlist 前会自动发 HEAD 探可用性，是浏览器媒体栈行为、前端 JS 拦不住，故 HEAD 与 GET 同注册。HEAD 的 body 由传输层抑制，`Content-Length` 仍是真值，状态码与 GET 完全一致（含下方各种 404/503）。

**路径参数**：`task_id`（int）。
**查询参数**：

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `step_id` | int | **是** | — | 洗消步骤 id，仅回放该 step；缺失 → 422 |
| `track` | string | 否 | processed | `raw` \| `processed`；其它值 → 422 |

### 响应 `200`

`Content-Type: application/vnd.apple.mpegurl`、`Cache-Control: no-store`，体是纯文本 m3u8：

```m3u8
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-TARGETDURATION:76
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="http://<host>:8000/media/init/<init_token>"
#EXTINF:70.666,
http://<host>:8000/media/segment/<token>
#EXTINF:75.644,
http://<host>:8000/media/segment/<token>
#EXT-X-ENDLIST
```

动态生成（不是 serve 落盘文件），四个性质：

- **VOD**（带 `#EXT-X-ENDLIST`），即使任务未封档也当完整点播处理。
- **fMP4**，`#EXT-X-MAP` 的 init 段不可缺——**裸的 `/media/segment/{token}` 单独播不了**，浏览器解不了无 init 的 fragment。init 段**按轨各一份**（`raw_init.mp4` / `processed_init.mp4`），同轨内所有段共享；两轨是独立 playlist，不可互指。
- 段 / init URL 都是 **token 化绝对地址**，host 取自请求。
- **只收「写入侧 playlist 里已有 `#EXTINF`」的段**，在途段（mp4v 已落盘但转码+append 未完成）被过滤——放进去只能填估算时长，会与 fMP4 内部 tfdt 时间戳对不上产生 hls.js 缓冲洞。EXTINF 时长直接回读写入侧 playlist，不重新推导。

**任务进行中拉到的 playlist 短于实际已录时长**：在途段被过滤是刻意的；进行中重复拉取会逐渐变长。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `422` | 缺 `step_id`，或 `track` 非 `raw`/`processed` | `{"detail":[...]}`（FastAPI 校验格式） |
| `404`（A） | 该 `(task_id, step_id, track)` 目录下**一个段都没有** | `{"error":"Resource not found","detail":"...","resource_type":"Segments","resource_id":"..."}` |
| `404`（B） | 有段但**全是在途段**（经 playlist EXTINF 过滤后为空） | `{"detail":"No playable segments yet"}` |
| `503` | 缺 `{track}_init.mp4`（旧格式产物，或首段仍在 transcode；服务端无法自愈，**无迁移路径**） | `{"detail":{"error":"HLS init segment missing","detail":"..."}}` |

> **两处不一致，务必只认 status code**：
> ① 两种 404 的 body 形态不同——A 走异常模型（带 `error`/`resource_type`/`resource_id`），B 是裸 `HTTPException`（**只有 `detail`**）。别靠 body 字段区分「无段」和「全在途」。
> ② 这里的 503 也是裸 `HTTPException` 且 `detail` 是**嵌套对象**（`{"detail":{"error":...,"detail":...}}`），**不带** README 错误模型的顶层 `error`/`retryable`——与 `/evidence` 的 DB 503（带 `retryable:true`）形态也不一致。

### 前端坑点

- **段一路 200 突然全 403**：token 过期或服务重启换了 secret，重拉本 playlist 换新 token（详见 [media.md](media.md)），不是鉴权配错。
- **反代下 m3u8 拉到但段全失败**：host 取自请求，反代未透传 `X-Forwarded-*`，播放器在请求内网地址。
- **`track` 取值范围**：只有 `raw` / `processed` 两值；一个 step 未必两轨都落盘，硬写默认的 `processed` 而该 step 只有 raw 会 404（A）。可播轨道从 [`GET /task/history`](task.md) 的 `steps[].tracks` 里取（该清单只覆盖最近 10 个已完成任务）；不在清单里的任务仍需按 404 兜底或两轨都试。

---

## GET /traceback/alarm/{alarm_id}/playlist.m3u8

**用途**：给单条告警的证据回放生成 VOD playlist（触发段 + 前后上下文），供 admin / lab 端直接播放。fMP4 段必须经 m3u8 + init 拼装浏览器才能解码，故告警证据播放走这里而非裸 `/media/segment/{token}`。定位方式同 `/evidence`（用 alarm 自带 `(task_id, step_id)`）。

**方法**：`GET` / `HEAD`（同上，理由与行为一致）。

**路径参数**：`alarm_id`（int）。
**查询参数**：`track`（默认 processed）、`n_before`、`n_after`（`-1..20`，默认 -1）——语义同本页顶部约定。

### 响应 `200`

同 `/task/{id}/playlist.m3u8` 的 m3u8 格式与响应头，区别仅在段范围是「触发段 ± 上下文」而非整个 step。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `404`（A） | `alarm_id` 不存在；告警 `step_id` 为 null；或该轨在告警附近**无段** | `{"error":"Resource not found","detail":"...","resource_type":"Alarm"或"Segments",...}` |
| `404`（B） | 找到了段但**全在途**（EXTINF 过滤后为空） | `{"detail":"No playable segments yet"}` |
| `422` | `track` 非法，或 `n_before`/`n_after` 超范围 | `{"detail":[...]}` |
| `503`（DB） | 拉告警时 DB 不可用 | `{"error":"Database unavailable","detail":"...","retryable":true}` |
| `503`（init） | 缺 `{track}_init.mp4` | `{"detail":{"error":"HLS init segment missing",...}}` |

> 同上：两种 404 body 不一致；两种 503（DB vs init）body 也不一致。**判分支只认 status code。**

### 前端坑点

- 与 task playlist 相同的 token 过期 / 反代 host 两条坑，处理方式一致（重拉 playlist / 透传 `X-Forwarded-*`）。
- 与 `/evidence` 的区别：`/evidence` 给裸段 URL 列表（能拿到 `ts_ms` / `is_trigger` 等元数据，但不能直接喂播放器），本端点给可播的 m3u8。要做「进度条打点 + 直接播放」通常两个都要。

---

## GET /traceback/task/{task_id}/timeline

**用途**：给某 step 的回放拿「起止时间 + 时长 + 告警事件点」，前端在视频进度条上叠加告警标记。段时长来自**磁盘**（扫 `{task_id}/{step_id}/` 的段并回读 playlist EXTINF），告警事件来自 **DB**——两个数据源独立，DB 挂了只丢事件、不丢时长。

**路径参数**：`task_id`（int）。
**查询参数**：`step_id`（int，**必填**；缺失 → 422）。

### 响应 `200`

```jsonc
{
  "task_id": 123,
  "step_id": 10,
  "start_ms": 1751800000000,     // 该 step 最早段起点，epoch 毫秒；无段时为 0
  "end_ms": 1751800060000,       // 最后一段起点 + 其 EXTINF 时长，epoch 毫秒；无段时为 0
  "duration_ms": 60000,          // end_ms - start_ms；无段时为 0
  "events": [
    {
      "ts_ms": 1751800015000,    // 告警时间，epoch 毫秒（已归一化）
      "type": "alarm",           // 目前恒为 "alarm"
      "alarm_id": 1001,
      "alarm_type": "流程违规",    // 可为 null
      "severity": "high",        // 可为 null；low | medium | high | critical
      "step_id": 10,             // 可为 null
      "step_name": "泄漏检测",     // 可为 null
      "message": "..."           // 可为 null
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null / 空条件） |
|------|------|------------------------|
| `start_ms` / `end_ms` / `duration_ms` | int | epoch **毫秒**。该 step 磁盘上无段时**三者均为 `0`**。取 raw / processed **双轨并集**的最早起点、最晚终点 |
| `end_ms` | int | = `max(段起点 + 该段 EXTINF)`，**不是** `max(段起点)`——已含最后一段自身时长，与 `<video>.duration` 对齐 |
| `events` | array | 该 step 的告警事件，按 `ts_ms` 升序。**无告警或 DB 不可用时为 `[]`** |
| `events[].ts_ms` | int | epoch **毫秒**（`detected_at` 为 null 的告警被跳过，不进 events） |
| `events[].type` | string | 目前恒 `"alarm"` |
| `events[].alarm_type` / `severity` / `step_id` / `step_name` / `message` | 各类 \| null | 对应 DB 列可空时为 null |

**时长细节**：`end_ms` 取「最后一段起点 + EXTINF」而非最后一段起点，故进度条右端与 `<video>.duration` 一致。在途段（playlist 无 EXTINF）在双轨里都被跳过，与 playlist 过滤策略一致。

### 降级（重点）

**DB 不可用时不 503，返回 200**，`start_ms`/`end_ms`/`duration_ms` 照常（来自磁盘），只有 `events` 退化为 `[]`。DB 恢复后自动重新带回事件（自愈，无需切换任何开关）。

**前端如何识别是「降级」还是「本就无告警」**：本端点**无法从响应体区分**——两种情况都是 `events: []` + 200。若需确证，用 `/task/{task_id}/alarms`（见 [task.md](task.md)）交叉验证：那个端点 DB 挂时会返 **503**，据此判断当前是 DB 故障还是确实没有告警。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `422` | 缺 `step_id` | `{"detail":[...]}`（FastAPI 校验格式） |

> 本端点**尽力而为、不因 DB 抛异常**：DB 故障走降级（200 空 events），不会 503。除 `step_id` 缺失的 422 外无其它错误码。

### 前端坑点

- **段 `duration_ms` 与 playlist 的可播时长可能有差**：timeline 取双轨并集，playlist 是单轨；两轨段边界不一定对齐，别拿 timeline 的 `duration_ms` 当某单轨的播放长度。
- **`events` 空要交叉判断**：见上「降级」——空 `events` 不代表无告警，可能是 DB 挂了。
- **无段时全 0**：目录建了但没写成段（起流即失败）→ `start_ms`/`end_ms`/`duration_ms` 都是 0，前端需兜底避免除零 / 空进度条。

---

## 附：静默失败的几种情况

以下情况后端**不报错**，只是没有数据 / 降级，排查时容易误判为 bug：

| 现象 | 后端实际状态 |
|------|------------|
| `/evidence` 返回 200 但 `raw_clips` / `processed_clips` 都是 `[]` | 告警存在但视频段已清理 / 还没落盘。**不是 404**——只有告警本身或其 `step_id` 缺失才 404 |
| `/evidence` 一轨有 clip 一轨空 | 该 step 只落了其中一轨（如仅 raw），另一轨天然为 `[]` |
| playlist 返回 404 且 body 只有 `detail` | 段全是在途段（mp4v 已落、转码未完成），EXTINF 过滤后为空；任务进行中重拉会逐渐有段 |
| playlist 拉到了但所有段请求失败 | 段 URL 是绝对地址，反代未透传 `X-Forwarded-*`，播放器在请求内网地址 |
| 回放拖到后半段才挂（段突然 403） | 段 token 过期（超 TTL）或服务重启换 secret，需重拉 playlist 换新 token |
| `/timeline` 的 `events` 一直为空 | ①该 step 确实没告警；②DB 不可用已降级（时长仍在，事件为空）。二者从本端点响应无法区分，用 `/task/{id}/alarms`（DB 挂会 503）交叉判断 |
| `/timeline` 的 `start_ms`/`end_ms`/`duration_ms` 全 0 | 该 `(task_id, step_id)` 磁盘上无段（目录建了但未写成段 / 起流即失败） |

> 消费本组接口的参考实现：lab 前端 [app/static/lab/index.html](../../app/static/lab/index.html)（`attachVideo` / `switchTrack`，hls.js + token 过期重拉 playlist）。
