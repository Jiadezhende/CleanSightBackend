> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 追溯与 Lab 送标

追溯和 Lab 共用 HLS 落盘结果，核心定位键是 `task_id + step_id`。

## 两套时间坐标（两个板块的共同前提）

同一个 step 上并存两把尺，**混用即静默算错**：

```
墙钟   段文件名里的采集时刻。回答"这件事几点发生的"——审计、检索、落库、素材命名。
       断流那段时间在它上面有宽度。
媒体   Σ EXTINF，与 <video>.currentTime / duration 同源。回答"在播放器的第几秒"——
       进度条、告警标记、seek、裁剪。它是压紧的墙钟，断流在它上面宽度为零。
```

换算要读清单，**只有后端做得了**（`app/services/utils/media_timeline.py` 的 `MediaTimeline`），浏览器侧的 `首段墙钟 + currentTime` 只在从没断过流时成立。因此：**凡是要在播放器里定位的量一律走媒体坐标；凡是要离开本次请求被保存下来的时刻一律落墙钟**（媒体刻度依赖当前段集合，段增删后同一个刻度就指向另一帧）。

## 告警定位回放

按 `alarm_id` 一次取回证据的专用入口**已不存在**（原 `/traceback/alarm/{alarm_id}/evidence` 与配套 playlist 已删除）。同一件事现在由两个 step 级端点组合完成：

- `GET /traceback/task/{task_id}/playlist.m3u8?step_id=...&track=...`
- `GET /traceback/task/{task_id}/timeline?step_id=...&track=...`

流程：

1. 从告警来源（`GET /task/{task_id}/alarms` 或实时推送）拿到 `(task_id, step_id, detected_at)`——`clean_alarm` 自带这三项，**链路不查 `source_ip`**。
2. 用 `(task_id, step_id)` 拼 playlist 播放该 step。
3. timeline 返回该 step 的全部告警事件，每条同时带墙钟 `ts_ms` 与媒体刻度 `media_offset_ms`；前端按后者在进度条上落标记、跳转。
4. 媒体 URL 全部是 HMAC token，不暴露文件系统路径。

**能力边界**：调用方必须自带 `(task_id, step_id)`；后端没有 `alarm_id → 位置` 的解析端点。上下文段数（原 `traceback_context_before/after`）的概念一并消失——回放范围就是整个 step。

## 任务回放与时间轴

`step_id` 必填，只返回单个洗消步骤的数据，**不做跨 step 聚合**（两个 step 之间可以隔任意长时间，任务级聚合区间不对应任何可播放的东西）。

- 段与时长的唯一真源是写入侧 playlist 的 EXTINF（`app.storage.hls.list_segments`），不用文件名时间戳估算；在途段不在清单里，天然被滤掉。
- timeline 的 `duration_ms` 是 **raw/processed 双轨并集**的墙钟跨度，`media_duration_ms` 是**单轨** Σ EXTINF，两者不同尺——断流总时长必须由后端逐段算出（`gap_total_ms`），不能拿两者相减去凑。
- 时间轴事件来自 `clean_alarm`；DB 不可用时退化为空事件、仍返回时长，不 503。

## Lab 送标

入口：`POST /lab-f3m8/submit`、`GET /lab-f3m8/tasks`、`GET /lab-f3m8/download`、`GET /lab-f3m8/health`、`GET|PUT /lab-f3m8/config`。

Lab **只裁 raw 轨**（processed 是渲染结果，标注要原始画面；processed 可切换查看但不可打点）。操作员在一个 step 内选多个不重叠的媒体区间 `[start_media_ms, end_media_ms]`，后端换算出墙钟、用 ffmpeg 裁成 mp4，逐段上传到 Label Studio。

当前约束：

- 区间用**媒体坐标**提交，墙钟由后端换算后随响应带回（算不出时 null）。
- 跨越真实录制停顿的区间拒裁（`error_code=range_gap`）；判据 `gap = 下一段起点 − (本段起点 + EXTINF) > 0.5s`。
- 单段最大时长 `lab_export_max_clip_ms`、单次总时长 `lab_export_max_total_ms`、单次 clip 数 `lab_export_max_clips_per_submit`。
- 送标 clip **不带元数据**：LS 的 multipart 导入只读文件字段，非文件字段一律忽略，素材侧溯源只剩文件名里的墙钟区间。
- LS url 与默认 project_id 可经 Lab config 持久化；token 只能来自 env。
- 任务列表可切 `task_source=storage`，完全不碰业务库（DB 故障时送标不受牵连）。

## 代码来源

- `app/routers/traceback.py`、`app/routers/media.py`、`app/routers/lab.py`
- `app/services/traceback/media_token.py`
- `app/services/utils/media_timeline.py`、`app/services/utils/vod_playlist.py`
- `app/services/lab/clip_builder.py`、`step_exporter.py`、`label_studio_client.py`、`config.py`
- `app/storage/hls/`、`app/storage/tasks.py`
