# 滑窗改帧级 FrameFeature：砍 `_zip_by_ts`，多流对齐上移写回口

> **变更状态**：生效中（2026-07-17）
> **知识库**：已沉淀 → [kb/SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)(2026-07-17)
>
> 触发方案：[specs/INFERENCE_ENGINE_DESIGN.md](../../specs/INFERENCE_ENGINE_DESIGN.md) 讨论中的实时特征路收敛。
> 承接：建立在「online/offline 分离 + FeatureStore 单源」之上。本次是「上游共享特征」落地的第一步（下一步：extract 抽取共享 + FeatureStore frame_shape 往返 + 离线 segmenter 复用）。

## 概述

- **改了什么**：`ClientQueues` 滑窗从 **per-stream `Dict[str, Deque[FrameDetections]]`** 改为 **单条帧级 `Deque[FrameFeature]`**；写回口把整帧 `FrameInference` 一次物化成 `FrameFeature`（`ts + {流名: FrameDetections}`，新增于领域层 [app/domain/detection.py](../../app/domain/detection.py)）；算子契约 `analyze` 改收 `List[FrameFeature]`；删除 `Operator._zip_by_ts` 与 `AlignedFrame`。
- **为什么改**：同 stage 各 detector 跑同一批帧（`FrameInference` 已携整帧多流），旧路先 `push_detection` 逐 detector 拆成 per-stream、算子再 `_zip_by_ts` 按 ts inner-join 拼回——拆了又拼纯冗余；且动作识别不需要算子链/异构节奏对齐，zip 潜在价值不成立。
- **影响面**：`queues`（窗口结构 + 4 个方法体，函数名全保留）、写回口 `service._write_back_results`、`operator` 基类契约、`actor._tick`、四个 workflow 算子、`signals_10s` 聚合内部实现。

## 改动详情

### 1. [app/domain/detection.py](../../app/domain/detection.py) — 新增 `FrameFeature`
帧级多流对齐记录 `ts + by_source: {流名: FrameDetections}`；不含 `cq`，`client`/`inference`/`offline` 均 import 安全。持有对齐后的检测（非计算特征），张量化仍在算子内。

### 2. [app/services/client/queues.py](../../app/services/client/queues.py) — 窗口改帧级（**函数名全保留**）
- `_slide_window: Dict[str, Deque[FrameDetections]]` → `Deque[FrameFeature]`；删 `_stream_windows`；`_slide_window_seconds` 语义改为"帧窗保留时长"（= `max(10s 底线, 各算子最大感受野)`）；新增模块常量 `_SIGNALS_WINDOW_SEC = 10.0`。
- `push_detection(task_name, output)` → `push_detection(feature: FrameFeature)`（一帧一次）。
- `get_slide_window(task_name)` → `get_slide_window()`（返 `List[FrameFeature]`）。
- `get_slide_window_summary` 重写：遍历帧窗、按 `by_source` 聚合，**固定 10s 底线裁窗**，输出格式 `{流名: {active, hit_count, max_conf}}` 不变（[app/routers/task.py](../../app/routers/task.py) 契约不动）。

### 3. [service.py](../../app/services/inference/detection/service.py) `_write_back_results`
per-detector `push_detection` 循环 → 保留 degraded 警告遍历 + 物化一份 `feature = FrameFeature(ts=res.timestamp, by_source=res.detections)`（共享 `res.detections` 引用，不复制），**帧窗 + 原子快照共用**：`cq.push_detection(feature)` + `cq.set_latest_inference(feature)`。`FrameInference` 退化为纯 pool→写回口传输消息，不再被 cq 留存 → 消除 `cq→_latest_inference→cq` 自引用环。

### 3b. Viz 快照改吃 FrameFeature
`_latest_inference: Optional[FrameFeature]`；`set/get_latest_inference` 类型改 `FrameFeature`（**函数名不变**）。[worker.py](../../app/services/inference/visualization/worker.py) 读 `inference.ts`/`.by_source`，stage 取自 `cq.stage`（不可变身份，快照不再携 stage）。

### 4. [operator.py](../../app/services/inference/temporal/operator.py) — 契约帧级 + 删 zip
删 `AlignedFrame` / `_zip_by_ts`；`analyze(windows: List[FrameFeature])`；`_clip` 按 `FrameFeature.ts` 裁；`primary_window` = 裁窗后投影首个订阅流的逐帧 `FrameDetections`。

### 5. 算子接入点
- bubble/bending/mock（[workflows](../../app/services/inference/workflows/)）：`analyze` 仅换类型注释 → `List[FrameFeature]`，`primary_window(windows)` 及体内逻辑逐字不变。
- [clean.py](../../app/services/inference/workflows/clean.py) `CleanOperator.analyze`：`_zip_by_ts` → `_clip`，其余（`_advance`/`_adapt_to_features`）不变（`by_source`/`.ts` 对 `FrameFeature` 同构）。

### 6. [actor.py](../../app/services/inference/temporal/actor.py) `_tick`
循环前一次 `windows = cq.get_slide_window()`；各算子 `op.analyze(windows)` 自行 `_clip`/投影。删 per-op `{src: get_slide_window(src)}` 逐流构建。

### 7. [queues.py](../../app/services/client/queues.py) — 顺手清理无效锁 `_task_lock`
`_task_lock` 是历史遗留：身份 `task_id/step_id/stage/task_started_at` 早已是构造定死的不可变
primitive、热路径免锁直读，该锁不再护任何状态。删除声明 + Lock Inventory 条目 + `_release_payload`
持锁列表条目，payload 锁 **7 → 6**（`_release_payload` docstring / RunState 注释 / 锁库存注释同步改 6）。
与本次帧窗改造无耦合，一并清理。

### 保留项（不改动）
- 函数名 `push_detection` / `get_slide_window` / `get_slide_window_summary` / `set_stream_windows` / `analyze` / `primary_window` / `_clip` 全保留（仅签名/体变），便于 diff 对应。
- `set_latest_inference`（Viz 单槽）、`feature_store.append`（离线腿）不动。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| `_slide_window`（帧级 FrameFeature） | 写回口 `push_detection` 一帧一次 | 算子 `analyze` + `signals_10s` 汇总 | 是（per-stream → 帧级；无 zip） |
| `_latest_inference` | 写回口 `set_latest_inference` | Viz | 是（改存 `FrameFeature`，去自引用环；Viz stage 取 `cq.stage`） |
| `FeatureStore` | 写回口 `feature_store.append` | 离线 | 否 |

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 296 passed |
| signals_10s 语义 | `get_slide_window_summary` 输出格式/键不变，固定 10s 底线；今无 stream 感受野 >10s 故逐值等价 |
| 迁移单测 | 帧窗工厂 `make_frame_feature`（[tests/factories.py](../../tests/factories.py) 单源）；`_zip_by_ts` 用例改为验证写回口 `FrameFeature` 携全流 |
