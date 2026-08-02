# `/lab-f3m8` — 送标导出（Label Studio）

让操作员在某个 step 的 raw 整段视频上圈选 N 段 `[start_ms, end_ms]`，后端 ffmpeg 剪出 mp4 并上传到 Label Studio（LS）创建标注任务。

- 数据源：任务列表来自 **DB**（`clean_task` 表）或 **磁盘**（枚举 raw 段目录），由运行时开关 `task_source` 决定；裁剪素材来自磁盘 raw 段（复用 traceback 的 `(task_id, step_id)` 文件约定）。
- 无任何持久化状态（除失败时保留的临时 `job_dir`）；无新表。
- 本组所有端点**均无鉴权、正常返回 200**；前缀 `lab-f3m8` 含混淆串防自动扫描器。
- 静态 UI：`GET /lab-f3m8/ui`（`app/static/lab`，`html=True`）。
- 通用约定（Base URL / Gateway / 错误模型 / 枚举）见 [README](README.md)。

> 时间单位在本组不统一：`/tasks` 返回的任务时间是 **epoch 秒**，而 `clips[].start_ms/end_ms/duration_ms` 是**毫秒**（绝对墙钟 ms，与 traceback timeline 同源）。逐字段已标注，别混用。

典型流程：

```
① GET /config  ──→ 确认 task_source / LS 已配置
② GET /tasks   ──→ task_id + step_id + raw_steps
③ POST /submit ──→ 裁剪 + 送标（per-clip 结果）
   GET /health ──→ 送标前探测 LS 是否可达
```

---

## GET /lab-f3m8/tasks

**用途**：给送标页面列出「有 raw 段、可送标」的任务，让操作员选 `task_id` + `step_id`。数据来自 DB 或磁盘（见下）。

**查询参数**：

| 参数 | 类型 | 必填 | 默认 | 范围/说明 |
|------|------|------|------|-----------|
| `q` | string | 否 | null | 子串过滤，**≤64 字符**（超长 → 422）。DB 模式匹配 `task_id`/`source_ip`/`status`；storage 模式只匹配 `str(task_id)` |
| `limit` | int | 否 | 50 | **1–200**，越界 → 422 |
| `offset` | int | 否 | 0 | **≥0**，负数 → 422 |

> 参数越界由 FastAPI 校验，返回 **422**（非业务 400）。

### 响应 `200` `LabTaskListResponse`

```jsonc
{
  "total": 12,                         // 过滤后总数（分页前）
  "tasks": [
    {
      "task_id": 123,
      "source_ip": "192.168.1.100",    // storage 模式恒 null
      "current_step": "1",             // storage 模式恒 null
      "step_id": 1,                    // storage 模式恒 null
      "status": "running",             // storage 模式恒 "unknown"
      "updated_time": 1751800000,      // epoch 秒，可 null
      "start_time": 1751799000,        // epoch 秒，可 null
      "end_time": null,                // epoch 秒，storage 模式恒 null
      "raw_steps": [1, 2],             // 磁盘上有 raw 段的 step 列表
      "has_raw_segments": true,
      "has_current_step_raw": true     // storage 模式恒 false
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null 条件） |
|------|------|---------------------|
| `total` | int | 过滤后、分页前的总数 |
| `tasks[].task_id` | int | → `POST /submit` 的 `task_id` |
| `tasks[].source_ip` | string \| null | DB 模式取自表（表值本身可为 null）；**storage 模式恒 null** |
| `tasks[].current_step` | string \| null | DB 模式为 `current_step` 字符串；行内为 null 或 **storage 模式恒 null** |
| `tasks[].step_id` | int \| null | `current_step` 解析为 int；无法解析或 **storage 模式恒 null** |
| `tasks[].status` | string \| null | DB 模式取自表；**storage 模式恒 `"unknown"`** |
| `tasks[].updated_time` | int \| null | epoch **秒**。DB 模式取表；storage 模式取该 task 所有 raw 段 `ts_ms` 的最大值，无段则 null |
| `tasks[].start_time` | int \| null | epoch **秒**。DB 模式取表；storage 模式取 raw 段 `ts_ms` 最小值，无段则 null |
| `tasks[].end_time` | int \| null | epoch **秒**。DB 模式取表；**storage 模式恒 null** |
| `tasks[].raw_steps` | int[] | 磁盘上确有 raw 段的 step_id（升序） |
| `tasks[].has_raw_segments` | bool | `raw_steps` 非空。storage 模式只收有 raw 段的 task，故恒 true |
| `tasks[].has_current_step_raw` | bool | `step_id ∈ raw_steps`。**storage 模式恒 false** |

排序：`updated_time desc, task_id desc`。空结果返回 `{"total":0,"tasks":[]}`，不报错。

> storage 模式设计意图：DB 挂了也能列任务送标，代价是丢失所有 DB 才有的字段（IP/状态/当前步）。前端切到 storage 模式时须容忍上述字段为 null / "unknown"。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `422` | `q` 超 64 / `limit` 越界 / `offset<0` | FastAPI 校验体（`{"detail":[...]}`） |
| `503` | **仅 DB 模式**：查库抛 `SQLAlchemyError` | `{"error":"Failed to list lab tasks","retryable":true,...}` |

> storage 模式完全不碰 DB，故 DB 挂时不报 503——这正是它存在的理由。

### 前端坑点

- 时间是 **epoch 秒**，与 `/submit` 的毫秒不同源，别拿去当 `start_ms`。
- storage 模式下大量字段退化，务必先 `GET /config` 读 `task_source` 再决定 UI 展示。
- 分页 `total` 是过滤后总数；翻页用 `offset += limit`。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| 列表为空但你确信有任务 | 该 task 磁盘上无 raw 段（storage 模式直接不收）；或 `q` 过滤掉了 |
| 某任务字段全是 null/"unknown" | 当前处于 **storage 模式**，非 bug |

---

## POST /lab-f3m8/submit

**用途**：把圈选的 N 段裁成 mp4 并逐段上传 LS。**整个流程同步**（ffmpeg + 上传都在请求线程池里跑完，段多/段长会明显阻塞）。**单段失败不拖垮整请求**：HTTP 仍 200，逐段在 `clips[]` 里带 `success`/`error_code`。

**请求体** `LabSubmitRequest`：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `task_id` | int | 是 | — | 运行键 |
| `step_id` | int | 是 | — | 步序号 |
| `project_id` | int \| null | 否 | null | LS project id；为空/0 时回退 `default_project_id`（见 /config），都没有 → 400 |
| `clips` | array | 是 | — | **≥1 段**（空 → 422），元素见下 |
| `keep_artifacts_on_failure` | bool | 否 | true | 任一段失败时是否保留 `job_dir` 下的 mp4 供手动重试 |

`clips[]` 元素 `LabClipRange`：

| 字段 | 类型 | 必填 | 约束 |
|------|------|------|------|
| `start_ms` | int | 是 | **≥0**（绝对墙钟 ms），否则 422 |
| `end_ms` | int | 是 | **≥1**，否则 422 |
| `label` | string \| null | 否 | **≤64 字符**，透传到 LS `task.data` 作标注 hint |

### 响应 `200` `LabSubmitResponse`

```jsonc
{
  "task_id": 123,
  "step_id": 1,
  "project_id": 5,                     // 实际使用的 project（含 default 回退后的值）
  "job_dir": null,                     // 仅「有段失败 且 keep_artifacts_on_failure=true」时非空
  "total": 2,
  "success_count": 1,
  "failure_count": 1,
  "clips": [
    {
      "start_ms": 0, "end_ms": 3000,   // epoch 墙钟毫秒
      "success": true,
      "label_studio_task_id": 9001,    // 成功时 LS 分配的 task id；失败时 null
      "duration_ms": 3000,             // 毫秒；build 成功后才有，range/gap 失败时 null
      "size_bytes": 512000,            // build 成功后才有，否则 null
      "n_source_segments": 2,          // 该 clip 由几段 raw 拼成；build 成功后才有，否则 null
      "error_code": null,              // 见下方全集
      "error": null                    // 人读错误详情
    }
  ]
}
```

| 字段 | 类型 | 说明（含 null 条件） |
|------|------|---------------------|
| `project_id` | int | 实际用的 project（req 优先，否则 default 回退后的值） |
| `job_dir` | string \| null | 临时产物目录**绝对路径**。**仅当有段失败 且 `keep_artifacts_on_failure=true`** 时非空；否则（全成功，或不要求保留）已被删除，返回 null |
| `success_count` / `failure_count` | int | 成功/失败段数，和为 `total` |
| `clips[].duration_ms` | int \| null | 毫秒。仅 build 成功（含上传失败）时有；`range_out_of_bounds`/`range_gap`/`ffmpeg_failed` 时 null |
| `clips[].size_bytes` | int \| null | 同上 null 条件 |
| `clips[].n_source_segments` | int \| null | 同上 null 条件 |
| `clips[].label_studio_task_id` | int \| null | 仅 `success=true` 时有；LS 建了任务但响应不含 id 时也可能 null 而 success=true（见静默失败） |
| `clips[].error_code` | string \| null | 见下表；`success=true` 时 null |

**per-clip `error_code` 全集**（均在 200 内、逐段）：

| error_code | 触发条件 | 前端处置建议 |
|------------|---------|-------------|
| `range_out_of_bounds` | 选的 `[start_ms,end_ms]` 与该 step 的 raw 段无任何重叠 | 提示「所选时间不在可用录像范围内」，引导重新在 timeline 内圈选 |
| `range_gap` | 范围内相邻段间隔超过 `gap_tolerance_ms`（真录制停顿：源断流/重连） | 提示「该时段录像中断，请缩小范围避开断点」 |
| `ffmpeg_failed` | ffmpeg 裁剪失败 / 输出文件缺失 | 视为可重试的后端错误，提示重试；持续失败上报运维查 ffmpeg |
| `ls_bad_response` | LS 返回非预期（含无 `task_ids`、4xx 非鉴权、解析失败） | 提示「送标失败」，保留 `job_dir` 后可手动重试；查 LS project 是否存在 |
| `ls_unreachable` | 连不上 LS（网络/超时/URLError） | 提示「Label Studio 不可达」，先 `GET /health` 确认连通再重试 |
| `ls_auth` | LS 返回 401/403 | token 失效/无权限，需后端改 env `CLEANSIGHT_LABEL_STUDIO_TOKEN` |

> `error_code` 非仅 `ls_bad_response`——LS 客户端会区分 `ls_unreachable`/`ls_auth`；无 code 时才兜底成 `ls_bad_response`。别把「送标失败」一律当同一种处理。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `400` | 见下「400 全部触发条件」 | `{"error":"...","detail":"...","field":"clips"\|"project_id"}` |
| `404` | `(task_id, step_id)` 无任何 raw 段 | `{"error":"...","resource_type":"Segments","resource_id":"task=..,step=..,track=raw"}` |
| `422` | 请求体字段级校验失败（`clips` 为空、`start_ms<0`、`end_ms<1`、`label>64`） | FastAPI 校验体 |
| `503` | LS **url 或 token 未配置** | `{"error":"Label Studio not configured","detail":"url 可在页面填、token 须 env"}`（HTTPException，**body 只有 `detail`，无 `retryable`**） |

**`400` 全部触发条件**（`_validate_clips` / `_resolve_project_id`，整请求级、任一即拒）：

- `clips` 段数 > `lab_export_max_clips_per_submit`（默认 20），`field=clips`；
- 某段 `end_ms ≤ start_ms`，`field=clips`；
- 某段时长（`end_ms-start_ms`）> `lab_export_max_clip_ms`（默认 300000 ms = 5 min），`field=clips`；
- 按 `start_ms` 排序后相邻段**重叠**（`start_ms < 前一段 end_ms`），`field=clips`；
- 各段时长之和 > `lab_export_max_total_ms`（默认 1800000 ms = 30 min），`field=clips`；
- `project_id` 缺失（req 未传且无 default），`field=project_id`。

> 校验顺序：先查 LS 配置（**503**）→ 再算 project_id/校验 clips（**400**）→ 再查段存在性（**404**）。故未配置 LS 时即便入参也非法，也先返回 503。
> **503 与 400/404 的 body 形态不一致**：503 是 HTTPException（`{"detail":{...}}`，无 `field`/`retryable`），400/404 是业务异常体（带 `field` 或 `resource_type`）。**判分支只认 status code，别依赖 body 字段**。

### 前端坑点

- 单段上限 5 min、总时长 30 min、最多 20 段（默认值，以后端 settings 为准）；圈选时前端最好先本地拦一道，减少 400。
- 段重叠是**排序后**判的，`clips` 顺序无所谓，但两段时间区间不能相交。
- `job_dir` 非空 = 有段失败且要求了保留；此目录**没有 TTL / 自动清理**，会一直留在 `{temp_dir}/{nonce}`（默认 `{storage_base_dir}/.lab_exports`）直到人工/运维清。别指望它过期自动消失。
- 同步端点，段多时响应慢，前端请给足超时、加 loading。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| HTTP 200 但 `failure_count>0` | 正常——单段失败不影响整请求，逐段看 `error_code` |
| `success=true` 但 `label_studio_task_id=null` | LS 确已建任务但响应未回 id，后端按成功处理，非 bug |
| 全成功却 `job_dir=null` | 正常——成功即清理产物，只有失败+keep 才保留 |
| 反复 `ls_unreachable`/`ls_auth` | LS 配置/网络/token 问题，先走 `GET /health` 定位 |

参考实现：仓库内消费本接口的送标页面见 [app/static/lab/index.html](../../app/static/lab/index.html)。

---

## GET /lab-f3m8/health

**用途**：送标前探测 LS 是否**已配置**、是否**可达**、token 是否有效。**不抛异常**——未配置返回 `configured=false`，探测失败返回 `reachable=false` + `error`，HTTP 恒 200。

### 响应 `200` `LabHealthResponse`

```jsonc
{
  "configured": true,                  // url 与 token 都非空
  "reachable": true,                   // 单次 ping 成功
  "error": null,                       // 未配置或 ping 失败时的原因文本
  "label_studio_url": "http://...",    // 未配置时可能为 null
  "default_project_id": 5
}
```

| 字段 | 类型 | 说明（含 null 条件） |
|------|------|---------------------|
| `configured` | bool | `url` 与 `token` 均非空 |
| `reachable` | bool | 已配置时做一次 ping 的结果；未配置恒 false |
| `error` | string \| null | 未配置 → 提示语；ping 失败 → `"{类型}: {消息}"`；正常 → null |
| `label_studio_url` | string \| null | 生效 url；未配置且 url 为空时 null |
| `default_project_id` | int | 生效默认 project_id（0 表示无默认） |

> 探测超时：单次 ping 固定 **10 秒**（`LabelStudioClient(timeout=10)`）。ping 内任何异常都被吞成 `reachable=false` + `error`，不会 5xx。

### 错误

无（尽力而为，恒 200）。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| `configured=true` 但 `reachable=false` | url/token 已填但 LS 连不上或 token 无效；看 `error` 文本 |
| `configured=false` | url 或 token 缺失；token 只能在后端 env 配 |

---

## GET /lab-f3m8/config

**用途**：读当前 LS 连接配置，供页面回显。**不做网络探测**（探测走 `/health`），**不返回 token 明文**。

### 响应 `200` `LabConfigResponse`

```jsonc
{
  "label_studio_url": "http://...",    // 生效 url，"" 表示未配置
  "default_project_id": 5,             // 0 表示无默认
  "task_source": "db",                 // "db" | "storage"，控制 /tasks 数据源
  "token_configured": true,            // env 里是否设了 token（不回明文）
  "source": "file"                     // "file"（PUT 改过、来自持久化文件）| "env"（回退环境变量）
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `label_studio_url` | string | 空串 = 未配置（不是 null） |
| `default_project_id` | int | 0 = 无默认 |
| `task_source` | string | `db` \| `storage` |
| `token_configured` | bool | env `CLEANSIGHT_LABEL_STUDIO_TOKEN` 是否非空；token 明文不外露 |
| `source` | string | `file`（存在持久化 JSON、以其为准）\| `env`（无文件或读文件失败，回退 settings） |

> token 永远只来自 env、页面不可改；`token_configured` 是唯一可见信号。

### 错误

无（恒 200）。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| PUT 过 url 但 `source` 仍是 `env` | 持久化文件读取失败被降级回退（看后端日志 `读取 ... 失败`），你的改动可能未生效 |

---

## PUT /lab-f3m8/config

**用途**：改 LS `url` / `default_project_id` / `task_source` 并**持久化到 JSON 文件**（`{storage_base_dir}/lab_runtime_config.json`），即时生效、跨重启保留。**token 不在此管理**（仅 env）。

**请求体** `LabConfigUpdateRequest`：

| 字段 | 类型 | 必填 | 默认 | 约束 |
|------|------|------|------|------|
| `label_studio_url` | string | 否 | `""` | **空串 = 禁用/未配置**；非空须以 `http://` 或 `https://` 开头（尾部 `/` 会被去掉） |
| `default_project_id` | int | 否 | 0 | **≥0**（0 = 无默认），负数 → 400 |
| `task_source` | string \| null | 否 | null | `db` \| `storage`；**null = 不改**（只想改 url 时别把模式冲掉） |

### 响应 `200`

同 GET 的 `LabConfigResponse`，其中 `source` 更新后**恒为 `file`**。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `400` | `label_studio_url` 非空且不以 `http(s)://` 开头 / `default_project_id<0` / `task_source` 非 `db`\|`storage` | `{"error":"...","detail":"...","field":"label_studio_url"}` |
| `422` | `default_project_id` 传了非 int | FastAPI 校验体 |

> 三种 400 都被路由统一挂到 `field="label_studio_url"`（即便实际错在 `default_project_id`/`task_source`），别据 `field` 反推是哪个字段错——看 `error`/`detail` 文本。

### 前端坑点

- 想「禁用 LS」就传 `label_studio_url=""`；传别的非法串会 400。
- 只改数据源模式（DB 挂了切 storage）时，`label_studio_url` 必须一并回传当前值，否则会被默认 `""` 冲掉（该字段默认空串，缺省即清空 url）。`task_source` 缺省不改，url/project_id 缺省会覆盖。

### 静默失败

| 现象 | 后端实际状态 |
|------|------------|
| 只想改 `task_source`，结果 url 被清空 | `label_studio_url` 未回传，取默认 `""` 覆盖了旧值——PUT 是整体覆盖 url/project_id，非 patch |
