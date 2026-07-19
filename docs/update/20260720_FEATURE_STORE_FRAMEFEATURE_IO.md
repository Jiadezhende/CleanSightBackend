# FeatureStore 序列化/反序列化统一到 FrameFeature（离线打通）

> 日期：2026-07-20　依据：代码改动

在线滑窗/快照货币早已是帧级 `FrameFeature`（`ts + {source: FrameDetections}`），但 L2 落盘基座 `FeatureStore` 两端不对称：`append` 吃 `FrameInference`、`load`/`load_many` 吐 per-source `Dict[str, List[FrameDetections]]`。本次把两端 + 离线消费端统一到 `FrameFeature`。**磁盘格式 `{ts, features, wh}` 不变**（老 `features.jsonl` 兼容）。

## 改动

### 序列化（写入端）
- [store.py](../../app/services/inference/feature/store.py) `append(feature: FrameFeature, ...)`：从 `feature.by_source`/`feature.ts` 序列化，`wh` 仍由 `_extract_frame_wh(feature.by_source)` 提取。
- [service.py](../../app/services/inference/detection/service.py) 写回口传 `feature`（帧窗/快照/落盘共用同一份，不再传 `res`）。`FrameInference` 彻底退化为 pool→写回口传输消息。

### 反序列化（读取端）
- [store.py](../../app/services/inference/feature/store.py) **`load(task_id, step_id) -> List[FrameFeature]`** 一次到位替换旧 `load(source)`/`load_many`（**已删**）：单扫 `features.jsonl`，每行还原一个 `FrameFeature`（by_source 含该行全部 source，present-key 落在键集；`wh` 回填每个 `FrameDetections.metadata` 的 `frame_width/height`），按 ts 升序。无包装、无嵌套。

### 离线消费端（吃 FrameFeature）
- [offline/segmenter.py](../../app/services/inference/offline/segmenter.py) 基类 `preprocess(self, frames: Sequence[FrameFeature])`。
- [offline/segmenters/clean.py](../../app/services/inference/offline/segmenters/clean.py)：**`build_base_features` 直接吃 `Sequence[FrameFeature]`**（唯一调用点、无直测）——per-source 合并折进 `_collect_object_arrays` 的既有检测遍历（多一层 `by_source.values()`），`_frame_size` 从任一流 metadata 取宽高。preprocess 收敛成一行（**无 by_ts 融合、无中间 FrameDetections**）。`build_base_features` 之后的 recipe（113/121/249 维）零改动。
- [offline/segmenters/mock.py](../../app/services/inference/offline/segmenters/mock.py)：preprocess 透传、segment 逐帧遍历 `by_source`。
- [offline/runner.py](../../app/services/inference/offline/runner.py)：`frames = load(...)`；empty 判定改按 `by_source` 键集并集。

## 边界 / 不动
- 改动止于各 segmenter `preprocess` 返回（segment/模型推理/解码不动）。
- 在线 `CleanOperator._adapt_to_features`(6 维) 与离线 `build_base_features`(113+ 维) 仍是刻意分离的两条特征管线，**本次不统一**。metadata 键沿用离线读的 `frame_width/height`（在线用 `frame_shape`，两路不交叉）。

## 测试
- `test_offline_pipeline`：`TestLoadMany`→`TestLoad`（断言 `List[FrameFeature]` + `wh` 往返）；直调 preprocess 的用例经本地 `_frames()` 由 per-source dict zip 成帧；假 segmenter 签名跟改。
- `test_offline_reservation`/`test_feature_store_owner_fence`：`append(FrameFeature)` + `load()` 后按 `by_source[src]` 断言。
- `test_writeback_handle_fence`：spy 断言落盘键 + `by_source is res.detections`。
- `pytest tests/` **335 passed**。
