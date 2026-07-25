# FeatureStore 序列化/反序列化统一到 FrameFeature（离线打通）

> **变更状态**：生效中（历史记录，本次维护确认）
> **知识库**：已沉淀 → [kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)(2026-07-21)

> 日期：2026-07-20　依据：代码改动

在线滑窗/快照货币早已是帧级 `FrameFeature`（`ts + {source: FrameDetections}` + 帧级 `frame_width/height`），但 L2 落盘基座 `FeatureStore` 两端不对称：`append` 吃 `FrameInference`、`load`/`load_many` 吐 per-source `Dict[str, List[FrameDetections]]`。本次把两端 + 离线消费端统一到 `FrameFeature`。磁盘 record 为 `{ts, features, frame_width?, frame_height?}`（帧分辨率全命名键、缺省省略，老 `features.jsonl` 兼容）。

## 改动

### 序列化（写入端）
- [store.py](../../app/services/inference/feature/store.py) `append(feature: FrameFeature, ...)` 经 `_feature_to_record`：`ts`/`by_source` → `{ts, features}`，`frame_width/height` 两者皆非 None 时落顶层全命名键。
- [service.py](../../app/services/inference/detection/service.py) 写回口传 `feature`（帧窗/快照/落盘共用同一份，不再传 `res`）。`FrameInference` 彻底退化为 pool→写回口传输消息。

### 反序列化（读取端）
- [store.py](../../app/services/inference/feature/store.py) **`load(task_id, step_id) -> List[FrameFeature]`** 一次到位替换旧 `load(source)`/`load_many`（**已删**）：单扫 `features.jsonl`，每行经 `_record_to_feature` 还原一个 `FrameFeature`（by_source 含该行全部 source，present-key 落在键集；`ts` 在此边界统一 `float`；`frame_width/height` 还原到 **`FrameFeature` 字段**，非 `FrameDetections.metadata`），按 ts 升序。无包装、无嵌套。

### 离线消费端（吃 FrameFeature）
- [offline/segmenter.py](../../app/services/inference/offline/segmenter.py) 基类 `preprocess(self, frames: Sequence[FrameFeature])`。
- [offline/segmenters/clean.py](../../app/services/inference/offline/segmenters/clean.py)：**`build_base_features` 直接吃 `Sequence[FrameFeature]`**（唯一调用点、无直测）——per-source 合并折进 `_collect_object_arrays` 的既有检测遍历（多一层 `by_source.values()`），帧分辨率直接读 `FrameFeature.frame_width/height`（缺失回退传入默认，逻辑内联、无独立 `_frame_size` 函数）。**`transform_features` 已并入 `preprocess`**：base `preprocess` 只做 `build_base_features`（v2/113），ASFormer/BiGRU 覆盖 `preprocess` 经 `super().preprocess()` 叠 recipe（121/249）。`build_base_features` 之后的 recipe 函数（`add_business_priors`/`add_centered_window_stats`）零改动。
- [offline/segmenters/mock.py](../../app/services/inference/offline/segmenters/mock.py)：preprocess 透传、segment 逐帧遍历 `by_source`。
- [offline/runner.py](../../app/services/inference/offline/runner.py)：`frames = load(...)`；empty 判定改按 `by_source` 键集并集。

## 边界 / 不动
- 改动止于各 segmenter `preprocess` 返回（segment/模型推理/解码不动）。
- 在线 `CleanOperator._adapt_to_features`(6 维) 与离线 `build_base_features`(113+ 维) 仍是刻意分离的两条特征管线，**本次不统一**。帧分辨率现为 `FrameFeature` 帧级字段（pool 从原始帧盖章、store 往返还原），在线/离线同源。

## 顺带清理（同批）
- `_frame_size` 只一处调用 → 内联进 `_collect_object_arrays`，删函数。
- `ts` 类型转换归到反序列化边界（`store.load`），`build_base_features` 去掉 `float(ff.ts)`。
- inference 模块 import 全量扫描：删 4 处未用（`dispatcher.py` 的 `ClientQueues`、`manager.py` 的 `Tuple/Type/Union`）。

## 测试
- `test_offline_pipeline`：`TestLoadMany`→`TestLoad`（断言 `List[FrameFeature]` + `frame_width/height` 往返到 `FrameFeature` 字段、`FrameDetections.metadata` 为空）；直调 preprocess 的用例经本地 `_frames()` 由 per-source dict zip 成帧；假 segmenter 签名跟改。
- `test_offline_reservation`/`test_feature_store_owner_fence`：`append(FrameFeature)` + `load()` 后按 `by_source[src]` 断言。
- `test_writeback_handle_fence`：spy 断言落盘键 + `by_source is res.detections`。
- `pytest tests/` **335 passed**。
