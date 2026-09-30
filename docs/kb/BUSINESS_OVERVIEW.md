> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 业务总览

CleanSight Backend 是一个用于内镜人工清洗流程的 AI 视觉巡检后端。当前代码围绕实时视频流接入、AI 检测、告警上报、视频追溯和送标展开，另附一项独立的试纸比色能力。

## 核心业务对象

- 任务：业务主键为 `task_id`（int），数据库表为 `clean_task`，ORM 在 `app/db/tasks.py`（`DBTask`，后端只读，经 `db_tasks.query_*` 查询）。**`task_id` 即运行键**（注册表/decoder/Actor/落盘分区全链路 int）。
- 客户端来源：`clean_task.source_ip` 为**被动**身份字段（诊断 + 遗留 wire 适配），不再作路由键。
- 步骤：`current_step` 字符串，`RunControlService` 边界 `int()` 转 step_id（非数字 → 400），`InferenceService.resolve_stage` 恒等路由 `"1"`→LEAK、`"2"`→CLEAN；YAML 未定义或无在线检测的 step → `/api/start` 400（无兜底 stage），且校验在动旧 run 之前。
- 运行（run）：同一步骤每次开跑分配一个 run（`run_id` = 分配时刻 epoch 毫秒），产物与回放都以 run 为单位。
- 告警：数据库表 `clean_alarm`，运行时告警由 Operator（时序判定）产生，过闸编排在 `inference/online/temporal/alarm_sink`，alarm 服务（`app/services/alarm/`）无状态上报外部接口。
- 证据：HLS 视频段按 `{storage_base_dir}/{task_id}/{step_id}/{run_id}/hls/` 存储（run 下按域分目录，检测结果在同 run 的 `inference/`）。

## 当前业务能力

- 启动任务并拉取 RTSP 流：`POST /api/start`。
- 实时推理视频：`WebSocket /ai/video?task_id=...`（旧 `?client_id=` 双模兼容）。
- 实时前端消息：`GET /task/message/{task_id}`。
- 历史告警查询：`GET /task/{task_id}/alarms`。
- 单步骤 VOD 回放：`GET /traceback/task/{task_id}/playlist.m3u8?step_id=...[&run_id=...]`（缺省最新可见 run）。
- 回放时间轴与告警打点：`GET /traceback/task/{task_id}/timeline?step_id=...[&run_id=...]`（告警定位回放由这两个端点组合完成，按 `alarm_id` 取证据的专用入口已删除）。
- Lab 送标：`POST /lab-f3m8/submit` 按媒体区间从 raw 轨裁剪视频并提交 Label Studio。
- 离线动作分割（CLEAN）：run 结束后由 admin 手动提交离线作业，结果经 `POST /ai/temporal`（分段）与 `POST /lab-f3m8/label-probs`（逐帧概率）回看；不判合规、不产告警。
- 试纸比色：`POST /algorithm/colorstrip` 上传一张同时拍到瓶身参考色卡与试纸的照片，返回合格 / 不合格；判不出来时返回结构化失败码与补拍提示（见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)）。

## 业务边界

- 当前核心检测阶段是 `LEAK`，包含气泡检测和弯折动作检测。
- `CLEAN` 阶段有两个 detector（clean_large/clean_small）+ 在线时序算子 `clean_monitor`（动作识别，当前仅出叠字、不产告警）+ 离线段 `CleanNodepGRUSegmenter`（整段动作分割，手动触发）。
- 追溯按 `task_id + step_id`（+ 可选 `run_id`）定位，不再依赖 `source_ip`，因为注释明确说明该字段可能被业务侧覆写。
- Lab 只使用 raw 轨送标，不使用 processed 轨。
- 试纸比色独立于内镜视频巡检主流程：不关联 task / step，不落库、不告警、不留证据；定位是防误操作与留痕，不是精密定量，也不防蓄意伪造。

## 代码来源

- `app/main.py`
- `app/routers/api.py`
- `app/db/tasks.py`
- `app/services/run_control/service.py`
- `app/services/inference/online/service.py`（`resolve_stage`）
- `app/routers/traceback.py`
- `app/routers/lab.py`
- `app/routers/algorithm.py`、`app/services/algorithm/service.py`
- `config/inference_config.yaml`
