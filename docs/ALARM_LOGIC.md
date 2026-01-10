# 告警去重与批量上报逻辑说明

本文档说明 `app/services/ai.py` 中报警（alarm）去重与批量上报的设计、数据结构、运行流程以及可配置项与测试方法，便于开发、排查与扩展。

**作者**: 清洁视觉后端 (代码实现位于 `app/services/ai.py`)

## 目标

- 避免对同一类告警在短时间内高频重复上报，减轻远端告警平台与运维告警噪声。
- 将短时间内重复的告警按任务/步骤聚合后批量上报，并记录出现次数与时间范围。
- 保持原有的远端上报与本地数据库记录能力（即最终仍调用原 `_handle_alarm` 完成上报与落库）。

## 主要数据结构

- `_pending_alarms: Dict[str, Dict]`
  - key: 默认为 `"{task_id}_{step_id}"`（可定制）。
  - value: 包括字段 `count`, `first_seen`, `last_seen`, `alarm_info`。
  - 用途：在内存中聚合同一 key 的告警事件。

- `_recent_alarms: Dict[str, float]`
  - key 同上。
  - value: 上一次成功上报（或提交）该 key 的时间戳。
  - 用途：实现冷却窗口（cooldown），防止频繁重复上报。

- `_alarm_lock: threading.Lock`
  - 用于保护以上两个字典的并发安全。

- `_alarm_thread`
  - 后台线程，周期性触发一次 flush（即把 pending 中合适的告警上报）。

- 配置参数（可通过 `app.config.settings` 设置）：
  - `alarm_batch_interval`：批量上报间隔 (秒)。默认 30s。
  - `alarm_cooldown_seconds`：去重冷却时长 (秒)。默认 60s。

## 触发与聚合流程

1. 推理模块在 `_execute_inference_pipeline` 中检测到异常（如下）：
   - 任一子任务返回 `success == False`；或
   - `motion` 类任务返回指示异常的 `actions`（如 bending_detected、bubble_detected、submersion_status 非正常值）。

2. 生成基础 `alarm_info` 字典：包含 `task_id`, `step_id`, `detection_result` 等。代码示例：

   {
     "task_id": 1,
     "step_id": 0,
     "detection_result": { ... }
   }

3. 将该 `alarm_info` 传入 `manager._enqueue_alarm(alarm_info)`：
   - 计算聚合 key（默认 `"{task_id}_{step_id}"`）。
   - 若该 key 不存在于 `_pending_alarms`，则创建包含 `count=1, first_seen=now, last_seen=now, alarm_info` 的条目。
   - 若已存在，则 `count += 1` 并更新 `last_seen`。
   - 此步骤非常轻量，仅在内存中更新字典（受 `_alarm_lock` 保护），不会阻塞推理主循环。

## 周期性 Flush（上报）逻辑

- 后台线程 `_alarm_flush_loop` 每隔 `alarm_batch_interval` 秒运行一次：
  1. 遍历 `_pending_alarms` 的 key 列表。
  2. 对于每个 pending 条目，检查 `_recent_alarms`：
     - 如果该 key 在 `_recent_alarms` 且现在距离上次上报时间小于 `alarm_cooldown_seconds`，则跳过（保持在 pending 中）。
     - 否则，把 pending 条目聚合为 `agg_alarm`，添加聚合字段 `alarm_count`, `first_seen`, `last_seen`（时间字符串），并将该条目从 `_pending_alarms` 中移除，同时更新 `_recent_alarms[key] = now`。
  3. 将每个 `agg_alarm` 提交给线程池去调用 `_handle_alarm(agg_alarm)`，由原有逻辑完成远端上报 `_send_alarm_report` 与本地 DB 写入 `_record_alarm_db`。

- 设计要点：实际的网络与 DB 写入发生在线程池线程中（不阻塞 flush loop 或推理主循环）。

## 聚合告警字段（发送前会加入）

聚合后的 `agg_alarm` 至少包含：
- 原始的 `alarm_info` 内容（task_id, step_id, detection_result 等）
- `alarm_count`: 在聚合窗口内出现的次数（int）
- `first_seen`: 第一次出现时间（YYYY-MM-DD HH:MM:SS）
- `last_seen`: 最后一次出现时间（YYYY-MM-DD HH:MM:SS）

建议：如果需要让外部平台收到聚合信息，可将上述 `alarm_count/first_seen/last_seen` 一并加入 `_send_alarm_report` 请求体（当前实现会把 `detection_result` 传给外部，`agg` 字段可按需添加）。

## 去重/冷却策略的可调节点

- `key` 的粒度：当前使用 `task_id` + `step_id`，意味着同一任务步骤内的不同异常会被合并。
  - 若希望按异常类型去重，可把 key 扩展为 `f"{task_id}_{step_id}_{alarm_type}"` 或基于 `detection_result` 计算哈希（示例：对 `detection_result` 的关键字段进行 JSON 序列化并取 SHA1）。
- `alarm_batch_interval`（批量间隔）：决定上报延迟与聚合窗口大小。
- `alarm_cooldown_seconds`（冷却）：决定同一 key 多久内不会重复上报。

## 紧急告警绕过（可选扩展）

- 对于 `alarm_level == 'critical'` 的事件，可以实现立即上报而不经过队列/冷却逻辑。
- 该改动建议在 `_enqueue_alarm` 或 `_execute_inference_pipeline` 中判断 `alarm_info` 的 `alarm_level` 字段并直接走 `_executor.submit(self._handle_alarm, alarm_info)`。

## 数据持久化与平台上报

- 远端上报：`_send_alarm_report` 调用配置在 `AI后端接口文档.md` 中的 URL（示例 `http://116.204.65.72:8881/gdmp/v1/api/nt/alarm_report`），请求头包含 `User-Agent: AI-Backend/1.0`。
- 本地落库：`_record_alarm_db` 会在数据库中创建 `alarm_record` 表（若不存在）并插入一条记录。当前实现使用 PostgreSQL 专用字段 `JSONB` 与 `SERIAL`。
  - 注意：若你使用的是非 PostgreSQL 数据库，需要把 DDL/语法调整为目标数据库兼容形式，或使用 SQLAlchemy ORM model + migration 管理表结构。

## 如何测试与排查

1. 单元/集成测试（模拟）：
   - 在 Python REPL 或测试脚本中导入 `app.services.ai` 的 `manager`，构造不同 `alarm_info` 并调用 `manager._enqueue_alarm(alarm_info)` 多次，观察日志（或通过调试断点）在下一次 flush 后是否发送聚合告警。

   示例（快速脚本片段）：

   ```python
   from app.services import ai
   ai.manager._enqueue_alarm({"task_id":1, "step_id":0, "detection_result":{"foo":1}})
   ai.manager._enqueue_alarm({"task_id":1, "step_id":0, "detection_result":{"foo":1}})
   # 等待 > alarm_batch_interval 秒，查看后端日志是否只有一次上报，且 payload 包含 alarm_count=2
   ```

2. 端到端测试：
   - 触发真实或合成的视频输入导致推理检测到异常，观察后台日志（`Alarm flush thread started`、`Alarm reported successfully` 等），并检查远端平台是否收到请求以及本地 `alarm_record` 是否写入。

3. 排查常见问题：
   - 如果不见上报，确认 `_alarm_thread` 是否在运行（查看启动日志或 `ps`）；确认 `alarm_batch_interval` 未被设为过大。
   - 如果多次重复上报，检查 `key` 的粒度是否过粗，或 `alarm_cooldown_seconds` 设置过小。
   - 如果 DB 写入失败，检查 `engine` 的配置（`settings.database_url`）、数据库类型（是否 PostgreSQL）以及异常日志。

## 可扩展方向（建议）

- 使用更稳健的去重 key（包含 `alarm_type` 或对 `detection_result` 做哈希），以避免不同异常被误合并。
- 将 `alarm_record` 表结构迁移到 ORM model + Alembic migration，避免运行时 DDL。
- 支持把聚合字段 `alarm_count/first_seen/last_seen` 作为标准字段上报给外部平台；并在外部平台上展示聚合历史。
- 在高并发场景下，考虑把 `_pending_alarms` 持久化到轻量级本地缓存（如 Redis），以支持多进程或重启不中断的去重窗口。

---

如需，我可以：
- 把聚合字段加入对外上报的请求体（在 `_send_alarm_report` 中加入 `alarm_count/first_seen/last_seen`）；
- 把告警 key 改为基于 `alarm_type` 或 `detection_result` 的哈希实现更精细的去重；
- 编写并运行一个小脚本，演示入队、合并与上报的完整流程（并打印请求体）。

选择其中一项我就直接实现并演示。