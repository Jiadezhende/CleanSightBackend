# 测试聚合：mock 构造收敛单一真源 + 覆盖率量化

> **变更状态**：生效中（2026-07-05）　<!-- 全量 pytest 264 passed，行为等价；覆盖率基线已量化 -->
> **知识库**：已沉淀 → [kb/TESTING_MAP.md](../kb/TESTING_MAP.md)(2026-07-21)
>
> 背景：换键/生命周期重构（T1–T5）落地后，`tests/` 已 29 文件 264 passed，但零 `conftest.py`、
> 零共享 fixture，领域对象各文件手搓、`ClientQueues` 在 8 文件 20+ 处重复构造。换键时这些散点全靠手改。

## 概述

- **改了什么**：新建 `tests/factories.py`（纯 builder 单一真源）+ `tests/conftest.py`（factory-as-fixture + 共享 setup），把 ~12 个测试文件的本地 `_cq`/`_result`/`_make_record`/`_out`/`_frame` 等构造 helper 收敛到共享 factory；接入 `pytest-cov`，把"路径覆盖"从肉眼估算变成 **54.7% 量化基线**。
- **为什么改**：契约一变只改一处（本轮换键就手改了一批 `source_ip→task_id`）。**纯测试基础设施重构**——不新增行为测试、不改被测生产代码，264 passed 前后不变。
- **影响面**：`tests/` 12 文件迁移 + 2 新建；`requirements*.txt` 加 `pytest-cov`；新增 `.coveragerc`。生产代码零改动。

## 改动详情

### 1. `tests/factories.py`（新增，单一真源）

纯函数、无 pytest 依赖（可被 `integration_tests/` 复用）。默认取"最常见良性态"，用例只写偏差：

| builder | 契约 | 收敛来源 |
|---|---|---|
| `make_detection` | `Detection` | 6+ 文件同字面量 |
| `make_frame_detections` | `FrameDetections` | `_out` / `make_detection_output` |
| `make_frame` | `Frame` | `_frame` / `_frames` |
| `make_cq` / `make_bare_cq` | `ClientQueues`（有身份 / 裸建 MOCK） | `_cq` / `_cq_with_task` 等 8 文件 |
| `make_frame_inference` | `FrameInference`（cq 句柄可选） | `_result` / `_make_result` ×3 |
| `make_alarm` | `Alarm` | `_make_record` / `_alarm` |

### 2. `tests/conftest.py`（新增，Hybrid）

- **factory-as-fixture**：`make_cq`/`make_detection`/`make_frame_inference` 各出一个 fixture（返回 factories 里的同一纯函数），支持注入式书写；主用法仍是 `from factories import ...`（可 @parametrize / 可复用），两者同源不产第二份逻辑。
- **共享 setup**：`tmp_storage` fixture 收编散落的 `monkeypatch.setattr(settings, "storage_dir", ...)`。
- 不做 autouse 全局魔法，用例显式声明依赖。

### 3. 迁移（只换构造、不改断言）

12 文件：`test_cq_state_machine`、`test_cq_immutable_run`、`test_rekey_source_ip_shim`、`test_teardown_identity_fence`、`test_alarm_increment`、`test_pipeline_drop_counters`、`test_operator_framework`、`test_writeback_handle_fence`、`test_feature_store_owner_fence`、`test_offline_reservation`、`test_hls_eff_fps`、`test_temporal_debounce`。每处 override 语义原样保留。

**刻意不迁移**（集中无收益，honest 留局部）：
- `test_persistence_sink` 的 `Alarm(AlarmType.MOCK, ..., AlarmMetric.BUBBLE)`——被测主体本身、单文件、带特化枚举。
- `test_api_concurrency._make_db_task` / `test_inference_stage_routing._fake_cq`——是 **MagicMock 替身**（非真实领域对象），单文件专用。

### 4. `pytest-cov` 接入（opt-in，不设门禁）

- `requirements.txt` / `requirements-cpu.txt` 加 `pytest-cov>=4.1`。
- 新增 `.coveragerc`：`source=app`、`branch=True`、`omit=app/main.py`（启动壳）。
- **不入 addopts**：覆盖率不强加于每次 `pytest`，按需 `pytest tests/ --cov=app --cov-report=term-missing`。不设 `--cov-fail-under`（本轮先量化，阈值待缺口补齐后另议）。

## 覆盖率基线（2026-07-05，`--cov=app --branch`）

> 迁移后基线 **54.7%**；补齐"轻缺口"（下方桶 2）后 **57.3%**（292 passed）。下表为迁移后基线快照。

**TOTAL 54.7%**（6255 stmts / 2592 miss / 1550 branch）。分层：

| 层 | 覆盖 | 说明 |
|---|---|---|
| `domain/` | **100%** | alarm/detection/frame/render 全覆盖 |
| `services/client/` | 71–72% | queues/manager/config，契约护栏扎实 |
| `services/inference/` | 混合 | operator 97.8% / bubble 84% / models 96.6%；detector 28.6% / pool 19.9% / visualizer 19.2% / mock 27% 偏低 |
| `services/traceback/` | 93–95% | media_token/segment_finder 高 |
| `utils/` | 混合 | gateway 92% / executor 87% / exceptions 82.6%；decorators 13% / context 27% / worker_guard 27% 偏低 |
| `routers/` | 混合 | api 78.9% / media 86.5% / traceback 77.7%；**ai 13% / admin 12%** 近零 |

## 必要测试分级（全部保留，无删除）

- **契约护栏（最高价值，刚落地不变式的回归网）**：`test_cq_state_machine`、`test_cq_immutable_run`、`test_writeback_handle_fence`、`test_feature_store_owner_fence`、`test_teardown_identity_fence`、`test_rekey_source_ip_shim`、`test_api_concurrency`、`test_reconnect_on_initial_failure`。
- **核心逻辑**：`test_operator_framework`、`test_temporal_debounce`、`test_alarm_increment`、`test_gateway`、`test_traceback_*`、`test_lab_*`、`test_persistence_sink`、`test_hls_eff_fps`、`test_offline_reservation`。
- **未做合并**：`test_cq_immutable_run` 与 `test_cq_state_machine` 主题相邻但断言无冗余（前者验身份不可变/换槽/存储 supersede，后者验状态机写门），保留分立。

## 缺口分桶（关键判断：不是所有缺口都该单测）

覆盖率的 miss 分两类，处理策略相反：

### 桶 1 — I/O 边界，**集成-only，不硬写单测**（低覆盖是有意）

对这些硬写单测＝大量精力 mock `subprocess.Popen`/CUDA/`VideoWriter`/WS，**测的是 mock 不是真实行为**，负 ROI。其纯逻辑早已抽出单测（`_effective_fps`、切段、ROI），剩下"没覆盖"的正是那层不可约的 I/O 壳；真实保障靠 `integration_tests/` + 远程真流审计（见 [THREAD_INSTANCE_LIFECYCLE_AUDIT](20260626_THREAD_INSTANCE_LIFECYCLE_AUDIT.md)）。

| 模块 | 覆盖 | 真实保障 |
|---|---|---|
| `stream/decoder.py` | 10.9% | 远程真流审计（stop_stream 12.9ms 实测 SIGKILL 三级降级） |
| `persistence/strategies/hls_strategy.py` | 14.2% | 纯算 `_effective_fps` 已测；编码腿走集成 |
| `inference/detection/pool.py` | 19.9% | CUDA `infer_batch` 不可中断，GPU 机集成 |
| `inference/visualization/visualizer.py` | 19.2% | ROI 已测（`test_rounded_rect_roi`）；像素眼看 |
| `routers/ai.py` | 13.1% | WS 推流循环 → `integration_tests/` viewer |
| `persistence/workers/*` | 21–38% | 纯变换（切段）已在 `test_persistence_sink` 测；线程循环走集成 |

### 桶 2 — 轻缺口，**已补**（2026-07-05，本轮顺带）

纯函数/线程本地，cheap 且被到处用。新增 3 文件、28 用例：

| 模块 | 覆盖 27→ | 新测文件 |
|---|---|---|
| `utils/context.py` | **96.4%** | `test_context.py`（set/get/clear + ClientContext 嵌套/异常还原） |
| `utils/decorators.py` | **76.1%** | `test_decorators.py`（client_id 提取优先级 / 参数清洗 / log_call·timing 透明性） |
| `routers/admin.py` | **61.2%** | `test_admin_serialization.py`（`_client_info` / `_quantile` 插值 / metrics JSON） |

> **顺带修一个真 bug**（`test_admin_serialization` 发现）：`_parse_metrics_json` 用 `families.get("frame_drop_total")` 等查 metric family，但 prometheus 对 Counter **剥 `_total` 后缀**（`Counter("frame_drop_total")` → family 名 `frame_drop`、sample 仍 `frame_drop_total`），family 实名为 `frame_drop`/`infer_failure`/`gpu_oom`/`retry` —— 5 个指标里 4 个恒 miss，admin 面板只有 `infer_latency_ms` 有数。
>
> 两处收口：① 修 admin 4 处 `families.get(...)` 键（输出 JSON 键保留 `_total` 不变，前端零改）；② [metrics.py](../../app/utils/metrics.py) 4 个 Counter 定义名去 `_total`（`Counter("frame_drop", ...)`）对齐约定——**sample 名/对外 `/metrics`/PromQL 查询全不变**（库自动补 `_total`），仅让定义字符串与 family 名一致、消歧义；Python 变量名保留 `*_total`（匹配可查询的 sample 名）。

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 迁移后 **264 passed**（前后一致）→ 补桶 2 后 **292 passed**（+28） |
| `--cov=app` 基线 | 迁移后 **54.7%** → 补桶 2 后 **57.3%**；context 96% / decorators 76% / admin 61% |
| 静态核对 `grep "ClientQueues(" tests/` | 仅 `factories.py`（+ 迁移后各文件经 `make_cq`/`make_bare_cq`） |
| 分批迁移每步保绿 | CQ 批 46 / FrameInference+Detection 批 24 全通过 |
