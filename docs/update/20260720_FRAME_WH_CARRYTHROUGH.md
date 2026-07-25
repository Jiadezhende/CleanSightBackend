# 帧分辨率从 metadata 提升为帧级透传 wh

> **变更状态**：生效中（历史记录，本次维护确认）
> **知识库**：已沉淀 → [kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)(2026-07-21)

> 日期：2026-07-20　依据：代码改动　承接：[20260720_FEATURE_STORE_FRAMEFEATURE_IO](20260720_FEATURE_STORE_FRAMEFEATURE_IO.md)

`frame_shape`（帧分辨率）是 **fan-out 之前就定死的每帧输入常量**（一个 `DetectionTask` 扇给 stage 内 N 个模型，各流看同一张图），却被当作检测器输出逐个塞进 `FrameDetections.metadata`，落盘时再用 `_extract_frame_wh` 从任一检测器抠回、回读时还原成另一个名字 `frame_width/frame_height`——一个每帧常量穿过 fan-out 复制 N 份再收敛回 1 份，且 online 读 `frame_shape`、offline 读 `frame_width`，两处消费不对称。

本次让分辨率沿**每帧轴**透传：pool 在帧还活着时盖一次章 → `FrameInference` → 写回口物化进 `FrameFeature` → 落盘/回读随 record 走。**这取代了上一篇里 `_extract_frame_wh` + metadata 回填 `frame_width/height` 的做法。**

约定：内存契约拆成 **`frame_width: Optional[int]` + `frame_height: Optional[int]`** 两个显式字段（不用 `(w,h)` 元组，避免隐式序混淆——本仓库已有 frame_shape(H,W,C)/wh(W,H)/frame_width 名义打架的前车之鉴）；缺省 None → 消费方走默认兜底。磁盘同样用命名键 `frame_width/frame_height`（不再用位置数组 `wh`），全链路无位置约定。

顺带把 store 的落盘/回读收成一对**对称映射** `_feature_to_record`/`_record_to_feature`（紧挨放置、互为逆），`append`/`load` 只做缓冲与扫描、委托它们做字段映射。磁盘 record 明确是 FrameFeature 的**精简投影**：只落离线必要信息（ts + 每源 bbox/conf/cls + 分辨率），**刻意不落** mask/keypoints（重）与 metadata/success/error（离线不消费），回读按契约默认还原。

## 改动

### 契约加透传字段
- [detection.py](../../app/domain/detection.py) `FrameFeature` + `frame_width`/`frame_height`（帧级分辨率）。
- [models.py](../../app/services/inference/models.py) `FrameInference` + `frame_width`/`frame_height`（pool 盖章、随传输消息透传）。
- `FrameDetections` **结构不动**——`metadata` 继续装真·检测器级数据（`model`/`error`/`mean_brightness`）。

### 盖章 / 物化
- [pool.py](../../app/services/inference/detection/pool.py) 构造 `FrameInference` 时 `frame_width=frame.shape[1], frame_height=frame.shape[0]`——唯一采集点（原始帧此后即销毁）。
- [service.py](../../app/services/inference/detection/service.py) 写回口 `FrameFeature(..., frame_width=res.frame_width, frame_height=res.frame_height)`。

### 检测器停产 frame_shape
- [detector.py](../../app/services/inference/detection/detector.py)、[workflows/mock.py](../../app/services/inference/workflows/mock.py)：metadata 去掉 `frame_shape`。

### 消费端改读帧级分辨率
- online [workflows/clean.py](../../app/services/inference/workflows/clean.py) `_adapt_to_features`：分辨率从「by_source 内逐 source 读 `frame_shape`」改成「`for aligned` 顶部读一次 `aligned.frame_width/height`」，缺失/非法则该帧留全零行。
- [store.py](../../app/services/inference/feature/store.py)：落盘/回读收成对称映射 `_feature_to_record`/`_record_to_feature`（磁盘命名键 `frame_width/frame_height`，不再灌 metadata）；**删 `_extract_frame_wh`**。
- offline [offline/segmenters/clean.py](../../app/services/inference/offline/segmenters/clean.py) `_frame_size`：读 `frame.frame_width/height`，缺失回退默认。

## 边界 / 不动
- 磁盘键从位置数组 `wh` 改成命名 `frame_width/frame_height`：features.jsonl 是 `{task_id}/{step_id}/` 工作目录下随 step **TTL 回收**的临时件、且每 run 重生，故不做迁移；极端情况（run 跨部署、旧 `wh` 行被新码读）→ 该行分辨率缺失 → offline 默认兜底（无 crash，仅该 step 用默认尺寸归一化）。该默认兜底非冗余：离线 segmenter 的可配置默认分辨率（`stage_factory` **params）与无分辨率的测试数据都走它。
- online（6 维）与 offline（113+ 维）两条特征管线仍刻意分离，本次只统一分辨率来源为 `FrameFeature.frame_width/height`（消解上一篇 `frame_shape`/`frame_width` 名不一致）。

## 测试
- [tests/factories.py](../../tests/factories.py)：`make_frame_feature`/`make_frame_inference` + `frame_width`/`frame_height` 参数。
- `test_offline_pipeline` 的分辨率往返：断言 `FrameFeature.frame_width/height` 而非 `metadata.frame_width/height`。
- `test_pool_ts_anchor`：断言 pool 从 `(8,8,3)` 帧盖章 `(fi.frame_width, fi.frame_height) == (8, 8)` 并透传到 FrameFeature。
- `pytest tests/` **335 passed**。
