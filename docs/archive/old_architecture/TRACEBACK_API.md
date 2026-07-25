<!-- markdownlint-configure-file {"MD024": {"siblings_only": true}} -->

# 视频追溯 API 文档

> **最后更新**: 2026-07-01
>
> 本文描述的是**基于 (task_id, step_id) 文件系统**的追溯实现（2026-05 重构）。
>
> 与上一版差异：落盘路径由 `{base_dir}/{client_id}/{task_id}/` 改为 `{base_dir}/{task_id}/{step_id}/`，
> 完全解耦 `source_ip`。原因：洗消任务跨 step 执行时业务侧会覆写 `clean_task.source_ip`，
> 原架构通过 source_ip 反查 client_id 永远只能定位一半数据。`/traceback/*` 不再读 source_ip。
>
> **2026-05-12 fMP4 改造（破坏性）**：MP4 段落盘格式从「普通 MP4（moov+mdat）」改为「HLS fMP4 fragment（styp+moof+mdat）」+ 每 step 共享一份 `init.mp4`。
>
> - 前端影响：playlist 头部升级到 `#EXT-X-VERSION:7` 并包含 `#EXT-X-MAP:URI="..."` 一行（指向新增的 `/media/init/{token}`）。`hls.js` 1.0+ 自动处理，无需改前端代码。
> - 历史段必须经迁移脚本升级才能播放，未迁移的 step 调 playlist 端点会拿到 **503**（详见末尾「历史数据迁移」）。
> - **告警证据回放**：`evidence` JSON 中的 `*_clips[].url` 仍是单段 fragment URL（无 init），原生 `<video>` 无法直接播放。新增 [`/traceback/alarm/{id}/playlist.m3u8`](#2-告警证据回放-playlist按-alarm_id-生成-vod-m3u8) 端点专供证据回放使用，admin/lab 均通过 `hls.js` 加载它。

---

## 设计概览

### 核心思路

HLS 写入时已按规则命名落盘（step 维度隔离）：

```text
{base_dir}/{task_id}/{step_id}/{raw|processed}_segment_{ts_us}.mp4
{base_dir}/{task_id}/{step_id}/{raw|processed}_playlist.m3u8   # LIVE 原始 playlist
```

文件名中的 `ts_us`（微秒时间戳）就是该段的开始时间。文件系统天然按 `(task_id, step_id)` 隔离，**无需任何数据库索引**：

- 告警证据：直接用 `clean_alarm.(task_id, step_id)` 进入对应目录
- 时间点 → 段：按文件名 ts_us 升序二分查找

> `client_id`（运行时 = source_ip）仅作为 `ClientManager` 区分并发推理流的内存 key 保留，
> **不再出现在追溯链路**。任何依赖 task_id → source_ip 反查的旧逻辑都已下线。

### 两层路由

```text
前端
  │
  ├── GET /traceback/*          ← 业务逻辑层（鉴权待扩展）
  │       返回带 token 的媒体 URL
  │
  └── GET /media/{kind}/{token} ← 媒体访问层（HMAC token 鉴权）
          kind ∈ {segment, init}
          流式返回 fMP4 / MP4
```

前后端物理隔离：所有媒体文件经 HTTP 路由返回，不依赖共享文件系统，不暴露文件路径。

### Token 机制

```text
token = base64url(payload_json) + "." + base64url(HMAC-SHA256(secret, payload_json))

payload = {
  "t": task_id,                          # 任务 id
  "s": step_id,                          # 洗消步骤 id（替代了旧字段 "c" client_id）
  "f": filename,                         # 文件名（不含路径）
  "k": "segment" | "init",               # 资源类型
  "e": expiry_epoch                      # 过期时间（Unix 秒）
}
```

- Secret 来自 `CLEANSIGHT_MEDIA_TOKEN_SECRET` 环境变量；未配置时生成随机临时 secret（重启失效）
- 默认 TTL：300s（可通过 `CLEANSIGHT_MEDIA_TOKEN_TTL` 调整）
- URL 中不含原始文件路径，防止越权枚举
- payload 校验同时绑定 `kind`，两种 kind 之间不可互换（如 segment token 不能当 init token 使用）
- `init` kind 是 2026-05-12 fMP4 改造新增，专用于 `init.mp4`（HLS fMP4 codec init segment，每 step 共享一份）

---

## 接口列表

### 1. 告警证据回溯（按 alarm_id 查相关视频段）

```http
GET /traceback/alarm/{alarm_id}/evidence?n_before=1&n_after=2
```

用途：误报验证——拉一条告警的原始 + 处理后双轨视频片段与推理结果，用于人工复核。

#### 路径参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| alarm_id | integer | 告警 ID（来自 `clean_alarm.alarm_id`） |

#### 查询参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| n_before | integer | -1 | 触发段之前附带的上下文段数（0-20）；`-1` 取配置默认（`CLEANSIGHT_TRACEBACK_CONTEXT_BEFORE`） |
| n_after | integer | -1 | 触发段之后附带的上下文段数（0-20）；`-1` 取配置默认（`CLEANSIGHT_TRACEBACK_CONTEXT_AFTER`） |

#### 定位逻辑

`(task_id, step_id)` 直接取自 `clean_alarm` 行，**不查 `clean_task`**：

```text
1. SELECT task_id, step_id, detected_at FROM clean_alarm WHERE alarm_id = ?
2. step_id IS NULL → 404（无法定位）
3. 归一化 detected_at → 毫秒（秒/毫秒/微秒自适应）
4. 列出 {base_dir}/{task_id}/{step_id}/{raw|processed}_segment_*.mp4
5. 按 ts_us 升序，bisect_right(ts_list, detected_ms*1000) - 1 → 触发段下标
6. 向前取 n_before 段，向后取 n_after 段
```

#### 成功响应（200 OK）

```json
{
  "alarm": {
    "alarm_id": 42,
    "task_id": 100,
    "step_id": 2,
    "step_name": "酶洗",
    "alarm_type": "bubble_detected",
    "severity": "high",
    "message": "检测到气泡,可能存在漏气",
    "detected_at": 1700000022000,
    "resolved": false,
    "resolved_by": null,
    "resolved_at": null
  },
  "task_id": 100,
  "step_id": 2,
  "raw_clips": [
    {
      "url": "http://host/media/segment/<token>",
      "filename": "raw_segment_1700000010000000.mp4",
      "ts_us": 1700000010000000,
      "ts_ms": 1700000010000,
      "is_trigger": false
    },
    {
      "url": "http://host/media/segment/<token>",
      "filename": "raw_segment_1700000020000000.mp4",
      "ts_us": 1700000020000000,
      "ts_ms": 1700000020000,
      "is_trigger": true
    }
  ],
  "processed_clips": []
}
```

#### 字段说明

| 字段 | 说明 |
| --- | --- |
| `task_id` / `step_id` | 顶层冗余出来,方便前端拼后续 `/traceback/task/.../playlist.m3u8?step_id=...` |
| `raw_clips` / `processed_clips` | 双轨片段列表,`is_trigger=true` 标记实际触发告警的段 |

> 上一版响应中的 `client_id` 字段已**移除**。keypoints 回溯能力（旧 `keypoints_url` / `detection` 字段）已废弃，响应中不再返回。

#### 错误响应

| 状态码 | 场景 |
| --- | --- |
| 404 | 告警不存在 / 告警 `step_id IS NULL`（无法定位） |
| 422 | `n_before`/`n_after` 不在 [-1, 20] 范围内 |
| 503 | 数据库不可用 |

---

### 2. 告警证据回放 Playlist（按 alarm_id 生成 VOD m3u8）

```http
GET /traceback/alarm/{alarm_id}/playlist.m3u8?track=processed&n_before=1&n_after=2
```

用途：单条告警证据弹窗的视频回放。返回 trigger 段 + 前后上下文段的 VOD m3u8（含 `#EXT-X-MAP`），可直接被 `hls.js` 消费。

`evidence` 接口返回的 `*_clips[].url` 是裸 fragment（无 `ftyp+moov` codec init），浏览器原生 `<video>` 无法播放；admin/lab 端的告警证据视频均通过该端点拼接 init + 上下文段。

#### 路径参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| alarm_id | integer | 告警 ID（来自 `clean_alarm.alarm_id`） |

#### 查询参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| track | string | processed | `raw` 或 `processed` |
| n_before | integer | -1 | 触发段前上下文段数（0-20）；`-1` 取 `CLEANSIGHT_TRACEBACK_CONTEXT_BEFORE` |
| n_after | integer | -1 | 触发段后上下文段数（0-20）；`-1` 取 `CLEANSIGHT_TRACEBACK_CONTEXT_AFTER` |

#### 成功响应（200 OK）

`Content-Type: application/vnd.apple.mpegurl`，VOD m3u8 文本，结构与 [`/traceback/task/{task_id}/playlist.m3u8`](#3-任务步骤完整回放-vod-playlist按-task_idstep_id-查整段洗消视频) 相同，仅包含 `[trigger - n_before, trigger + n_after]` 范围内的段。

`#EXT-X-MAP` 指向同 step 共享的 `init.mp4`，`hls.js` 自动先拉 init。

时长生成优先级、是否复用落盘 EXTINF、token 化 URL 与 task playlist 端点一致。

#### 前端 seek 锚点换算

evidence JSON 中每个 clip 的 `ts_ms` 与 playlist 中 `#EXTINF` 累计时间在帧时间轴上对齐（同源段连续编码）。前端切换 clip 时使用：

```javascript
const seekSec = (clips[i].ts_ms - clips[0].ts_ms) / 1000;
video.currentTime = seekSec;
```

#### 错误响应

| 状态码 | 场景 |
| --- | --- |
| 404 | 告警不存在 / 告警 `step_id IS NULL` / 该 alarm 周围找不到段 |
| 422 | 参数非法（`track` 非 raw/processed，`n_before/n_after` 不在 [-1, 20]） |
| 503 | step 目录段存在但 `init.mp4` 缺失（历史段未迁移） |

---

### 3. 任务+步骤完整回放 VOD Playlist（按 task_id+step_id 查整段洗消视频）

```http
GET /traceback/task/{task_id}/playlist.m3u8?step_id=2&track=processed
```

用途：单个洗消步骤（如「酶洗」「漂洗」）的完整回看,前端 `hls.js` 直接消费。
**任务级跨 step 聚合本期不支持**——一次只回放一个步骤。

#### 路径参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| task_id | integer | 任务 ID |

#### 查询参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| step_id | integer | **是** | — | 洗消步骤 id;仅返回该 step 目录下的段 |
| track | string | 否 | processed | `raw` 或 `processed` |

#### 成功响应（200 OK）

返回 `Content-Type: application/vnd.apple.mpegurl`,动态生成的 VOD m3u8：

```m3u8
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="http://host/media/init/<init_token>"
#EXTINF:10.000,
http://host/media/segment/<token>
#EXTINF:10.000,
http://host/media/segment/<token>
#EXT-X-ENDLIST
```

> `#EXT-X-MAP` 行是 fMP4 必备 —— 段是 fragment（无 moov），需要 init.mp4 提供 codec init。
> `hls.js` 1.0+ 自动识别并先拉 init，前端无需特殊处理。

时长生成优先级：
1. 落盘的 `{track}_playlist.m3u8`（LIVE 格式,无 `#EXT-X-ENDLIST`）中的精确 EXTINF
2. 缺失项用相邻段的 `ts_us` 间隔估算
3. 最后一段 fallback 到 `config/persistence_config.yaml` 中 `hls.segment_duration`（默认 10s）

每段 URL 均为 token 化的 `/media/segment/{token}`,不暴露文件路径。

#### 错误响应

| 状态码 | 场景 |
| --- | --- |
| 404 | `(task_id, step_id, track)` 没有任何段 |
| 422 | 缺 `step_id` 参数 / `track` 非法（必须是 `raw` 或 `processed`） |
| 503 | step 目录段存在但 `init.mp4` 缺失（历史段未跑迁移脚本，详见末尾「历史数据迁移」）。响应 body：`{"error": "HLS init segment missing", "detail": "..."}` |

#### 前端播放示例

```javascript
import Hls from 'hls.js';

const video = document.querySelector('video');
const playlistUrl = `/traceback/task/${taskId}/playlist.m3u8?step_id=${stepId}&track=processed`;

if (Hls.isSupported()) {
  const hls = new Hls();
  hls.loadSource(playlistUrl);
  hls.attachMedia(video);
} else if (video.canPlayType('application/vnd.apple.mpegurl')) {
  video.src = playlistUrl; // Safari 原生支持
}
```

---

### 4. 任务+步骤时间轴打点

```http
GET /traceback/task/{task_id}/timeline?step_id=2
```

用途：前端在视频进度条上叠加告警标记。**与 playlist 一样按 step 维度聚合**。

#### 路径参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| task_id | integer | 任务 ID |

#### 查询参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| step_id | integer | **是** | 洗消步骤 id；仅返回该 step 的段时间窗与告警事件 |

#### 成功响应（200 OK）

```json
{
  "task_id": 100,
  "step_id": 2,
  "start_ms": 1700000000000,
  "end_ms": 1700002400000,
  "duration_ms": 2400000,
  "events": [
    {
      "ts_ms": 1700000022000,
      "type": "alarm",
      "alarm_id": 42,
      "alarm_type": "bubble_detected",
      "severity": "high",
      "step_id": 2,
      "step_name": "酶洗",
      "message": "检测到气泡,可能存在漏气"
    }
  ]
}
```

#### 字段说明

| 字段 | 说明 |
| --- | --- |
| `start_ms` / `end_ms` | 该 step 目录下最早 / 最晚段时间戳（ms） |
| `duration_ms` | `end_ms - start_ms`（近似值,最后一段结束时间未计入） |
| `events` | 告警事件列表（仅该 step）,按 `ts_ms` 升序;当前仅 `type=alarm` 一种 |

无段时 `start_ms = end_ms = duration_ms = 0`,`events` 仍可能非空。

#### 错误响应

| 状态码 | 场景 |
| --- | --- |
| 422 | 缺 `step_id` 参数 |
| 503 | 数据库不可用 |

> 没有专门的 404：合法的 `(task_id, step_id)` 即使没有段、没有告警,也会返回全零结构。

---

### 5. 媒体访问——MP4 段

```http
GET /media/segment/{token}
```

用途：流式返回单个 MP4 段。URL 仅由 `/traceback/*` 接口内部签发,前端不应自行构造。

#### 成功响应（200 OK）

`Content-Type: video/mp4`,MP4 文件流。响应头：

```http
Content-Disposition: inline
Cache-Control: private, max-age=60
```

#### 错误响应

| 状态码 | 场景 |
| --- | --- |
| 400 | filename 包含路径分隔符 / token 指向的不是 `.mp4` |
| 403 | token 无效 / 签名不符 / 已过期 / kind 不匹配 |
| 404 | 文件不存在（已被 TTL 清理或段未生成） |

---

### 6. 媒体访问——HLS fMP4 init segment

```http
GET /media/init/{token}
```

用途：返回 HLS fMP4 init segment（每 step 共享一份，包含 ftyp + moov 与 codec init）。
URL 由 playlist 端点在 `#EXT-X-MAP` 行内自动签发，**前端不应自行构造**。`hls.js` 解析 m3u8 后会自动 GET 这个 URL，开发者无需关心调用时机。

#### 成功响应（200 OK）

`Content-Type: video/mp4`，init segment 文件流。响应头：

```http
Content-Disposition: inline
Cache-Control: private, max-age=3600
```

> init.mp4 在一个 step 的整个生命周期内不变，故 `max-age` 为 1 小时（远长于普通 segment 的 60s），减少重复拉取。

#### 错误响应

| 状态码 | 场景 |
| --- | --- |
| 400 | token 指向的 filename 不是 `init.mp4` |
| 403 | token 无效 / 签名不符 / 已过期 / kind 不匹配 |
| 404 | `init.mp4` 不存在（历史段未迁移；通常 playlist 端点会先返回 503 拦住） |

---

## 配置参数

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CLEANSIGHT_MEDIA_TOKEN_SECRET` | 空（临时随机） | HMAC 签名密钥,**生产环境必须设置** |
| `CLEANSIGHT_MEDIA_TOKEN_TTL` | 300 | token 有效期（秒） |
| `CLEANSIGHT_TRACEBACK_CONTEXT_BEFORE` | 1 | 告警证据：触发段前上下文段数 |
| `CLEANSIGHT_TRACEBACK_CONTEXT_AFTER` | 2 | 告警证据：触发段后上下文段数 |

持久化根目录由 `config/persistence_config.yaml` 的 `storage.base_dir` 决定（默认 `./database`）。

---

## 历史数据迁移

部署里同时存在两类历史数据需要分别处理：

### A. step 维度落盘迁移（2026-05 重构）

旧目录 `{base_dir}/{client_id}/{task_id}/` 重整为 `{base_dir}/{task_id}/{step_id}/`：

```bash
# 预览
python scripts/migrate_legacy_runs.py --dry-run

# 实际迁移；目标路径已存在时合并
python scripts/migrate_legacy_runs.py --merge
```

脚本从旧目录的 `metadata.json` 推断 `step_id` 后,把文件移到新路径 `{base_dir}/{task_id}/{step_id}/`。

### B. fMP4 + init segment 升级（2026-05-12）

旧段是普通 MP4（mp4v 或 H.264 + faststart），无法被 `hls.js` 在 m3u8 中作为段播放（会触发 `fragParsingError`）。**playlist 端点检测到 step 目录无 `init.mp4` 时直接返回 503**，前端会拿不到回放。需要把历史段就地升级为 fMP4 fragment 并补出 step 级 `init.mp4`：

```bash
# 预览：列出待迁移的 step 目录与段数
python -m scripts.transcode_segments_to_h264 --dry-run <task_id> [<task_id>...]

# 实际迁移
python -m scripts.transcode_segments_to_h264 <task_id> [<task_id>...]

# 强制重转（已迁移过也再跑一遍，慎用）
python -m scripts.transcode_segments_to_h264 --force <task_id>
```

脚本对每个 step 目录：

1. 把每个 `*_segment_*.mp4` 重转为 fMP4 fragment（原地替换，文件名不变）
2. 第一个段产出时同时落盘 step 级 `init.mp4`（含 codec init），后续段产出的 init 丢弃
3. 重写 `*_playlist.m3u8` 头部为 `#EXT-X-VERSION:7` + `#EXT-X-MAP:URI="init.mp4"`
4. 幂等：含 init.mp4 + V7 头的 step 默认跳过（除非 `--force`）

迁移完成后，原 503 的 playlist 请求可正常返回。

---

## 前端集成示例

### 告警证据双轨对比（推荐：evidence playlist + seek）

> ⚠️ 旧版示例「手拼 m3u8 喂给 hls.js」与「裸 `<video src="…/media/segment/{token}">`」在 fMP4 改造后**均不可用** ——
> evidence 响应中的 `*_clips[].url` 是 fragment URL，不含 `#EXT-X-MAP`/`ftyp+moov`，
> 直接当 mp4 播会触发 `MEDIA_ERR_SRC_NOT_SUPPORTED`，
> 拼成 V3 m3u8 会触发 hls.js 的 `fragParsingError`。
> 推荐改为：用 `/traceback/alarm/{id}/playlist.m3u8` 拿到上下文 VOD m3u8，定位到触发段后 `seek`。

```javascript
async function loadEvidence(alarmId) {
  const ev = await fetch(`/traceback/alarm/${alarmId}/evidence`).then(r => r.json());

  for (const track of ['raw', 'processed']) {
    const clips = ev[`${track}_clips`] || [];
    if (!clips.length) continue;

    const playlistUrl = `/traceback/alarm/${alarmId}/playlist.m3u8?track=${track}`;
    const video = document.getElementById(`${track}-video`);
    const hls = new Hls();
    hls.loadSource(playlistUrl);
    hls.attachMedia(video);

    // 触发段相对 playlist 起点的偏移 = (trigger.ts_ms - clips[0].ts_ms) / 1000
    const triggerIdx = clips.findIndex(c => c.is_trigger);
    const seekSec = triggerIdx > 0
      ? (clips[triggerIdx].ts_ms - clips[0].ts_ms) / 1000
      : 0;
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (seekSec > 0) video.currentTime = seekSec;
      video.play();
    });
  }
}
```

切换其他上下文段时同样按 `(clips[i].ts_ms - clips[0].ts_ms) / 1000` 计算偏移并 `video.currentTime = …`，无需重建 hls 实例。

### 单步骤完整回放 + 进度条告警打点

```javascript
async function loadStep(taskId, stepId) {
  const timeline = await fetch(
    `/traceback/task/${taskId}/timeline?step_id=${stepId}`
  ).then(r => r.json());

  const hls = new Hls();
  hls.loadSource(`/traceback/task/${taskId}/playlist.m3u8?step_id=${stepId}&track=processed`);
  hls.attachMedia(document.getElementById('player'));

  for (const e of timeline.events) {
    const ratio = (e.ts_ms - timeline.start_ms) / Math.max(timeline.duration_ms, 1);
    addMarkerToProgressBar(ratio, e.severity, e.message);
  }
}
```

### 告警 → 跳转所属步骤回放

```javascript
async function jumpFromAlarm(alarmId) {
  const ev = await fetch(`/traceback/alarm/${alarmId}/evidence`).then(r => r.json());
  // evidence 顶层带 task_id / step_id,直接用来拼整段回放 URL
  return loadStep(ev.task_id, ev.step_id);
}
```
