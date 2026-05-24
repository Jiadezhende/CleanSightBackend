> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 追溯与 Lab 送标

追溯和 Lab 共用 HLS 落盘结果，核心定位键是 `task_id + step_id`。

## 告警证据回溯

入口：

- `GET /traceback/alarm/{alarm_id}/evidence`
- `GET /traceback/alarm/{alarm_id}/playlist.m3u8`

流程：

1. 从 `clean_alarm` 查询告警。
2. 读取告警自带的 `task_id`、`step_id`、`detected_at`。
3. 将 `detected_at` 归一化为毫秒。
4. 使用 `SegmentFinder` 在 `{base_dir}/{task_id}/{step_id}` 定位 raw/processed 段。
5. 按触发段前后上下文返回片段 URL 或 VOD playlist。
6. 媒体 URL 使用 HMAC token，不暴露文件系统路径。

默认上下文段数量来自 settings：

- `traceback_context_before`
- `traceback_context_after`

## 任务回放与时间轴

入口：

- `GET /traceback/task/{task_id}/playlist.m3u8?step_id=...&track=processed`
- `GET /traceback/task/{task_id}/timeline?step_id=...`

当前代码要求 `step_id` 必填，只返回单个洗消步骤的数据，不做跨 step 聚合。

时间轴事件来自 `clean_alarm`，视频时长来自 HLS playlist 中的 EXTINF，而不是文件名时间戳估算。

## Lab 送标

入口：

- `POST /lab-f3m8/submit`
- `GET /lab-f3m8/health`
- `GET /lab-f3m8/config`
- `PUT /lab-f3m8/config`

Lab 只使用 raw 轨。操作员选择一个 step 内的多个不重叠 `[start_ms, end_ms]` 区间，后端通过 ffmpeg 裁剪成 mp4，并逐段上传到 Label Studio。

当前约束：

- 单段最大时长：`lab_export_max_clip_ms`
- 一次提交总时长：`lab_export_max_total_ms`
- 一次最多 clip 数：`lab_export_max_clips_per_submit`
- URL 和默认 project_id 可通过 Lab config 持久化；token 只能来自 env。

## 代码来源

- `app/routers/traceback.py`
- `app/routers/media.py`
- `app/services/traceback/segment_finder.py`
- `app/services/traceback/media_token.py`
- `app/routers/lab.py`
- `app/services/lab/clip_builder.py`
- `app/services/lab/label_studio_client.py`

