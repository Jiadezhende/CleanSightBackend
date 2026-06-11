> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 任务生命周期

任务生命周期由统一 API、AI 推理服务、流服务、健康监控和客户端状态共同维护。

## 启动流程

入口：`POST /api/start`

请求字段：

- `task_id`
- `rtsp_url`
- `fps`，默认 30

主要步骤：

1. API 层从数据库 `clean_task` 查询 `task_id`。
2. 校验任务存在，且 `source_ip` 非空。
3. 使用 `source_ip` 作为 `client_id`。
4. 获取 per-client 异步锁，串行化同一 client 的 start/terminate。
5. 若同一 client 已存在任务，判断是否完全相同。
6. 完全相同则幂等返回；task、step 或 URL 任一变化则先完整清理旧 client。
7. 构造运行时 `Task`，调用 `ai.set_task()`。
8. 调用 `stream_service.start_stream()` 启动解码。

## 幂等条件

同一 client 已运行时，只有以下条件全部相同才幂等返回：

- 旧 `task_id == req.task_id`
- 旧 `current_step == db_task.current_step`
- 旧流 URL 等于请求 `rtsp_url`

否则执行全量重建，避免跨任务、跨步骤或换流后的状态残留。

## 终止流程

入口：`POST /api/terminate?client_id=...`

终止同样使用 per-client 异步锁。优先委托 `GlobalHealthMonitor.cleanup_client()`，健康监控未初始化时走 API fallback。

统一清理顺序：

1. 停止 StreamService 中的 decoder。
2. 调用 InferenceManager 移除推理资源、触发结算告警、落盘残余 HLS 段。
3. 从 ClientManager 移除客户端状态。

清理采用尽力而为策略，单步失败会记录错误但继续后续步骤。

## 任务切换

任务切换发生在同一 `client_id` 再次 start 且 task、step 或 URL 变化时。当前设计会先清理旧任务，再绑定新任务，关键原因是：

- TemporalActor 的结算告警必须归属旧任务。
- ClientQueues 中的滑动窗口、最新推理结果、告警 gate 需要清空。
- 解码器和 HLS 残余段需要安全收尾。

## 代码来源

- `app/routers/api.py`
- `app/services/ai.py`
- `app/services/inference/core/manager.py`
- `app/services/health_monitor/monitor.py`
- `app/services/client/manager.py`
- `tests/test_api_concurrency.py`

