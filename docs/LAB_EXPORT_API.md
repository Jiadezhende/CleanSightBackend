<!-- markdownlint-configure-file {"MD024": {"siblings_only": true}} -->

# Lab 视频段导出 & Label Studio 送标 API

> **最后更新**: 2026-05-11
>
> 用于：从一个洗消步骤的 raw 视频上截取 N 段不重叠的 mp4，逐条提交到 Label Studio 创建标注任务。
>
> 与 [TRACEBACK_API.md](TRACEBACK_API.md) 的关系：
>
> - **复用** `SegmentFinder` 与落盘约定（`{base_dir}/{task_id}/{step_id}/raw_segment_{ts_us}.mp4`）
> - **不复用** media token —— 本接口产物是「剪好的连续 mp4」，直接 push 到 LS，不走 `/media/segment/{token}`
> - 前端视频播放仍走 `/traceback/task/{task_id}/playlist.m3u8?step_id=...&track=raw`，时间轴打点仍走 `/traceback/task/{task_id}/timeline?step_id=...`

---

## 数据流

```text
前端 Lab 页面
  ├── GET  /traceback/task/{task_id}/playlist.m3u8?step_id=...&track=raw     ← raw VOD 播放
  ├── GET  /traceback/task/{task_id}/timeline?step_id=...                    ← 告警时间打点
  ↓
  操作员在视频上选 N 段 [start_ms, end_ms]（不重叠）
  ↓
  POST /lab/submit  { task_id, step_id, project_id, clips: [...] }
                                       │
                                       ↓
                                ClipBuilder (ffmpeg concat + -ss/-to)
                                       │
                                       ↓
                              LabelStudioClient (multipart POST)
                                       │
                                       ↓
                              Label Studio /api/projects/{pid}/import
```

后端 ffmpeg 拼接是**有损重编码**（libx264 + faststart），目的是支持 ms 精度裁剪 ——
段文件之间用 `concat demuxer` 衔接，整体 `-ss offset -to (offset+duration)` 一次完成裁剪与转码。

---

## 接口列表

### 1. 提交视频段送标

```http
POST /lab/submit
Content-Type: application/json
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | integer | 是 | 任务 id |
| `step_id` | integer | 是 | 洗消步骤 id |
| `project_id` | integer | 否 | LS project id；不传则使用 `CLEANSIGHT_LABEL_STUDIO_DEFAULT_PROJECT_ID` |
| `clips` | array | 是 | 待截取段列表，1~`max_clips_per_submit` 段（默认 20） |
| `clips[].start_ms` | integer | 是 | 段起点（绝对墙钟 ms，与 traceback timeline 的 `ts_ms` 同时基） |
| `clips[].end_ms` | integer | 是 | 段终点；必须 > `start_ms` |
| `clips[].label` | string | 否 | 透传到 LS task.data 的标注 hint，≤ 64 字符 |
| `keep_artifacts_on_failure` | bool | 否 | 默认 `true`；任一段失败时保留临时 mp4 文件供手动重试 |

**请求示例**：

```json
{
  "task_id": 100,
  "step_id": 2,
  "project_id": 7,
  "clips": [
    {"start_ms": 1715400123000, "end_ms": 1715400128000, "label": "bubble"},
    {"start_ms": 1715400140000, "end_ms": 1715400143500}
  ],
  "keep_artifacts_on_failure": true
}
```

#### 入参校验（任一失败 → 400）

- 任意 `end_ms <= start_ms` → 400
- 排序后相邻段 `clips[i+1].start_ms < clips[i].end_ms`（重叠）→ 400
- 任一段 `duration > CLEANSIGHT_LAB_EXPORT_MAX_CLIP_MS`（默认 5 min）→ 400
- 段数 > `CLEANSIGHT_LAB_EXPORT_MAX_CLIPS_PER_SUBMIT`（默认 20）→ 400
- 总时长 > `CLEANSIGHT_LAB_EXPORT_MAX_TOTAL_MS`（默认 30 min）→ 400
- `project_id` 既未传也无默认 → 400

#### 成功响应（200）

```json
{
  "task_id": 100, "step_id": 2, "project_id": 7,
  "job_dir": null,
  "total": 2, "success_count": 2, "failure_count": 0,
  "clips": [
    {
      "start_ms": 1715400123000, "end_ms": 1715400128000,
      "success": true, "label_studio_task_id": 4521,
      "duration_ms": 5000, "size_bytes": 820144, "n_source_segments": 1,
      "error_code": null, "error": null
    },
    {
      "start_ms": 1715400140000, "end_ms": 1715400143500,
      "success": true, "label_studio_task_id": 4522,
      "duration_ms": 3500, "size_bytes": 612001, "n_source_segments": 1,
      "error_code": null, "error": null
    }
  ]
}
```

#### 部分失败响应（仍 200）

设计哲学：单段失败不让整请求失败。HTTP 仍 `200`，调用方根据 `clips[].success` 自行决定怎么重试。

```json
{
  "task_id": 100, "step_id": 2, "project_id": 7,
  "job_dir": "./database/.lab_exports/8f3c2e1a",
  "total": 2, "success_count": 1, "failure_count": 1,
  "clips": [
    {"start_ms": 1715400123000, "end_ms": 1715400128000,
     "success": true, "label_studio_task_id": 4521,
     "duration_ms": 5000, "size_bytes": 820144, "n_source_segments": 1,
     "error_code": null, "error": null},
    {"start_ms": 1715400140000, "end_ms": 1715400143500,
     "success": false, "label_studio_task_id": null,
     "duration_ms": 3500, "size_bytes": 612001, "n_source_segments": 1,
     "error_code": "ls_unreachable",
     "error": "URL error: timed out"}
  ]
}
```

返回的 `job_dir` 是保留下来的临时目录绝对路径（或相对于项目根的路径）；
里面是 `clip_{start_ms}_{end_ms}.mp4`，操作员可手动 `curl` 重传或 scp 处理。

#### HTTP 错误码

| 状态码 | 场景 |
| --- | --- |
| 400 | 区间颠倒 / 重叠 / 超数量 / 超时长 / 缺 `project_id` |
| 404 | `(task_id, step_id)` 没有 raw 段 |
| 503 | LS 未配置（`CLEANSIGHT_LABEL_STUDIO_URL` 或 `_TOKEN` 为空） |
| 500 | 其它未捕获错误 |

#### 单段 `error_code` 枚举（响应 200，单段失败）

| `error_code` | 含义 |
| --- | --- |
| `range_out_of_bounds` | `[start_ms, end_ms]` 与该 step 的任何段都不重叠 |
| `range_gap` | 重叠的段之间存在大于容忍阈值（默认 500 ms）的时间空隙 |
| `ffmpeg_failed` | ffmpeg 退出码非 0 或超时 |
| `ls_unreachable` | LS 连不上 / 超时 / DNS 错误 |
| `ls_auth` | LS 返回 401/403（token 失效或无项目权限） |
| `ls_bad_response` | LS 返回非 JSON / 没有 task_id |

---

### 2. 健康探测

```http
GET /lab/health
```

不抛异常的探活：返回 LS 是否已配置 + 当前 token 能否打通 LS。

#### 响应（200）

```json
{
  "configured": true,
  "reachable": true,
  "error": null,
  "label_studio_url": "http://10.176.122.22:8080",
  "default_project_id": 7
}
```

LS 未配置时 `configured=false`，`error` 提示 url 可在送标页面设置、token 需后端 env。

---

### 3. LS 连接配置（url / default_project_id 运行时可改）

`label_studio_url` 与 `default_project_id` 可在送标页面修改并持久化，**改完即时生效、重启后保留，无需重启后端**。
持久化文件：`{storage.base_dir}/lab_runtime_config.json`（已 gitignore）。
解析优先级：文件存在 → 用文件值；否则回退到 env 默认值。

`token` 不在此管理：恒来自 env（`CLEANSIGHT_LABEL_STUDIO_TOKEN`），页面不可见、不可改。

#### 读取当前配置

```http
GET /lab-f3m8/config
```

不做网络探测（探测见 `/health`）。响应（200）：

```json
{
  "label_studio_url": "http://10.176.122.22:8080",
  "default_project_id": 7,
  "token_configured": true,
  "source": "file"
}
```

- `token_configured`：env 里 token 是否已配置（**不返回明文**）
- `source`：`file`（页面改过并持久化）| `env`（仍是环境变量默认值）

#### 更新配置

```http
PUT /lab-f3m8/config
Content-Type: application/json

{ "label_studio_url": "http://10.176.122.22:8080", "default_project_id": 7 }
```

- `label_studio_url`：为空表示「未配置」；非空须以 `http://` 或 `https://` 开头，否则 400
- `default_project_id`：`>= 0`，0 表示无默认值
- 成功返回更新后的配置（同 `GET /config` 响应体）

---

## 配置项（环境变量）

> `LABEL_STUDIO_URL` 与 `LABEL_STUDIO_DEFAULT_PROJECT_ID` 现在是**回退默认值**：
> 一旦通过 `PUT /lab-f3m8/config`（或送标页面「⚙ LS 设置」）改过，运行时配置文件优先于环境变量。
> `LABEL_STUDIO_TOKEN` 仍只能通过环境变量配置。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CLEANSIGHT_LABEL_STUDIO_URL` | 空 | LS 服务器 base URL，如 `http://10.176.122.22:8080`；**必填** |
| `CLEANSIGHT_LABEL_STUDIO_TOKEN` | 空 | LS 个人 API token（`Authorization: Token <...>`）；**必填** |
| `CLEANSIGHT_LABEL_STUDIO_DEFAULT_PROJECT_ID` | 0 | 默认 project_id；0 = 未配置（请求需显式传 `project_id`） |
| `CLEANSIGHT_LAB_EXPORT_TEMP_DIR` | 空 | 临时输出目录；空则用 `{storage.base_dir}/.lab_exports` |
| `CLEANSIGHT_LAB_EXPORT_FFMPEG_PRESET` | `veryfast` | libx264 preset |
| `CLEANSIGHT_LAB_EXPORT_MAX_CLIP_MS` | 300000 | 单段时长上限（5 min） |
| `CLEANSIGHT_LAB_EXPORT_MAX_TOTAL_MS` | 1800000 | 单次提交总时长上限（30 min） |
| `CLEANSIGHT_LAB_EXPORT_MAX_CLIPS_PER_SUBMIT` | 20 | 单次提交最大段数 |
| `CLEANSIGHT_FFMPEG_PATH` | `ffmpeg` | ffmpeg 可执行文件（沿用现有 settings） |

LS 未配置时整个 `/lab/submit` 直接 503；不阻塞应用启动。

---

## 前端集成示例

```javascript
// 1) 拉播放列表 + 时间轴
const [m3u8Url, timeline] = await Promise.all([
  `${API_BASE}/traceback/task/${taskId}/playlist.m3u8?step_id=${stepId}&track=raw`,
  fetch(`${API_BASE}/traceback/task/${taskId}/timeline?step_id=${stepId}`).then(r => r.json()),
]);

// 2) 用户在播放器上选了 N 段（绝对墙钟 ms）
const clips = [
  { start_ms: 1715400123000, end_ms: 1715400128000, label: 'bubble' },
  { start_ms: 1715400140000, end_ms: 1715400143500 },
];

// 3) 一次性 POST 给后端
const resp = await fetch(`${API_BASE}/lab/submit`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    task_id: taskId,
    step_id: stepId,
    project_id: 7,
    clips,
  }),
}).then(r => r.json());

console.log(`成功 ${resp.success_count}/${resp.total}`);
resp.clips.filter(c => !c.success).forEach(c =>
  console.warn(`[${c.start_ms},${c.end_ms}] ${c.error_code}: ${c.error}`)
);
```

---

## 验证步骤

1. **前置准备**：跑一次录制，`ls {base_dir}/100/2/raw_segment_*.mp4` 应有若干段；
   通过 `/traceback/task/100/timeline?step_id=2` 拿到 `start_ms` / `end_ms` 区间。
2. **LS 可达性**：
   ```bash
   curl -H "Authorization: Token $LS_TOKEN" $LS_URL/api/version
   curl http://localhost:8000/lab/health
   ```
3. **单段送标**：
   ```bash
   curl -X POST http://localhost:8000/lab/submit \
     -H 'Content-Type: application/json' \
     -d '{"task_id":100,"step_id":2,"project_id":7,
          "clips":[{"start_ms":1715400123000,"end_ms":1715400128000}]}'
   ```
   期望 `success_count=1`，返回 `label_studio_task_id`。
4. **在 LS 端核对**：
   ```bash
   curl -H "Authorization: Token $LS_TOKEN" $LS_URL/api/tasks/{label_studio_task_id}
   ```
   并在 LS UI 中打开 task 检查视频可播、时长 ≈ 5000 ms。
5. **离线核对 mp4**（设 `keep_artifacts_on_failure=true` 后故意 break LS URL）：
   ```bash
   ffprobe {job_dir}/clip_*.mp4   # codec=h264 / duration ≈ end-start
   ```
6. **失败路径回归**：
   - `CLEANSIGHT_LABEL_STUDIO_URL=http://127.0.0.1:1` → 单段 `error_code=ls_unreachable`，mp4 保留
   - 重叠区间 → 400
   - `start_ms` 早于任何段 → 单段 `error_code=range_out_of_bounds`

---

## 已知限制

- **整文件入内存**：multipart 上传时把整个 mp4 一次性读到内存（沿用项目 `urllib.request` 风格，未引入 streaming）；
  当前 ≤5 min / clip 可接受，超大文件需改造为 streaming。
- **同步阻塞**：`/lab/submit` 是同步接口，每段 ffmpeg 重编码 + LS 上传都在请求线程里完成；
  对短段（<1 min）秒级返回，对接近上限的 5 min 段可能耗时数十秒。
- **仅 raw 轨**：processed 轨（带模型可视化）不送标，避免偏置标注者。
- **无重试**：LS 失败一次报告一次，操作员根据 `job_dir` 决定怎么重传；后端不替你做幂等。
- **无后台 worker**：没有 job 状态表、没有轮询接口；状态完全靠同步响应携带。
- **无 import_url 模式**：不让 LS 反向拉取 `/media/clip/{token}`，避免反向网络可达性的运维负担。

---

## 故障恢复

当 `failure_count > 0` 且 `keep_artifacts_on_failure=true`（默认）时，响应里 `job_dir` 字段给出保留的临时目录。该目录下：

```
{job_dir}/
  clip_{start_ms}_{end_ms}.mp4   # 已生成但未送上 LS 的 mp4（每段一个）
```

操作员可：

1. 直接用 `curl -F file=@clip_xxx.mp4 -H "Authorization: Token $LS_TOKEN" $LS_URL/api/projects/{pid}/import` 手动重传
2. 或者修复 LS 后再次调用 `/lab/submit`（用相同的 `start_ms`/`end_ms` 即可重新生成；段是幂等的）

成功重传或确认废弃后请手动删除 `job_dir`（后端不会自动清理失败留下的文件，避免误删未送达的产物）。
