> 更新时间：2026-09-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Testing Map

本文件索引当前测试覆盖面，并给后续修改提供优先补测方向。

## 测试数据构造单一真源（factories + conftest）

> 硬约束：构造 CQ / Detection / FrameInference 等领域对象**只走 `tests/factories.py`**，别在用例里另起炉灶或复制构造逻辑；契约一变只改 factories 一处。

- **`tests/factories.py`**（单一真源）：纯 builder 函数、**无 pytest 依赖**（可被 `integration_tests/` 复用；pytest prepend 模式把 `tests/` 插入 sys.path，用例直接 `from factories import ...`）。每个 builder 带「最常见良性态」默认值，用例只写关心的偏差（关键字 override）。
  - `make_detection`→`Detection`、`make_frame_detections`→`FrameDetections`、`make_frame_feature`→`FrameFeature`、`make_frame`→`Frame`、`make_cq`/`make_bare_cq`→`ClientQueues`（有身份 / 裸建 stage=MOCK）、`make_frame_inference`→`FrameInference`（cq 句柄可选，task_id/stage 缺省从 cq 派生）、`make_alarm`→`Alarm`。
- **`tests/conftest.py`**（Hybrid）：
  - factory-as-fixture——`make_cq`/`make_detection`/`make_frame_inference` 各出一个 fixture，返回 factories 里的**同一纯函数**，支持注入式书写；与 `from factories import ...` 同源、不产第二份逻辑。
  - 共享 setup——`tmp_storage` fixture 用 `monkeypatch.setattr(settings, "storage_dir", tmp_path)` 收编散落的存储根重定向（读写两侧同源、用例间不串）。
  - 不做 autouse 全局魔法，用例显式声明依赖。
- **刻意不收敛**（集中无收益）：MagicMock 替身（如 `test_api_concurrency` 的 db_task、`test_inference_stage_routing` 的 fake cq）与带特化枚举的单文件构造（`test_persistence_sink` 的 `Alarm(...MOCK...BUBBLE)`）留在本地。
- 静态护栏：`grep "ClientQueues(" tests/` 应仅命中 `factories.py`。

## API 与并发

- `tests/test_api_concurrency.py`：并发 start、任务切换、不同 client 不互阻、terminate 锁。
- `tests/test_start_rollback.py`：`start_run` setup 步失败 → 对称回滚注销 CQ（`stop_run(expected=cq)`），无泄漏。
- `tests/test_task_message_api.py`：实时消息接口。
- `tests/test_alarm_increment.py`：告警 gate、seq、自增和任务切换重置。

## 推理与时序

- `tests/test_inference_stage_routing.py`：`current_step` 到 stage 路由；`start_workflow` 不碰注册表（不 `set`）。
- `tests/test_pool_ts_anchor.py`：帧捕获 ts 锚点不变式——pool 穿透 `timestamps` 到 detector，各帧 `FrameDetections.timestamp` 精确等于捕获 ts（供写回口按 ts 一次物化整帧多流 `FrameFeature` 对齐）。
- `tests/test_temporal_debounce.py`：时序去抖逻辑。
- `tests/test_boundary_layers.py`：边界层行为。
- `tests/test_exception_handling.py`：异常分类和处理。

## 流与重连

- `tests/test_stream_rewrite.py`：RTSP URL 内部端口改写。
- `tests/test_reconnect_on_initial_failure.py`：初始拉流失败后重连。
- `integration_tests/test_single_client.py --scenario 2/3`：断流重连（成功 / 超时自动清理）端到端场景。

## Gateway

- `tests/test_gateway.py`：ASGI Gateway 行为。
- `tests/test_mediamtx_gateway.py`：MediaMTX Gateway 行为。

## 追溯与媒体

- `tests/test_traceback_router.py`：追溯路由。
- `tests/test_traceback_segment_finder.py`：段定位。
- `tests/test_traceback_media_token.py`：媒体 token。

## Lab

- `tests/test_lab_clip_builder.py`：Lab 裁剪构建。
- `tests/test_lab_step_exporter.py`：整段导出（在途段过滤、缺 init、临时 m3u8 清理、孤儿回收）。

## 离线帧反查（`frame_tracker`）

**两个测试互补，都要跑**——边界数学与「解出来的像素是不是那一帧」是两回事：

- `tests/test_frame_tracker_boundary.py`（进 `pytest tests/`）：**seam 单测，不起 ffmpeg**。子类覆盖
  `_run_ffmpeg` 改为按 sidecar 合成 Frame（真实实现的契约就是「产出 `sidecar[k_start..k_end]` 对应的
  帧」），造数只需空 `raw_segment_{ts_us}.mp4` 占位（`SegmentFinder` 只解析文件名）+ 真 `.idx`；复用
  `tmp_storage` fixture。覆盖段级/帧级边界、空区间、缺 sidecar 降级。
- `integration_tests/test_frame_tracker_roundtrip.py`（手动跑，约 10s）：**唯一能抓 ts↔像素错配的手段**。
  只依赖 ffmpeg，不需要 RTSP/DB/后端服务；走真实 `HLSPersistenceStrategy` 落盘再读回，帧内中心色块编码
  frame_id（三通道 16 阶量化抗 H.264 有损）逐帧比对。写 `database/9900002/` 后自清理。

## 覆盖率基线与缺口分层

> `pytest-cov` opt-in 接入（`requirements*.txt` 含 `pytest-cov`，配置见根 `.coveragerc`：`source=app`、`branch=True`、`omit=app/main.py`）。**不入 addopts、不设 `--cov-fail-under` 门禁**；按需 `pytest tests/ --cov=app --cov-report=term-missing`。

量化基线（2026-07-05 快照，`--cov=app --branch`）：迁移后 **54.7%**（264 passed），补齐轻缺口后 **57.3%**（292 passed）。分层：`domain/` 100%；`services/client/` 71–72%（契约护栏扎实）；`services/traceback/` 93–95%；`services/inference/` 混合（operator/models 高、detector/pool/visualizer/mock 偏低）；`routers/` 混合（api/media/traceback 高，ai/admin 近零）。

**缺口分两类，策略相反**：

- **桶 1 — I/O 边界，集成-only，低覆盖是有意**：`stream/decoder.py`、`persistence/strategies/hls_strategy.py`（编码腿）、`inference/detection/pool.py`（CUDA infer_batch）、`inference/visualization/visualizer.py`、`routers/ai.py`（WS 推流循环）、`persistence/workers/*`（线程循环）。硬写单测＝测 mock 不测真实行为，负 ROI；纯逻辑（`_effective_fps`/切段/ROI）早已抽出单测，真实保障靠 `integration_tests/` + 远程真流审计。
- **桶 2 — 轻缺口（纯函数/线程本地），已补**：`utils/context.py`（`test_context.py`）、`utils/decorators.py`（`test_decorators.py`）、`routers/admin.py`（`test_admin_serialization.py`）。
  - 补 admin 序列化测时**顺带修一个真 bug**：prometheus 对 Counter **剥 `_total` 后缀**（`Counter("frame_drop_total")` → family 名 `frame_drop`），`_parse_metrics_json` 原用 `families.get("frame_drop_total")` 查 family 名，5 指标中 4 个恒 miss。两处收口：admin 4 处 `families.get` 键去后缀 + [metrics.py](../../app/utils/metrics.py) 4 个 Counter 定义名去 `_total`。对外 `/metrics`/PromQL/输出 JSON 键全不变（库自动补 `_total`）。

## 建议补测

- 新增检测任务时，补 Detector/Analyzer 单元测试和 YAML 加载测试。
- 修改 HLS 写入逻辑时，补 playlist EXTINF、在途段过滤、timeline end_ms 测试。
- 修改清理流程时，补结算告警归属和残余段 flush 测试。
- 修改 Gateway 配置时，补 relaxed/bypass/normal 三类路径测试。
- 修改 Lab 上传时，补单段失败不影响整请求的响应结构测试。
- 动 sidecar 写读顺序或 `frame_tracker` 边界时，seam 单测 + round-trip 两个都要跑（前者抓边界、后者抓像素错配）。

## 代码来源

- `tests/`（`factories.py` 构造单一真源、`conftest.py` 共享 fixture）
- `.coveragerc`（覆盖率配置）
- `integration_tests/`
- `app/services/inference/`
- `app/services/persistence/`
- `app/routers/traceback.py`
- `app/routers/lab.py`

