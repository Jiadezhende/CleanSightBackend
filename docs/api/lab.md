# `/lab-f3m8` — 送标导出

从 raw 轨裁剪 clip 并提交 Label Studio 送标。均无鉴权、返回 200。前缀含混淆串。静态 UI：`GET /lab-f3m8/ui`。通用约定见 [README](README.md)。

---

## GET /lab-f3m8/tasks

可送标任务列表。数据源由运行时配置 `task_source` 决定（`db` 查 `clean_task` 表并交叉核对磁盘 raw 段；`storage` 枚举存储目录，DB 不可用时兜底）。

**查询**：`q`（str，≤64，子串过滤 task_id/source_ip/status）、`limit`（int，默认 50，1–200）、`offset`（int，默认 0）。

**200** `LabTaskListResponse`：

```jsonc
{
  "total": 12,
  "tasks": [
    { "task_id": 123, "source_ip": "192.168.1.100", "current_step": "1", "step_id": 1,
      "status": "running", "updated_time": 1751800000, "start_time": 1751799000, "end_time": null,
      "raw_steps": [1, 2], "has_raw_segments": true, "has_current_step_raw": true }
  ]
}
```

（时间字段为 epoch 秒，可为 null。）

---

## POST /lab-f3m8/submit

裁剪 N 个 clip 并提交送标。**同步**执行；单个 clip 失败不影响整体（200 + per-clip 结果）。

**请求体** `LabSubmitRequest`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | int | |
| `step_id` | int | |
| `project_id` | int? | 缺省用 `settings.label_studio_default_project_id` |
| `clips` | list | ≥1 个，元素 `{start_ms:int≥0, end_ms:int≥1, label?:str≤64}` |
| `keep_artifacts_on_failure` | bool | 默认 true |

**200** `LabSubmitResponse`：

```jsonc
{
  "task_id": 123, "step_id": 1, "project_id": 5,
  "job_dir": null,                    // 临时目录；仅失败且 keep_artifacts_on_failure=true 时非空
  "total": 2, "success_count": 1, "failure_count": 1,
  "clips": [
    { "start_ms": 0, "end_ms": 3000, "success": true, "label_studio_task_id": 9001,
      "duration_ms": 3000, "size_bytes": 512000, "n_source_segments": 2,
      "error_code": null, "error": null }
  ]
}
```

**整体校验错误**：
- `400`：`clips` 数超 `lab_export_max_clips_per_submit`；某 clip `end_ms ≤ start_ms`；单 clip 时长超 `lab_export_max_clip_ms`；clip 重叠（按 start_ms 排序后）；总时长超 `lab_export_max_total_ms`；`project_id` 缺失。
- `404`：(task_id, step_id) 无 raw 段。
- `503`：Label Studio URL/token 未配置（细节在响应体）。

**单 clip `error_code`**（200 内 per-clip）：`range_out_of_bounds`（范围超出可用段）、`range_gap`（范围内段间隙超容忍）、`ffmpeg_failed`（裁剪失败）、`ls_bad_response`（Label Studio 导入失败）。

---

## GET /lab-f3m8/health

Label Studio 连通性 + 配置检查（不抛异常，单次 ping 超时 10s）。

**200** `LabHealthResponse`：`{ "configured": bool, "reachable": bool, "error": str?, "label_studio_url": str?, "default_project_id": int }`。

---

## GET /lab-f3m8/config

**200** `LabConfigResponse`：

```jsonc
{
  "label_studio_url": "http://...", "default_project_id": 5,
  "task_source": "db",               // db | storage
  "token_configured": true,          // 是否设了 CLEANSIGHT_LABEL_STUDIO_TOKEN
  "source": "file"                   // file（PUT 持久化）| env（回退）
}
```

## PUT /lab-f3m8/config

更新连接配置（写文件，跨重启保留；token 仍只在 env）。

**请求体** `LabConfigUpdateRequest`：`label_studio_url`（"" 或 `http(s)://...`）、`default_project_id`（≥0，0 = 无默认）、`task_source`（`db`|`storage`|null 不改）。

**200**：同 GET 的 `LabConfigResponse`（`source` 恒为 `file`）。
**错误**：`400`（url 非 http(s)、`default_project_id<0`、`task_source` 非法）。
