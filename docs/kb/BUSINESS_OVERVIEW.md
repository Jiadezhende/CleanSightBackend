> 更新时间：2026-07-21
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 业务总览

CleanSight Backend 是一个用于内镜人工清洗流程的 AI 视觉巡检后端。当前代码围绕实时视频流接入、AI 检测、告警上报、视频追溯和送标展开。

## 核心业务对象

- 任务：业务主键为 `task_id`（int），数据库表为 `clean_task`，ORM 在 `app/models.py`。**`task_id` 即运行键**（注册表/decoder/Actor/落盘分区全链路 int）。
- 客户端来源：`clean_task.source_ip` 为**被动**身份字段（诊断 + 遗留 wire 适配），不再作路由键。
- 步骤：`current_step` 字符串，`resolve_stage` 恒等路由 `"1"`→LEAK、`"2"`→CLEAN、未知→MOCK（RunController 边界 `int()` 转 step_id）。
- 告警：数据库表 `clean_alarm`，运行时告警由 Operator（时序判定）产生，过闸编排在 `inference/temporal/alarm_sink`，persistence 无状态落库上报外部接口。
- 证据：HLS 视频段按 `{storage_base_dir}/{task_id}/{step_id}/` 存储。

## 当前业务能力

- 启动任务并拉取 RTSP 流：`POST /api/start`。
- 实时推理视频：`WebSocket /ai/video?task_id=...`（旧 `?client_id=` 双模兼容）。
- 实时前端消息：`GET /task/message/{task_id}`。
- 历史告警查询：`GET /task/{task_id}/alarms`。
- 告警证据回溯：`GET /traceback/alarm/{alarm_id}/evidence` 和对应 playlist。
- 单步骤 VOD 回放：`GET /traceback/task/{task_id}/playlist.m3u8?step_id=...`。
- Lab 送标：`POST /lab-f3m8/submit` 从 raw 轨裁剪视频并提交 Label Studio。

## 业务边界

- 当前核心检测阶段是 `LEAK`，包含气泡检测和弯折动作检测。
- `CLEAN` 阶段有两个 detector（clean_large/clean_small）+ 在线时序算子 `clean_monitor`（动作识别，当前仅出叠字、不产告警）。
- 追溯按 `task_id + step_id` 定位，不再依赖 `source_ip`，因为注释明确说明该字段可能被业务侧覆写。
- Lab 只使用 raw 轨送标，不使用 processed 轨。

## 代码来源

- `app/main.py`
- `app/routers/api.py`
- `app/models.py`
- `app/services/inference/manager.py`
- `app/routers/traceback.py`
- `app/routers/lab.py`

