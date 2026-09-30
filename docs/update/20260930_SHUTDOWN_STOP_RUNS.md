# 进程停机前逐个 stop_run：交出 HLS 残段与剩余检测结果

> **变更状态**：生效中（2026-09-30）
> **知识库**：已沉淀 → [SERVICE_RUN_CONTROL.md](../kb/SERVICE_RUN_CONTROL.md)、[ARCHITECTURE_OVERVIEW.md](../kb/ARCHITECTURE_OVERVIEW.md)（2026-09-30）

## 概述

新增 `run_control.lifespan()`，嵌在 `inference.lifespan` 里层；停机时对每个在跑的 run 调 `stop_run`。每个任务不足一段的残帧（≤ 约 10 s 录像）与最后约 1 s 检测结果不再在停机时丢失。

## 变更背景

- **现状**：停机路径只有 `inference.stop()`——停 actor、落结算告警，但不经 `run_control.stop_run`，所以 `recording.flush_residual(cq)` 从未被调。cq 里凑不满一段的 raw / processed 帧和 `ca_detections` 落盘缓冲随进程退出丢弃；全仓只有 `api` 与 `health_monitor` 调 `stop_run`。
- **文档不符**：`main.py` 注释、`recording/__init__.py` 与 `alarm/__init__.py` 的 lifespan docstring 都写「`inference.stop()` 会经 run_control 交出最后一批 HLS 残段」，代码并不如此。
- **触发来源**：2026-09-30 KB 融合时核出；人决定「没有副作用的话，停机前 stop_run 一下」。

## 方案详情

### 全景：停机顺序（lifespan 逆序退出）

```text
run_control.lifespan 退出（最里层，最先）
  └─ RunControlService.shutdown()
       for task_id in client_service.snapshot():
         stop_run(task_id, "shutdown")
           封闸 DRAINING → 停 decoder（异步 kill）→ stop_workflow 收 settlement → alarm_sink 落结算告警
           → recording.flush_residual(cq)（残段 + 剩余 detections 入队）→ 注销 CQ
inference.lifespan 退出   inference.stop()：actor 已被摘空，Phase 2 只作兜底
recording.lifespan 退出   停 sweeper、排空队列（残段在这里落盘）
alarm / cleanup / stream / health_monitor 依次退出（stream.shutdown 收掉不属于任何 run 的 decoder）
```

硬约束：拆 run 必须在 `inference.stop()` 之前（actor 还在，settlement 走 stop_run 一条路），且 recording / alarm 队列仍活着（二者都在 inference 外层）。

| 部件 | 落在哪 |
|------|--------|
| `shutdown()` | [`app/services/run_control/service.py`](../../app/services/run_control/service.py) |
| `lifespan()` | [`app/services/run_control/__init__.py`](../../app/services/run_control/__init__.py) |
| 嵌套位置 | [`app/main.py`](../../app/main.py) `inference.lifespan` 里层、`yield` 外层（`shutdown_event.set()` 仍最先执行） |

### decoder 照走 stop_run 的异步停

`stop_stream` 把 decoder 从字典摘走后交给 daemon 线程 kill，之后的 `stream.shutdown()` 看不到它，只能靠该线程。
不另加同步停：`decoder.stop()` 开头即 SIGKILL（`decoder.lock` 只在 start / stop / is_alive 短暂持有，读循环不占），
毫秒级完成，而其后还有 `inference.stop()`、recording 排空等步骤；最坏情况父进程先退，ffmpeg 的 stdout 管道
读端关闭，下一次写即 SIGPIPE 自行退出。与 `/terminate` 走的是同一条路径。
run_control 只拆 run，不碰 stream 服务自身的收尾（归 `stream.lifespan`）。

### 副作用核对

| 动作 | 原停机路径 | 现在 |
|------|-----------|------|
| 结算告警上报 | `inference.stop()` Phase 2 上报 | 改由 `stop_run` 上报，actor 已摘走，Phase 2 为空——不重复 |
| HLS 残段 / 剩余 detections | 丢弃 | 入 recording 队列，随其 `stop()` 排空落盘 |
| ffmpeg 子进程 | `stream.shutdown()` 同步 kill | 随 `stop_run` 异步 kill（同 `/terminate`） |
| CQ 注销 + `cq.clear()` | 随进程退出 | 显式注销 |
| DB / 外部平台 | 不写 | 不写（`stop_run` 不碰 DB） |

### 顺带改的注释

`main.py` 起停顺序注释补第 7 层；`alarm/__init__.py`、`recording/__init__.py` lifespan docstring 与 `inference/online/service.py::stop` 的 Phase 2 注释改为现状；删掉 `stop()` 里「停机时检测结果能否被拉走取决于时序——已接受」一段；`run_control/instance.py` 引用面补「本包 `lifespan()`」。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 停机时每个在跑任务的残帧 | 丢弃（≤ 约 10 s） | 落盘为最后一段 |
| 停机时 cq 里剩余检测结果 | 视时序可能丢 | 全部落 `detections.jsonl` |
| 停机日志 | 无 per-run 拆除 | 每个任务一条 `stop_run(reason='shutdown') completed` |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_run_control_shutdown.py`（新增：逐 run 停 decoder、收 settlement、flush 并注销；不调 `stream.shutdown`） | 1 passed |
| `tests/test_import_hygiene.py`、`tests/test_start_rollback.py` | passed |
| 全量 `pytest tests/` | 981 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 未在真实 RTSP 流上跑过停机 | 残段实际落盘未经端到端验证 | 下次 dev 环境跑任务时 Ctrl-C，核对 run 目录最后一段与 `detections.jsonl` 尾部时间 |
| 停机耗时变长 | 每个任务多一次 flush（入队，非阻塞）+ recording 排空（已有 10 s 上限） | 观察停机日志；任务数量级小，预期可忽略 |
| KB 仍写「停机不 flush 残段」为已知缺口（ARCHITECTURE_API_SURFACE / ARCHITECTURE_DATA_FLOW / DESIGN_FAULT_TOLERANCE 等） | KB 与代码不符 | 下次 KB 融合按本记录改写 |
