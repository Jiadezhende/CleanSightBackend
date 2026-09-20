# `/traceback` — 任务回放与时间轴

按**任务步骤**定位磁盘上的 HLS 段，返回两种东西：可直接喂播放器的 VOD playlist（`playlist.m3u8`），或进度条打点的时间轴（`timeline`）。数据源是**磁盘落盘的 HLS 段**（`{base_dir}/{task_id}/{step_id}/`）；告警元数据来自 `clean_alarm` 表（DB）。所有媒体 URL 都是本组端点当场签发的 token 化 `/media/*` 绝对地址（消费见 [media.md](media.md)）。通用约定（Base URL、Gateway、错误模型、时间戳单位）见 [README](README.md)。

一次请求 = **一个 `(task_id, step_id)`**，不做跨 step 聚合——一个 task 的完整录像分散在各 step 目录里。

```
  task_id + step_id ──→ task/playlist.m3u8（回放） / timeline（打点）
```

> **按 `alarm_id` 反查证据段的两个端点（`/alarm/{id}/evidence`、`/alarm/{id}/playlist.m3u8`）已于 2026-09-08 下线**，不再存在（请求得 404）。同一需求走 lab 页面：task 级 VOD 回放 + `timeline` 的告警打点 + 帧级 seek，粒度比原来的「触发段 ± N 段」更细。详见 [20260908_ALARM_EVIDENCE_RETIRE.md](../update/20260908_ALARM_EVIDENCE_RETIRE.md)。

几处贯穿全组的约定，下面各端点不再重复：

- **`track`**：`raw`（原始画面）| `processed`（带检测框）。默认 `processed`。非 `raw`/`processed` → **422**（FastAPI Query 校验，`pattern` 拦截）。
- **`step_id`**（playlist / timeline 必填）：洗消步骤 id，仅返回该 step 的数据。缺失 → **422**。
- **段 URL 的 host 取自当前请求**（`request.base_url`）：走 Nginx 等反代时若不透传 `X-Forwarded-Proto` / `X-Forwarded-Host`，签出来的就是内网地址——m3u8 能拉到但所有段请求全失败。属部署配置问题。
- **段 URL 的 token 会过期**（默认 TTL 见 [media.md](media.md)）：播放时长超过 TTL 时后段 token 在播放途中失效 → 段请求 **403**。正确处理是**重拉一次 playlist**换新 token，别在前端续签或缓存旧 token。

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
| `404` | 该 `(task_id, step_id, track)` 的**清单里一个段都没有**。三种成因**不分档**：挑错 track、step 不存在、首段仍在转码 | `{"error":"Resource not found","detail":"...","resource_type":"Segments","resource_id":"task=..,step=..,track=.."}` |
| `503` | 缺 `{track}_init.mp4`（旧格式产物，或首段仍在 transcode；服务端无法自愈，**无迁移路径**） | `{"detail":{"error":"HLS init segment missing","detail":"..."}}` |

> **404 曾分两档，2026-09-20 起只有一档**（段查询收口到清单后，域里已无第二个入口去区分
> 「盘上没文件」和「有文件没登记」）。body 形态保持结构化不变——**不要**改成裸
> `HTTPException`，那会让按 `resource_type` 分支的客户端静默失效。
>
> **503 的形态与众不同**：它是裸 `HTTPException` 且 `detail` 是**嵌套对象**
> （`{"detail":{"error":...,"detail":...}}`），**不带** README 错误模型的顶层
> `error`/`retryable`——与其它端点的 DB 503（带 `retryable:true`）不一致。
>
> **两档检查的先后不能反**：段检查在 init 检查之前。反过来的话，一个根本不存在的
> task/step 会先撞上「缺 init」而得到 503，那是"服务端暂时不可用、请重试"的语义。

### 前端坑点

- **段一路 200 突然全 403**：token 过期或服务重启换了 secret，重拉本 playlist 换新 token（详见 [media.md](media.md)），不是鉴权配错。
- **反代下 m3u8 拉到但段全失败**：host 取自请求，反代未透传 `X-Forwarded-*`，播放器在请求内网地址。
- **`track` 取值范围**：只有 `raw` / `processed` 两值；一个 step 未必两轨都落盘，硬写默认的 `processed` 而该 step 只有 raw 会 404。可播轨道从 [`GET /task/history`](task.md) 的 `steps[].tracks` 里取（该清单只覆盖最近 10 个已完成任务）；不在清单里的任务仍需按 404 兜底或两轨都试。

---

## GET /traceback/task/{task_id}/timeline

**用途**：给某 step 的回放拿「起止时间 + 时长 + 告警事件点」，前端在视频进度条上叠加告警标记。段时长来自**磁盘**（读 playlist 的 EXTINF），告警事件来自 **DB**——两个数据源独立，DB 挂了只丢事件、不丢时长。

**路径参数**：`task_id`（int）。
**查询参数**：`step_id`（int，**必填**；缺失 → 422）、`track`（`raw` | `processed`，默认 `raw`）。

### 两套坐标，各回答不同的问题

| 口径 | 字段 | 回答 |
|------|------|------|
| **墙钟** | `start_ms` / `end_ms` / `duration_ms` / `events[].ts_ms` | 「这件事几点发生的」——审计、检索、跨 step 对照 |
| **媒体** | `media_duration_ms` / `gap_total_ms` / `events[].media_offset_ms` | 「在播放器的第几秒」——进度条、标记落点、跳转 |

**进度条必须用媒体坐标。** `<video>.currentTime` 与 `duration` 都是媒体量，而告警的 `ts_ms` 是墙钟；两者混用就是「比例不同尺」——一个断流 20s 的 step 里，指针按墙钟全长走到底也只到 96.7%，告警标记还与画面差整整一个断流时长。媒体轴是**压紧的**墙钟（段间空隙在它上面不存在），换算要清单，只有后端做得了，所以 `media_offset_ms` 由本端点给。

**`media_*` 随 `track` 变**：两轨各自独立切段，Σ EXTINF 不同尺。**前端切轨必须重取**，不能只换视频源。墙钟那几个字段与 `track` 无关（恒取双轨并集）。

### 响应 `200`

```jsonc
{
  "task_id": 123,
  "step_id": 10,
  "start_ms": 1751800000000,     // 该 step 最早段起点，epoch 毫秒；无段时为 0
  "end_ms": 1751800060000,       // 最后一段起点 + 其 EXTINF 时长，epoch 毫秒；无段时为 0
  "duration_ms": 60000,          // end_ms - start_ms（含断流空洞）；无段时为 0
  "track": "raw",                // 媒体坐标按哪条轨算（回显入参）
  "media_duration_ms": 40000,    // 该轨 Σ EXTINF，= <video>.duration；无段时为 0
  "gap_total_ms": 0,             // 该轨累计断流时长（逐段精确判据，不是两数相减）
  "has_gap": false,              // gap_total_ms > 0
  "events": [
    {
      "ts_ms": 1751800015000,    // 告警时间，epoch 毫秒（已归一化）
      "media_offset_ms": 15000,  // 同一时刻在该轨媒体轴上的位置，进度条用它
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
| `media_duration_ms` | int | 该 `track` 的 Σ EXTINF，**与 `<video>.duration` 同源**。无段时 `0` |
| `gap_total_ms` / `has_gap` | int / bool | 该轨累计断流时长：所有满足 `下一段起点 − (本段起点 + EXTINF) > 0.5s` 的空隙之和。无段时 `0` / `false` |
| `events[].ts_ms` | int | epoch **毫秒**（`detected_at` 为 null 的告警被跳过，不进 events） |
| `events[].media_offset_ms` | int | 该告警在**媒体轴**上的位置（ms），随 `track` 变。落进断流空洞的墙钟吸附到下一段段首——那段时间在媒体轴上宽度为零，没有对应刻度 |
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
- **无段时全 0**：目录建了但没写成段（起流即失败）→ `start_ms`/`end_ms`/`duration_ms`/`media_duration_ms` 都是 0，前端需兜底避免除零 / 空进度条。
- **不要用 `duration_ms − media_duration_ms` 推断断流总量**，那会误报：前者是**双轨并集**的墙钟跨度、后者是**单轨** Σ EXTINF，两者不同尺，差值里混着「两轨起止不对齐」这一项——推理起步晚于取流时 processed 首段必然晚于 raw 首段，于是零断流的 step 也算得出假空洞。要这个数就读 `gap_total_ms`。
- **空洞在进度条上画不出来**（媒体轴是压紧的，宽度为零），`gap_total_ms` 只够做一句文字提示。要画成不可选中的禁区需要逐段 `#EXT-X-PROGRAM-DATE-TIME`，本期未做。
- **切轨要重取 timeline**：`media_duration_ms` 与 `media_offset_ms` 按轨算，不重取就会拿 raw 的坐标去画 processed 的进度条。

---

## 附：静默失败的几种情况

以下情况后端**不报错**，只是没有数据 / 降级，排查时容易误判为 bug：

| 现象 | 后端实际状态 |
|------|------------|
| playlist 返回 404 且 body 只有 `detail` | 段全是在途段（mp4v 已落、转码未完成），EXTINF 过滤后为空；任务进行中重拉会逐渐有段 |
| playlist 拉到了但所有段请求失败 | 段 URL 是绝对地址，反代未透传 `X-Forwarded-*`，播放器在请求内网地址 |
| 回放拖到后半段才挂（段突然 403） | 段 token 过期（超 TTL）或服务重启换 secret，需重拉 playlist 换新 token |
| `/timeline` 的 `events` 一直为空 | ①该 step 确实没告警；②DB 不可用已降级（时长仍在，事件为空）。二者从本端点响应无法区分，用 `/task/{id}/alarms`（DB 挂会 503）交叉判断 |
| `/timeline` 的 `start_ms`/`end_ms`/`duration_ms` 全 0 | 该 `(task_id, step_id)` 磁盘上无段（目录建了但未写成段 / 起流即失败） |

> 消费本组接口的参考实现：lab 前端 [app/static/lab/index.html](../../app/static/lab/index.html)（`attachVideo` / `switchTrack`，hls.js + token 过期重拉 playlist）。
