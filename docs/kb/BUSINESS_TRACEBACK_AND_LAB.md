> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 追溯与 Lab 送标

追溯和 Lab 共用 HLS 落盘结果，定位键是 `(task_id, step_id, 可选 run_id)`：回放、时间轴、送标、导出的单位都是一个 run，不带 `run_id` 时取该 step 最新可见 run。实现见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md) 与 [SERVICE_LAB.md](SERVICE_LAB.md)。

## 播放器里定位用媒体坐标，要保存的时刻落墙钟

同一条轨上并存墙钟（段文件名里的采集时刻，断流有宽度）与媒体坐标（Σ EXTINF，即 `<video>.currentTime`，断流宽度为零），混用会静默算错；换算要读清单，只有后端做得了。定义见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「两套坐标：墙钟与媒体轴」。

- 进度条、告警标记、seek、裁剪区间一律用媒体坐标。
- 离开本次请求还要保存的时刻（审计、落库、素材命名）一律用墙钟——媒体刻度依赖当前段集合，段增删后同一刻度指向另一帧。

## 告警定位回放由 playlist + timeline 两个端点组合完成

后端没有按 `alarm_id` 取证据或解析位置的端点。流程：

1. 从告警来源（`GET /task/{task_id}/alarms` 或实时推送）拿到 `(task_id, step_id, detected_at)`，链路不查 `source_ip`。
2. `GET /traceback/task/{task_id}/playlist.m3u8?step_id=&track=[&run_id=]` 播放该 step 的一个 run。
3. `GET /traceback/task/{task_id}/timeline?step_id=&track=[&run_id=]` 返回该 run 存续期 `[run_id, 下一个 run_id)` 内的告警，每条带墙钟 `ts_ms` 与媒体刻度 `media_offset_ms`，前端按后者落标记、跳转。
4. 媒体 URL 都是 HMAC token，锁定签发时解析的 run，不暴露文件系统路径。

DB 告警没有 run 维度，靠 run 存续区间归属。同 step 重跑后旧 run 仍可点名回放，直到随 step 被 TTL 回收。

## 回放与时间轴只覆盖一个 step 的一个 run

`step_id` 必填，不跨 step、不跨 run 聚合（两次之间可隔任意长时间，聚合区间对应不到可播放的东西）。

- 段与时长只认写入侧清单的 EXTINF，在途段天然被滤掉。
- timeline 的 `duration_ms`（双轨并集墙钟跨度）与 `media_duration_ms`（单轨 Σ EXTINF）不同尺；断流总时长只能用后端给的 `gap_total_ms`，不能拿两者相减。
- DB 不可用时 timeline 退化为空事件、仍返回时长，不 503。
- 点名的 run 不存在（写错或已被 TTL 回收）→ 404；不点名且无可见 run → playlist 404、timeline 全 0。

## Lab 送标：从 raw 轨裁媒体区间，逐段推给 Label Studio

操作员在工作台（`/ui-f3m8/lab/`）从任务列表选定一个 run（列表给出每个 step 的 `run_id`，后续请求带上即锁定），在 raw 轨上选多个不重叠的媒体区间提交；后端换算墙钟、用 ffmpeg 裁成 mp4，逐段上传 Label Studio。admin 页离线 tab 复用 Lab 的任务列表与 `/label-probs`。

- 只裁 raw 轨：标注要原始画面。processed 可切换查看，但不可打点。
- 跨越真实录制停顿（相邻段墙钟间隙 > 0.5 s）的区间拒裁（`range_gap`）。
- 单段时长、单次总时长、单次 clip 数各有上限（`settings.lab_export_max_*`）。
- 单段失败不影响其他段：HTTP 仍 200，每段带 `success` / `error_code` 与墙钟区间。
- clip 不带元数据（LS 忽略 multipart 非文件字段），素材侧溯源只剩文件名里的墙钟区间。后端职责止于推 clip，不负责导出标注或转训练集。
- LS url 与默认 project_id 可在页面改并持久化；token 只来自 env。
- 任务列表可切 `task_source=storage`，完全不碰业务库，DB 故障时送标不受牵连。
- `/label-probs` 返回离线分割模型的逐帧类别概率（媒体刻度），只供可视化，不参与任何判断。

## 代码来源

- `app/routers/traceback.py`、`app/routers/media.py`、`app/routers/lab.py`
- `app/routers/utils/runs.py`、`app/routers/utils/media_token.py`
- `app/storage/hls/`、`app/storage/runs.py`、`app/services/utils/media_timeline.py`
- `app/services/lab/`、`app/db/alarms.py`
