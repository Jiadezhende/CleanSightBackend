# 删除 keypoints JSON 死写——detection 已单源落 FeatureStore

> **变更状态**：生效中（2026-06-27）
> **知识库**：已沉淀 → [kb/SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)(2026-07-21)
>
> 相关：[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（online/offline 分层数据流）、[20260627_INFER_CONTRACT_PURITY.md](20260627_INFER_CONTRACT_PURITY.md)（推理契约提纯）。

## 概述

- **改了什么**：删掉 HLS 持久化每段附带落盘的 `keypoints_{ts_us}.json`，以及它在 segment_finder / traceback / media / media_token 一整条下游引用链。
- **为什么改**：`FrameData.keypoints` 字段全仓**从无任何赋值**（持久化路径上恒为 None），落盘内容永远是 `[{"timestamp": x, "keypoints": null}]` 的死数据；真正的 detection 在 online/offline 拆分后已**单源落到 FeatureStore**（`features.jsonl`，按帧 ts 对齐）。keypoints JSON 既无内容产出，又是重复落盘，纯属拆分遗留的死写。
- **影响面**：persistence 每个 processed 段少一次无意义文件 IO；`GET /traceback/alarm/{id}/evidence` 返回体去掉 `keypoints_url`/`detection` 字段；`GET /media/keypoints/{token}` 路由下线。

## 改动详情

### 1. `app/services/persistence/strategies/hls_strategy.py` — 删落盘块与死代码

- 删 `_persist_processed_segment` 中的「写 keypoints JSON」整段（含 `PersistenceError` 包装）。
- 删因此变为死代码的 `_make_serializable()` 方法。
- 清理随之失效的 `import numpy as np`、`typing.Any`。

processed 段持久化现在只写视频段 + playlist + metadata。

### 2. `app/models/frame.py` — 删 `FrameData.keypoints` 字段

该字段标注「仅 ProcessedQueue」，但 grep 全仓无一处对它赋值，两处 `FrameData(...)` 构造（decoder / visualization）均不传它，恒为默认 None。

### 3. `app/services/traceback/segment_finder.py` — 删 keypoints 定位

- 删 `SegmentRef.keypoints_filename` 字段及其在 `list_segments` / `find` 中的计算与回填。
- 删 `keypoints_path()` 方法。
- 清理失效的 `typing.Optional` 与相关 docstring。

### 4. `app/routers/traceback.py` — evidence 接口去 keypoints

- 删 `_keypoints_url()` 辅助函数。
- evidence 接口删 keypoints 提取块；返回体去掉 `keypoints_url`、`detection` 两字段。
- 清理失效的 `import json`。

### 5. `app/routers/media.py` + `app/services/traceback/media_token.py` — 下线 keypoints 媒体通道

- 删 `GET /media/keypoints/{token}` 路由。
- `MediaKind` / `_VALID_KINDS` 由 `("segment", "keypoints", "init")` 收敛为 `("segment", "init")`。

### 6. 保留项（不改动）

- `app/services/inference/`（`data_models.py` / `workers/visualization.py` / `store.py`）中的 `Detection.keypoints`——这是**推理检测契约**里 keypoint 模型的合法输出字段，与本次删除的「持久化死写」无关，刻意保留。
- FeatureStore 落盘（`features.jsonl`）是 detection 的唯一真源，本次未动。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| keypoints JSON（HLS 段旁） | persistence（恒 null） | traceback evidence / media 路由 | 是——整条删除 |
| FeatureStore `features.jsonl` | inference（offline） | 离线分析 | 否——detection 单一真源不变 |

> 接口契约变更：前端若曾读取 evidence 的 `keypoints_url`/`detection`，需同步知晓二者已移除（移除前返回的也只是全 null 死内容）。

## 后续计划

若溯源确需展示检测结果，走「方案 B」：traceback 改为从 FeatureStore 按触发段 ts 区间读 `features.jsonl`，那是有真数据的单一真源，而非重建这条死写。

## 验证

| 项 | 结果 |
|----|------|
| 相关单测（traceback media_token / router / segment_finder） | 57 passed |
| 全量 `pytest tests/` | 203 passed |
| 残留扫描 `grep -rn keypoints app/`（排除 inference Detection 合法字段） | 无残留 |
