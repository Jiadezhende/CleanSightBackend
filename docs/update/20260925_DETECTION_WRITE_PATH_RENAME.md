# 检测结果写链路 feature → detection 改名，落盘文件改为 `detections.jsonl`

> **变更状态**：生效中（2026-09-25）　<!-- 纯改名；落盘文件名与行内键变更，dev 上的 inference/features.jsonl 不再可见 -->
> **知识库**：待沉淀

## 概述

`FrameDetection` 从写回口到盘上这条链路上的 `features` 命名全部改为 `detections`：`ClientQueues` 落盘缓冲、`recording` 第二条队列、`app.storage.inference` 读写口、落盘文件 `features.jsonl` → `detections.jsonl` 与行内键 `"features"` → `"detections"`。对外可见的两处同步：丢帧 metric 标签、admin `queue_depths` 键。

## 变更背景

- **现状 / 痛点**：这条链路搬运的是检测层（L1）产出的 `FrameDetection`，与时序层（L3）的 `facts.jsonl` 同级，但处处叫 feature，与算子里真正算出来的特征张量撞词。
- **承接**：建立在 [20260924_DETECTION_TYPE_RENAME.md](20260924_DETECTION_TYPE_RENAME.md)（三型改名）与 [20260925_FRAME_DETECTION_MERGE.md](20260925_FRAME_DETECTION_MERGE.md)（`FrameInference` 并入）之上。

## 方案详情

### 全景：写链路与改名点

```text
写回口 → cq.append_ca_detections ─┐  ca_detections 缓冲（满则 detection_backpressure 丢帧）
                                   ▼
recording sweeper → cq.drain_ca_detections → submit_detections → _detection_queue
                                   ▼
_write_detections（代次表 _claimed_detections）→ inference.append_detections
                                   ▼
{task}/{step}/inference/detections.jsonl  ──→ inference.read_detections → 离线 OfflineRunner
```

| 位置 | 旧 → 新 |
|------|---------|
| `ClientQueues` | `ca_features` / `append_ca_features` / `drain_ca_features` / `frames_dropped_features` → `*_detections`；`_latest_inference` / `set_` / `get_latest_inference` / `_inference_lock` → `*_detection` |
| `RecordingService` | `submit_features` / `_FeatureJob` / `_feature_queue` / `_claimed_features` / `_write_features` / `_forget_features` → `*_detections`；`_DetectionJob.features` → `frames`；线程名 `recording-features` → `recording-detections`；任务 label `feat:` / `forget-feat:` → `det:` / `forget-det:` |
| `app.storage.inference` | `append_features` / `read_features` → `append_detections` / `read_detections`；`FEATURES_NAME` → `DETECTIONS_NAME`；`_feature_to_record` / `_record_to_feature` → `_frame_to_record` / `_record_to_frame` |
| 落盘 | 文件 `features.jsonl` → `detections.jsonl`；行内键 `"features"` → `"detections"`，其余键不变 |
| 对外可见 | `frame_drop_total{reason="feature_backpressure"}` → `"detection_backpressure"`；admin `queue_depths["ca_features"]` → `["ca_detections"]` |
| 测试 | `test_client_features_buffer.py` → `test_client_detections_buffer.py`（`git mv`），用例 / 类名同步 |

### 落盘改名不做兼容读

`{step}/inference/` 布局只在 dev 分支（2026-09-22 起），未合入 main，已部署机器上只有旧平铺 `{step}/features.jsonl`——按既定政策它们本就不迁移、随 TTL 消失。所以现在改名不会让任何生产数据读不回；合入 main 后再改就得兼容读。

### 保留项

- 算子与离线里真正的特征：`features` 张量、`_adapt_to_features`、`feature_names` / `feature_version` / `FEATURE_VERSION` / `build_base_features` / `FeatureSequence`。
- `push_detection` / `get_slide_window`、启动埋点 `first_inference` 不动。
- `docs/kb/` 与历史 `docs/update/` 不回溯改写。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `feature` 一词在在线链路 | 指检测结果，也指算子里的特征张量 | 只指算出来的特征 |
| 落盘产物命名 | `features.jsonl` / `facts.jsonl` | `detections.jsonl` / `facts.jsonl`（L1 / L3 对仗） |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 866 passed |
| 断言改动 | 仅 recording 任务 label 两处（`feat:` → `det:`，label 本身被改名） |
| 误伤检查 | `feature_names` / `feature_version` / `build_base_features` 命中未变 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| dev / test 机上已有的 `{step}/inference/features.jsonl` | 读侧不再可见，离线对这些 step 读到空序列 | 按 TTL 自然消失；要保留某个 step 就手动改名文件并把行内 `"features"` 键改为 `"detections"` |
| metric 标签与 admin 键改名 | 外部看板 / 告警规则若引用旧名会断 | 仓库内无引用；合入 main 前确认线上看板 |
