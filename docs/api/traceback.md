# `/traceback` — 告警证据与回放

按告警 / 任务步骤定位 HLS 段，返回**证据 clip 列表**、**VOD playlist** 或**时间轴**。所有媒体 URL 均为 token 化的 `/media/*`（见 [media.md](media.md)），由本组端点签发。通用约定见 [README](README.md)。

`track` 取值：`raw` | `processed`（默认 `processed`）。`n_before` / `n_after`：触发段前后上下文段数，范围 `-1..20`，`-1` 用配置默认。

---

## GET /traceback/alarm/{alarm_id}/evidence

告警证据：触发段 ± 上下文的 raw/processed clip 列表。

**路径**：`alarm_id`（int）。**查询**：`n_before`、`n_after`（`-1..20`，默认 -1）。

**200**：

```jsonc
{
  "alarm": { /* 同 /task/{id}/alarms 的告警对象：alarm_id, step_id, alarm_type, severity, message, detected_at(ms)… */ },
  "task_id": 123,
  "step_id": 10,
  "raw_clips": [
    { "url": "/media/segment/{token}", "filename": "raw_segment_...mp4",
      "ts_us": 1751800000000000, "ts_ms": 1751800000000, "is_trigger": true }
  ],
  "processed_clips": [ /* 同结构 */ ]
}
```

**错误**：`404`（告警不存在 / 无 `step_id`）、`503`（DB 不可用）。

---

## GET /traceback/task/{task_id}/playlist.m3u8

某 (task, step, track) 的 VOD 播放列表。

**路径**：`task_id`（int）。**查询**：`step_id`（int，必填）、`track`（默认 processed）。

**200**：`Content-Type: application/vnd.apple.mpegurl`，HLS VOD m3u8 文本——含 `#EXT-X-PLAYLIST-TYPE:VOD`、`#EXT-X-ENDLIST`、`#EXT-X-MAP:URI="/media/init/{token}"`，每段 `#EXTINF` + token 化 `/media/segment/{token}` URL。仅纳入写入侧 playlist 中已有 EXTINF 的段（过滤在途段）。

**错误**：`404`（无段）、`503`（缺 `init.mp4`，历史段需转码）。

---

## GET /traceback/alarm/{alarm_id}/playlist.m3u8

同上，但以告警触发段 ± 上下文为范围。

**路径**：`alarm_id`（int）。**查询**：`track`、`n_before`、`n_after`。
**200**：同 task playlist 的 m3u8 格式。**错误**：`404`（告警不存在 / 无 step_id / 无段）、`503`（缺 init）。

---

## GET /traceback/task/{task_id}/timeline

某 step 的视频起止 + 告警事件时间轴。

**路径**：`task_id`（int）。**查询**：`step_id`（int，必填）。

**200**：

```jsonc
{
  "task_id": 123, "step_id": 10,
  "start_ms": 1751800000000, "end_ms": 1751800060000, "duration_ms": 60000,
  "events": [
    { "ts_ms": 1751800015000, "type": "alarm", "alarm_id": 1001,
      "alarm_type": "流程违规", "severity": "high", "step_id": 10, "step_name": "泄漏检测", "message": "..." }
  ]
}
```

**降级**：DB 不可用时仍返回起止/时长（来自磁盘段），`events` 为空数组，**不报 503**（DB 恢复后自动恢复事件）。
