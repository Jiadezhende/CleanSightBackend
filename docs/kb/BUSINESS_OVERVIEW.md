> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 业务总览

CleanSight Backend 是内镜人工清洗流程的 AI 视觉巡检后端：接入实时 RTSP 流做检测、上报告警、留存录像供追溯与送标。另附一项与主流程无关的试纸比色能力。

## 核心业务对象

| 对象 | 定义 |
|------|------|
| 任务 | `task_id`（int），平台表 `clean_task`，后端只读（`app/db/tasks.py`）。`task_id` 即全链路运行键 |
| 步骤 | `clean_task.current_step` 字符串，在 `RunControlService` 边界转成 int `step_id`，直接作推理 stage 键：`"1"`（alias LEAK）、`"2"`（alias CLEAN）。未配置或无在线检测的 step 由 `/api/start` 拒绝（400），无兜底 stage |
| 运行（run） | 同一步骤每开跑一次就是一个 run（`run_id` = 分配时刻 epoch 毫秒），是录像、回放、送标的单位，见 [BUSINESS_TASK_LIFECYCLE.md](BUSINESS_TASK_LIFECYCLE.md) |
| 告警 | 时序算子（Operator）判定产生，经 alarm 服务上报外部平台落 `clean_alarm`；后端只读回该表 |
| 证据 | 每个 run 的 HLS 录像（raw / processed 两轨）与检测结果，落 `{storage_base_dir}/{task_id}/{step_id}/{run_id}/{hls,inference}/` |
| 点位 | `clean_task.source_ip`，被动身份字段，不是运行键；只用于 WS 点位模式与 `/api/terminate?client_id=` 按点位反查当前 run（多个命中取最晚启动者） |

## 业务能力

- 起停任务：`POST /api/start`、`POST /api/terminate`。
- 实时画面：`WS /ai/video`，`?task_id=` 锁定一次 run，`?client_id=<source_ip>` 跟随点位当前 run——两种并列模式，不是新旧兼容。
- 实时消息与清单：`GET /task/message/{task_id}`、`GET /task/live`、`GET /task/history`、`GET /task/{task_id}/alarms`。
- 回放与告警打点：`/traceback/task/{task_id}/playlist.m3u8`、`/timeline`，见 [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md)。
- Lab 送标：`POST /lab-f3m8/submit`，从 raw 轨裁剪媒体区间推给 Label Studio。
- 离线动作分割（CLEAN）：admin 手动提交离线作业，结果经 `POST /ai/temporal`（分段）与 `POST /lab-f3m8/label-probs`（逐帧概率）回看。
- 试纸比色：`POST /algorithm/colorstrip`，见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)。

## 业务边界

- 只有 `LEAK` 阶段产告警（气泡检测 + 弯折动作检测）。`CLEAN` 的在线时序算子 `clean_monitor` 只做动作识别叠字；离线整段动作分割 `CleanNodepGRUSegmenter` 手动触发，不判合规、不产告警。
- 追溯只按 `task_id + step_id`（+ 可选 `run_id`）定位，不用 `source_ip`（该字段可能被业务侧覆写）。
- Lab 只从 raw 轨送标，后端职责止于把 clip 推给 Label Studio，不负责导出标注或转训练集。
- 试纸比色不关联 task / step，不落库、不告警、不留证据；定位是防误操作与留痕，不是精密定量，也不防蓄意伪造。

## 代码来源

- `app/routers/{api,ai,task,traceback,lab,algorithm}.py`
- `app/db/tasks.py`、`app/db/alarms.py`
- `app/services/run_control/service.py`、`app/services/inference/online/service.py`（`resolve_stage`）
- `config/inference_config.yaml`
