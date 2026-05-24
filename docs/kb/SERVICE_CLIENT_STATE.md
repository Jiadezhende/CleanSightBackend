> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Client State Service

客户端状态服务围绕 `ClientManager` 和 `ClientQueues` 展开，是流、推理、可视化、告警、追溯落盘之间的共享状态层。

## ClientManager

`ClientManager` 负责：

- 创建和获取 `ClientQueues`。
- 判断 client 是否存在。
- 维护 `task_id -> client_id` 映射。
- 按 task_id 查找当前 client。
- 移除 client 并可选清理队列。

## ClientQueues 队列

主要队列和槽位：

- `ca_ready`：待推理原始帧，SPSC deque。
- `ca_raw`：raw HLS 落盘帧。
- `ca_processed`：processed HLS 落盘帧。
- `_latest_rendered`：最新渲染帧，供 WebSocket。
- `_latest_inference`：最新推理结果快照，供可视化。
- `_slide_window`：每个 task/model 的检测输出滑动窗口，供时序分析。
- `_latest_temporal`：最新时序事件，供前端 overlay 和消息接口。
- `_alarm_log`：内存告警日志，供 `/task/message/{task_id}`。

## 任务绑定

`InferenceManager.set_task()` 调用 `cq.set_task()` 并设置 stage。任务变化时清理 task 级缓存，避免旧任务状态污染新任务。

## 告警 Gate

`try_pass_alarm_gate(task_id, metric, mode)` 对同一 task、metric、mode 做固定 5 秒冷却。被拦截的告警不会刷新窗口。

测试覆盖：

- 第二次窗口内告警被拦截。
- 不同 mode 或 metric 独立。
- 切换任务后 gate 清空。

## 前端消息

`/task/message/{task_id}` 通过 `client_manager.get_client_by_task_id()` 找到活跃 client，并返回增量告警与 `signals_10s`。

## 代码来源

- `app/services/client/manager.py`
- `app/services/client/queues.py`
- `app/services/client/config.py`
- `app/routers/task.py`
- `tests/test_alarm_increment.py`
- `tests/test_task_message_api.py`

