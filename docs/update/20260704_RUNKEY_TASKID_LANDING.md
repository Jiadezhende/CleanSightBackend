# 换键落地：运行键收敛为 int task_id + source_ip 边界垫片转正

> **变更状态**：生效中（2026-07-04）　<!-- 运行时全量 task_id 键已落；对外 wire 保持 source_ip 不变 -->
> **知识库**：已沉淀 → [kb/SERVICE_RUN_CONTROL.md](../kb/SERVICE_RUN_CONTROL.md)(2026-07-21)
>
> 承接：运行身份重构的收官落地——前序已把 CQ per-run 不可变、状态机 ACTIVE/DRAINING/CLOSED、单一拆除出口、句柄写回就位；本文记「换键 client_id→task_id」这一步的实际落地。

## 概述

- **改了什么**：把运行时注册表/锁/decoder/actor/FeatureStore 的路由键从 `client_id`(=source_ip 字符串) 收敛为 **`task_id`(int)** 单键，服务层内部形参/变量/字段名一并从 `client_id` 翻成 `task_id`；删掉 `CleaningTask` 运行态 VO，身份改为 primitives 直挂 `ClientQueues`；CQ 构造职责上移 `RunController`。
- **为什么改**：承接 T5「换键 + ClientManager 降级 RunRegistry」。`client_id` 是可变路由键、非 run 身份，历史上既当键又当身份，导致「一 task 一 run」要靠扫描/双向索引维持、迟到写回易跨 run 串台。`task_id` 单键后 `registry[task_id]` 天然单槽位（Q2），双向索引消失。
- **与工单的偏差（重要）**：**T6 的「撤垫片、wire 改 `?task_id=`」本次暂缓**——未来会迁移，但现阶段不急着动前端接口。生产前端按摄像头 `source_ip` 连 `/ai/video?client_id=<ip>`（见 [RTSP_FLOW.md](../RTSP_FLOW.md)），此 wire 现阶段保持不变。故 `source_ip` 当前既是被动身份字段、也承担**边界路由标识**；`find_by_source_ip` 是现阶段的边界适配器（T6 迁移后可撤）。
- **影响面**：`ClientManager` / `StreamService` / `GlobalHealthMonitor` / `InferenceManager` / Dispatcher / Pool / VisualizationWorker / `ClientTemporalActor` / `RunController` / 路由层 `api`·`ai`·`admin`。**lab 页与 lab.py 不受影响**（纯 task_id/step_id + 文件系统/DB，不碰运行时注册表）。

## 改动详情

### 1. 运行键 `client_id`(str) → `task_id`(int) — 注册表/锁/句柄键

- [`ClientManager`](../../app/services/client/manager.py)：`_runs`/`_task_locks` 键 `str→int`；`get`/`has_client`/`set`/`remove`/`remove_if`/`lock_for` 全改 int 键；**`get_client_by_task_id` 由扫描降为 `_runs.get(task_id)` O(1) 直取**（键即 task_id，双向索引消失）；删 `get_or_create`（哑存储、不建 CQ）。
- [`InferenceManager._actors`](../../app/services/inference/manager.py)、[`StreamService.decoders`](../../app/services/stream/service.py)、`GlobalHealthMonitor._reconnecting_clients`/`_last_activity`、`VisualizationWorker._last_rendered_ts` 键同步 int。
- 键类型经历 `source_ip(str) → run_key(str(task_id)) → task_id(int)` 两跳；`run_key` property 中间态**已删**，消 str/int 双身份。

### 2. `CleaningTask` VO 删除 — 身份 primitives 直挂 CQ

- 删 `app/domain/task.py`。[`ClientQueues`](../../app/services/client/queues.py) 构造直注 `task_id/current_step/status/source_ip/stage`（不可变身份，一 CQ == 一 run）；`get_task`/`step_id_of`/`set_task` 等复用机器移除。
- **CQ 构造上移** [`RunController.start_run`](../../app/services/run_control.py)（编排者建 CQ → `start_workflow(cq)`）；`stage` 由 [`InferenceManager.resolve_stage`](../../app/services/inference/manager.py)（原 `_resolve_stage` 转公有）在建 CQ 前解析一次。

### 3. 内部命名对齐 + 诊断字段语义锚定 source_ip

> 路由键改名后,「名叫 client_id、值却是 task_id」的语义漂移必须消除;但面向人的诊断/错误字段仍需可读的 source_ip。二者分离:

- **路由键**：`StreamService`（`start/stop/restart_stream`、`_get_client_queues`、`get_all_client_ids`→**`get_all_task_ids`**）、`FFmpegDecoder`（形参/属性 `client_id`→`task_id`）、`GlobalHealthMonitor` 全链路、`DetectionTask`/`FrameInference.client_id`→`task_id`、Dispatcher/Worker 迭代变量 — 一律 `task_id`。
- **诊断字段**：分两步落地——
  - 第一步（值锚定 source_ip）：`persist_alarms(client_id=...)`、[`RunController.stop_run`](../../app/services/run_control.py) 结果 dict、持久化 `AlarmRecord.client_id` 等，值改从捕获的 `cq.source_ip` 取，不再拿路由键冒充；
  - 第二步（**异常身份升级**，2026-07-05）：[`exceptions.py`](../../app/utils/exceptions.py) 的 `AppError` 及 7 子类删 `client_id` 字段，改带 **`task_id`+`step_id`**（排障主键）+`source_ip`（辅助）；`__str__` 输出 `[task=..][step=..][source_ip=..]`；raise 现场从 cq 派生三元身份（decoder 加 `_err_identity()` 复用）；[`main.py`](../../app/main.py) handler 日志 `extra` 带三元、**响应体 `client_id` wire 键保留**（值=`exc.source_ip`）；删无调用方的 `get_client_id_from_exception`。

### 4. 边界垫片转正（source_ip → 当前 run，匹配首个）

- [`ClientManager.find_by_source_ip`](../../app/services/client/manager.py)：扫描当前快照、**匹配首个** source_ip 命中（业务不保证 source_ip 唯一）。
- [`api.py /terminate`](../../app/routers/api.py)：wire 不变（`client_id` 参数即 source_ip）→ 垫片解析 → `stop_run(cq.task_id)`；查不到 run → success no-op。
- [`ai.py WS /ai/video`](../../app/routers/ai.py)：每轮 `find_by_source_ip(source_ip)` → 读其最新渲染帧，run 重启/切换自动跟随。
- [`api.py /start`](../../app/routers/api.py)：入参 task_id + source_ip，按 **task_id** 建 run；source_ip 作身份字段注入 CQ。

### 5. admin 页路由修复（换键回归）

- [`admin.py _client_info`](../../app/routers/admin.py) 增补 `source_ip` 字段（`/overview`·`/clients` 载荷）。
- [admin 页 `startLive`](../../app/static/admin/index.html)：改用所选 run 的 `source_ip` 连 `/ai/video`（此前误传 int task_id → `find_by_source_ip` 恒 miss、实时画面黑屏）；缺 source_ip 时提示不连；补 `ElMessage` 解构。
- `/clients/{client_id}/alarms` 路径参数 `str→int`（否则 int 键注册表恒 miss）。

### 6. 保留项（不改动）

- **`source_ip` 作 `/ai/video`·`/terminate` 的对外 wire**：生产前端/集成测试 viewer 均按摄像头 IP 连，现阶段契约不变（[RTSP_FLOW.md](../RTSP_FLOW.md)、`integration_tests/*`）；T6 迁移前不动前端。
- **诊断字段名 `client_id`**：异常/告警/错误响应中保留该字段名（值=source_ip），避免破坏已有日志/前端解析。

## 边界契约表

| 端点 / 组件 | 键语义 | 说明 |
|------|------|------|
| `POST /api/start` | 入参 task_id + source_ip | 按 **task_id** 建 run |
| `POST /api/terminate` | wire=`client_id`(=source_ip) | 垫片 → `stop_run(task_id)`；无 run→no-op |
| `WS /ai/video?client_id=` | 参数**恒为 source_ip** | `find_by_source_ip` 匹配首个，跟随当前 run |
| `GET /task/message/{task_id}`、`/task/{id}/alarms` | task_id | 实时内存 / DB 双源 |
| admin `/overview`·`/clients` | client_id=task_id + source_ip | 前端据 source_ip 连 WS |
| lab 页 / lab.py | task_id + step_id | 走文件系统/DB，**不碰运行时注册表** |
| 运行时注册表/锁/decoder/actor/feature | **task_id(int)** | 单槽位保证 Q2 |
| 异常/告警/错误响应 `client_id` 字段 | =source_ip | 诊断标识，非路由键 |

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 264 passed |
| 换键相关新增/改动测试 | `test_rekey_source_ip_shim`、`test_cq_immutable_run`、`test_api_concurrency`、`test_teardown_identity_fence`、`test_writeback_handle_fence` 等同步 int 键 |
| 静态核对 | 全仓无 `run_key` 生产引用；无 `get_all_client_ids` 真实调用；`DetectionTask`/`FrameInference` 无 `.client_id` 残留 |
| admin 实时画面 | 改为按 source_ip 连 `/ai/video`（回归修复） |

## 后续 / 未决

- 工单 T6「wire 改 `?task_id=`」**本次暂缓，未来迁移**：现阶段不动前端接口，source_ip wire 保持。迁移时在 [ai.py](../../app/routers/ai.py)/[api.py](../../app/routers/api.py) 加 `?task_id=` 并保留 source_ip 兼容期，前端切换后再撤 `find_by_source_ip` 垫片。
- ~~`InferenceManager.get_result` 死代码~~、~~`get_client_by_task_id` 冗余~~ 已清理（2026-07-05）：`get_result` 无调用方删除（连带 `Frame` import）；`get_client_by_task_id` 与 `get` 等价，调用方（[task.py](../../app/routers/task.py)）改直接 `get(task_id)` 后删除。
- 可选：`ClientManager` → `RunRegistry` 类改名（纯 cosmetic，波及全仓 `client_manager` 单例 importer，暂未做）。
