> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 任务生命周期

一次 run 的起停全部经 `RunControlService.start_run` / `stop_run`，运行键是 int `task_id`。本文件只写业务流程与规则；各步 owner、加锁与失败处理见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## run 是回放、追溯、送标、离线分析的单位

- 同一 `(task_id, step_id)` 每开跑一次就是一个新 run，盘上一个目录 `{task}/{step}/{run_id}/`。`run_id` = 开跑时刻 epoch 毫秒，同 step 内严格递增；身份 `RunIdentity`（`app/types/run.py`）。
- 一个 CQ 对应一个 run，CQ 构造时带上 `RunIdentity`，此后不变。
- 读侧缺省取该 step 最新可见 run，带 `run_id` 可点名旧 run；timeline 告警按 run 存续期 `[run_id, 下一个 run_id)` 归属。
- 离线分析锁定一个 run：对正在运行的 run 提交返回 409（输入还在写）。

## 启动：参数校验在动旧 run 之前

入口 `POST /api/start`，body `{ task_id, rtsp_url }`（历史字段 `fps` 已弃用，带上无害）。

1. API 层查 `clean_task`：任务不存在 → 404，`source_ip` 为空 → 400，DB 失败 → 503。
2. 经 `asyncio.to_thread` 调 `start_run`。`current_step` 非数字、未配置或无在线检测 → 400；此校验在锁外、动旧 run 之前，失败时已在跑的 run 不受影响。
3. 持 `lock_for(task_id)`：幂等判断 → 分配 run 目录 → 建并注册 CQ → 建推理 Actor → 起解码。注册后任一步失败即 `stop_run(expected=cq)` 回滚并重抛。

## 同 task 再次 start：step 与 URL 都没变才幂等

任一变化（改 step / 换流）→ 先 `stop_run` 停旧，再全量建新 run，不复用旧对象。换代的隔离：

- 内存侧：旧 run 的结算告警归属旧 CQ；旧 run 迟到的写入撞 DRAINING / CLOSED 状态门被拒，不串到新 run。
- 盘上：一 run 一目录，旧 run 迟到的落盘写进它自己的目录；换代不清旧产物，旧 run 保留到 step TTL。
- 回放：缺省指向最新可见 run；新 run 刚起时有一段缺省回放为空的窗口，见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)「读侧 run 锁定」。

## 四条路径结束一次 run，全部走 stop_run

| 触发 | 入口 |
|------|------|
| 前端终止 | `POST /api/terminate`：body `{ task_id }`（首选）或 query `?client_id=<source_ip>`（旧入口）；查不到 run 时 success no-op |
| 健康监控 | 重连无帧超时 / 孤儿 / 任务超时，经 `cleanup_client` 调 `stop_run`，传 `expected` CQ 做身份 fence |
| 启动回滚 | `start_run` 注册后的 setup 步失败 |
| 进程停机 | `run_control.lifespan` 退出时 `shutdown()` 对每个在跑的 run 调 `stop_run(reason="shutdown")`；嵌在 `inference.lifespan` 里层，保证早于 `inference.stop()`、recording / alarm 队列仍活着 |

`stop_run` 尽力而为、永不抛出，固定顺序：封闸 `to_draining()` → 停 decoder → 停 Actor 并上报结算告警 → `flush_residual(cq)` 交出 HLS 残段与剩余检测结果 → 注销 CQ（`cq.close()` 释放帧，故 flush 必须在它之前）。四条路径都会把每个 run 最后不足一段的录像与检测结果交给 recording 落盘。

## 代码来源

- `app/routers/api.py`、`app/db/tasks.py`
- `app/services/run_control/service.py`（`start_run` / `stop_run` / `shutdown`）、`app/services/run_control/__init__.py`（`lifespan`）、`app/main.py`（lifespan 嵌套）
- `app/storage/runs.py`、`app/types/run.py`
- `app/services/recording/service.py`（`flush_residual`）、`app/daemons/health_monitor/worker.py`（`cleanup_client`）
- `app/services/inference/offline/service.py`（运行中 run 提交 409）
- `tests/test_api_concurrency.py`、`tests/test_start_rollback.py`、`tests/test_teardown_identity_fence.py`、`tests/test_run_control_shutdown.py`
