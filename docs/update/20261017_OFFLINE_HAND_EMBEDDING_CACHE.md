# 离线策略中间产物缓存落 run 的 inference 域，C7 手部嵌入重跑直接复用

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

inference 域新增 `cache_{name}.npz`（`read_offline_cache` / `write_offline_cache`，路线 C 整体替换），`OfflineContext`
加 `load_cache` / `save_cache`。`CleanC7Segmenter` 把手部嵌入存进 `cache_hand_embeddings.npz`，同一 run 重跑时
手与骨干口径都一致就跳过取帧和 DINOv2。示例视频重跑 14.9 s → 0.6 s。

## 变更背景

- **现状**：C7 每次运行都重新解码 raw 录像、裁剪、过 DINOv2，这部分约占耗时 90%（10 分钟 step：GPU 限速 150/s 约 2 min，CPU 约 20 min）。
- **触发**：上线权重是折权重，训练侧全量重训到货、或分类器更新后，历史 run 都要重跑；人定手部嵌入要落缓存。
- **承接**：建立在 [20261017_OFFLINE_CLEAN_C7.md](20261017_OFFLINE_CLEAN_C7.md) 之上。

## 方案详情

### 全景

```text
CleanC7Segmenter._embed(ts, hands, context)
  key = 骨干名 | dinov2 权重 sha256 | 裁剪口径（scale/size/水印区域）
  cached = context.load_cache("hand_embeddings")          ← Runner：inference.read_offline_cache；读坏了 → None + WARNING
  ts、框逐行相等 且 key 相等 → 直接返回 cached["emb"]
  否则 取帧裁剪 + DINOv2 → context.save_cache(...)          ← Runner：inference.write_offline_cache；OSError → WARNING
```

| 部件 | 落在哪 |
|------|--------|
| `cache_{name}.npz` 读写、命名校验、拒绝对象数组 | [`app/storage/inference/_cache.py`](../../app/storage/inference/_cache.py)、`_layout.cache_name` |
| context 注入与降级 | [`OfflineRunner._context` / `_load_cache` / `_save_cache`](../../app/services/inference/offline/runner.py) |
| C7 的键与命中判定 | [`clean_c7/segmenter.py`](../../app/services/inference/offline/impl/clean_c7/segmenter.py) `_embed` / `_cache_matches`，[`hand_state.backbone_key`](../../app/services/inference/offline/impl/clean_c7/hand_state.py) |

### 关键约定

- **存储层不认识内容**：键、dtype、有效性都归策略；盘上只许数值与定长字符串（写时拒对象数组，读时 `allow_pickle=False`）。
- **有效性靠内容核对，不靠时间**：缓存存每行手的 `ts` 与框，读回后逐行比对；run 结束后 detections 不再变，所以只有骨干权重或裁剪口径变了才会重算。
  batch 大小不进键（只造成 fp16 末位差异，见 C7 记录的核对表）。
- **缓存永远不让作业失败**：读坏了按未命中重算覆盖；写失败只告警。run 已被回收时，后面写事实那一步照常判 `reclaimed`。
- **清理**：文件在 `{task}/{step}/{run}/inference/` 下，随 run 目录 TTL 整 step 回收，不另加清理逻辑；重跑原子覆盖。
  缺帧失败的运行不写缓存。

| 产物 | 大小 | 是否落盘 |
|------|------|---------|
| 手部嵌入 `cache_hand_embeddings.npz` | 示例 72 s 视频 3.13 MiB（10 分钟 step 约 26 MiB） | 落 |
| 解码帧 / 裁剪图 | 裁剪图 10 分钟约 2.5 GB | 不落，流式处理 |
| 手部状态概率 / 106 维特征 | 0.3 / 3.6 MiB，重算 20 ms / 6 s | 不落 |

## 变更效果

| 项 | 结果 |
|----|------|
| 真实 Runner 全链路重跑（示例视频，raw HLS + GPU） | 首跑 14.9 s → 重跑 0.6 s，`label_probs` 与首跑逐位一致 |
| 新增用例 | 存储往返 / 覆盖 / 缺失 / 非法名 / 损坏 / 对象数组 / 回收后写；context 降级；C7 命中跳过取帧与骨干、骨干或框变了重算覆盖、缺帧不写缓存、无手不碰缓存 |
| 全量 `pytest tests/` | 1065 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 每次运行都对 dinov2 权重（88 MB）算一遍 sha256 | 约 0.2 s | 可忽略 |
| `query_has_offline_results` 不把缓存算作离线结果 | 有意为之：缓存不是结果 | 无 |
