# `FrameInference` 并入 `FrameDetection`，`DetectorOutput.detections` 改名 `boxes`，`DetBox` 删死字段

> **变更状态**：生效中（2026-09-25）
> **知识库**：待沉淀

## 概述

推理 collector 直接组装 `FrameDetection`（新增 `cq` 字段），写回口取走 `cq` 后置 None、同一对象分发给帧窗 / 快照 / 落盘缓冲；`app/services/inference/types.py` 的 `FrameInference` 删除。`DetectorOutput.detections` → `boxes`；`DetBox` 删掉无人写读的 `mask` / `keypoints`，domain 检测契约不再依赖 numpy。落盘格式不变。

## 变更背景

- **现状 / 痛点**：`FrameInference`（collector → 写回口的传输消息）与 `FrameFeature`/`FrameDetection`（写回口物化的留存态）是同一份数据的两个形态，只差一个 `cq`，字典字段却一个叫 `detections`、一个叫 `by_source`。`FrameInference.stage` 构造后无人读，`task_id` 只用于日志、与 `cq.task_id` 同值。`DetectorOutput.detections` 与上一层的 `detections` 撞词，嵌套访问写成 `fd.detections`。`DetBox.mask` / `keypoints` 无任何检测器写入、无任何消费方读取（可视化读的是 `RenderItem.mask`），落盘还会静默丢掉。
- **承接**：建立在 [20260924_DETECTION_TYPE_RENAME.md](20260924_DETECTION_TYPE_RENAME.md) 的三型改名之上。

## 方案详情

### 全景：一帧检测结果的生命周期

```text
StageWorker / RemoteInferProxy collector
  └─ FrameDetection(ts, by_source, frame_width, frame_height, cq=<run 句柄>)
       │
       ▼ DetectionService._write_back_results
  cq, frame.cq = frame.cq, None          ← 句柄只活在这一段
  cq.is_active()? 否 → stale_run 计数丢弃
       │
       ├─ cq.push_detection(frame)        帧窗
       ├─ cq.set_latest_inference(frame)  快照
       └─ cq.append_ca_features(frame)    落盘缓冲（同一对象，不复制）
```

| 改动 | 落在哪 |
|------|--------|
| `FrameDetection.cq: Optional[Any] = field(default=None, repr=False, compare=False)` | `app/domain/detection.py` |
| `FrameInference` 删除 | `app/services/inference/types.py` |
| collector / 进程内路径直接组装 `FrameDetection`；`_Pending` 删 `task_id` / `stage` | `detection/infer_proxy.py`、`detection/stage_worker.py` |
| 写回口取走并清空 `cq`，日志改读 `cq.task_id` | `detection/service.py` |
| `DetectorOutput.detections` → `boxes` | 全部检测器 / 算子 / 存储 / 测试 / skill 模板 |
| `DetBox` 删 `mask` / `keypoints`，保留 `extra` | `app/domain/detection.py` |
| 测试工厂：`make_detection` / `make_frame_detections` / `make_frame_feature` / `make_frame_inference` → `make_det_box` / `make_detector_output` / `make_frame_detection`（带 `cq` 参数） | `tests/factories.py`、`tests/conftest.py` |

### 方案选型

| 方案 | 结论 |
|------|------|
| 合并，`cq` 常驻不清 | 否：帧窗里的帧反向持有 cq 成引用环，离线 / 快照读者能拿到过期句柄 |
| 合并，写回口清空 `cq`（采用） | 句柄生命周期与原 `FrameInference` 一致，留存态与原 `FrameFeature` 一致 |

- `cq` 标 `Any`：domain 不得依赖 services 的 `ClientQueues`。`compare=False` 让往返等值断言不受句柄影响。
- `DetBox.extra` **保留**：`DEVELOPMENT.md` 与 `infer-workflow` skill 把它定为单框派生量的扩展口，虽然目前只有 Mock 写入、无人读取。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 一帧检测结果的类型 | `FrameInference` + `FrameFeature` 两个 | `FrameDetection` 一个 |
| 嵌套访问 | `frame.by_source[s].detections` / `res.detections[s]` | `frame.by_source[s].boxes` |
| 写回口每帧分配 | 新建一个 `FrameFeature` | 零分配，复用 collector 的对象 |
| `app/domain/detection.py` 依赖 | numpy | 纯 stdlib |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 866 passed |
| 断言改动 | 仅两类：`FrameInference` 的字段名改为 `FrameDetection` 的字段名；落盘有损用例去掉已删除的 `mask` / `keypoints`。另补一条「写回后 `frame.cq is None`」 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 写回口原地改 `frame.cq` | collector 之外若还有人持有同一对象，会看到 cq 变 None | 当前 collector 组装后即交出、不留引用；新增持有方须知悉 |
| 以后接入分割 / 姿态模型 | 需要把 mask / keypoints 加回 `DetBox` | 届时同时决定落盘策略，别再静默丢 |
| 链路上 `features` 命名（存储 / 队列 / recording / 落盘文件） | 仍名实不符 | 下一批 |
