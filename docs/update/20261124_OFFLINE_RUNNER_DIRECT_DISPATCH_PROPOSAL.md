# 离线作业服务直接调度 Worker 提案

> **变更状态**：提案（2026-11-24；2026-11-28 按当天落地的分割流程更新现状，执行类改名 `OfflineWorker`）

`OfflineJobService` 为每个作业启动独立子进程，直接调用 `OfflineWorker.run(spec)`。服务通过进程间通道接收结构化结果。CLI 保留为人工测试、排障和查询入口，和服务共用 Worker。

## 现状

- **提交。** `OfflineJobService` 把指定 `RunIdentity` 的作业放入串行队列。 [提交与状态管理](../../app/services/inference/offline/service.py#L154)
- **执行。** 服务拼接 `python -m ...offline.cli run` 及参数。CLI 先钉卡和限线程，再调用 `OfflineWorker.run()`，由它完成分割、落盘和规则判定。 [子进程命令](../../app/services/inference/offline/service.py#L316) · [CLI 执行入口](../../app/services/inference/offline/cli.py#L54)
- **结果传递。** 服务从子进程 stdout 末行解析 JSON，再更新作业状态。参数和结果在服务与 CLI 之间各转换一次。 [结果解析](../../app/services/inference/offline/service.py#L344)

## 服务和 CLI 调用同一个 Worker

```text
后台作业  OfflineJobService → 串行队列 → 单次子进程 → OfflineWorker.run(spec)
人工测试  CLI → 参数解析与运行环境初始化             → OfflineWorker.run(spec)
```

### JobService 负责调度

服务继续用现有队列一次处理一个作业。建议采用仓库已使用的 `multiprocessing` spawn 模式，以 Python 函数作为子进程入口。每个作业传入明确的 `(task_id, step_id, run_id)` 和运行设置，子进程从该输入构造 `OfflineRunSpec`。 [仓库现有 spawn 调度](../../app/services/inference/online/detection/infer_proxy.py#L145)

### 子进程直接执行 Worker

子进程入口只初始化运行环境、调用 `OfflineWorker.run(spec)` 并回传结果。完整作业仍是**同一个 run 的分割、落盘和规则判定**。`run()` 已覆盖这条流程，见[离线分析统一推理入口与作业编排](20261124_OFFLINE_ANALYSIS_PIPELINE_REFACTOR.md)。 [当前 Worker](../../app/services/inference/offline/worker.py#L78) · [当前 CLI](../../app/services/inference/offline/cli.py#L54)

### CLI 保留人工使用方式

`run`、`check`、`query` 和 `materials` 沿用现有命令行参数、输出及退出码。`run` 调用同一个 Worker，方便本地复现、测试和排障。 [现有命令行契约](../../app/services/inference/offline/cli.py)

## 保留现有子进程隔离

- **CUDA。** 子进程在首次导入 Torch 或策略模型前，设置与现有作业相同的 `CUDA_VISIBLE_DEVICES`。验证 spawn 入口的导入链满足这个顺序。 [仓库现有钉卡顺序](../../app/services/inference/online/detection/stage_worker.py#L179)
- **资源。** 在子进程内初始化线程数和平台相应的低优先级，继续使用相同的推理配置和存储根。人工 `run` 可复用运行环境初始化。 [当前线程设置](../../app/services/inference/offline/cli.py#L44) · [当前优先级与环境](../../app/services/inference/offline/service.py#L316)
- **终止。** JobService 保留超时、取消、停机和进程回收。服务负责管理进程句柄，终止原因仍按现有规则优先于子进程返回值。 [当前终止机制](../../app/services/inference/offline/service.py#L261)

## 使用结构化结果更新作业

- **结果通道。** 每个作业用一个单向通道返回一次结果。成功结果包含 `status`、`producer`、`segment_count`、`alarm_count` 和 `message`。Worker 异常返回可读的失败信息，诊断日志仍走日志通道。
- **状态收口。** 服务等到有效结果和正常退出，再登记 Worker 返回的 `completed`、`skipped` 或 `reclaimed`。进程异常退出或缺失结果记为 `failed`。取消和超时沿用当前终态。 [当前状态映射](../../app/services/inference/offline/service.py#L344)
- **判定失败。** 分割提交成功后的规则判定异常保持原语义，分割作业登记 `completed`，判定失败写入 `message`。 [当前判定异常处理](../../app/services/inference/offline/worker.py#L106)

## 迁移与验收

1. **验证 Worker 入口。** 用同一个 run 对照分割输出、规则判定输出和返回状态，确认直接调用覆盖现有后台作业行为。
2. **验证子进程。** 用真实 spawn 跑轻量策略，确认传入指定 `run_id`、使用正确的配置和存储根，并在首次 Torch 导入前完成 CUDA 设置。在部署平台核对线程数与进程优先级。
3. **切换后台执行。** 将现有启动命令和 stdout 解析替换为子进程调用与结构化结果通道。覆盖排队取消、运行中取消、超时、停机、启动失败和子进程退出未回结果。 [现有作业服务测试](../../tests/test_offline_job_service.py)
4. **验证入口兼容。** 核对后台状态接口字段、落盘产物，以及手动 CLI 的参数、JSON 和退出码。
