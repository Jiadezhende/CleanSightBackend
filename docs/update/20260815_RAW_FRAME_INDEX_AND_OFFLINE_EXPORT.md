# raw 帧索引 sidecar + 离线特征导出器骨架（R0 打通）

> **变更状态**：生效中（2026-08-15）
> **知识库**：待沉淀
>
> 需求定稿见 [离线特征融合实验需求](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md)。本次落地其中的**前两阶**：S1 帧索引 sidecar、`offline/export/` 独立空间 + R0 recipe。视觉分支（帧源解码 + backbone + R1a/R1b/R2）与实验平台可视化不在本次范围。

## 概述

- **改了什么**：① raw 段落盘时增写逐帧 ts 索引 sidecar，使 `features.jsonl` 的 `ts` 能精确反查到段内第几帧；② 在 `offline/` 下新建 `export/` 子包作为离线特征导出器的独立空间，打通 R0（bbox-only）端到端产出模型输入样例。
- **为什么改**：CLEAN 离线只有 bbox 派生特征，要验证「加视觉特征是否有增益」，前提是 RGB 帧与检测框能对齐到同一帧。而 raw 段是以 `eff_fps = (N-1)/span` 反推的**合成 CFR** 写入的，逐帧真实 ts 写完即丢，段内定位只能按平均帧率近似。
- **影响面**：`persistence/strategies/` 新增一个模块 + `hls_strategy` 插一行；`inference/offline/` 新增 `export/` 子包 + `impl/clean.py` 加一个薄封装。**不改** `features.jsonl` 契约、**不改**在线推理链路、**不改** 6 类动作标签集、**不新增任何配置项**。

## 改动详情

### 1. `app/services/persistence/strategies/raw_frame_index.py`（新增）— 逐帧索引 sidecar

三个模块级函数：`build_frame_index` / `read_frame_index`（一对逆运算紧挨放置，同 [feature/store.py](../../app/services/inference/feature/store.py) 风格）+ `write_frame_index`。

sidecar 与段同目录同名换后缀：`raw_segment_{ts_us}.mp4` → `raw_segment_{ts_us}.idx.json`，内容**只有** `{"frame_ts": [...]}`。

反查链路：`ts → 定位段 → 在 frame_ts 里二分得 ordinal → 顺序解码取第 ordinal 帧`。

> **刻意不记 `eff_fps` / 段时长 / timescale。** 那些是容器层参数，正被 [HLS 时基修复](20260813_HLS_SEGMENT_TIMESCALE_FIX.md) 与 [墙钟时间轴](20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md) 重写；记下来就是第二份会漂移的真源。读侧取帧一律「顺序解码数第 i 帧」，不靠 PTS/`eff_fps` 反算 —— 于是段内是合成 CFR 还是真实 PTS、时基怎么修、封段触发怎么改，本映射都成立。

其他约定：只对 raw 轨写（取证职责在 raw）；`tmp + os.replace` 原子替换；best-effort（写失败只告警不抛，落盘主链路可用性优先）；无索引/损坏时 `read_frame_index` 返回 `None`，调用方据此判为**不可精确取帧**，而不是退化成近似反推。

### 2. `app/services/persistence/strategies/hls_strategy.py` — 插一行调用

`_persist_raw_segment` 内、`out_raw.release()` 之后：

```python
write_frame_index(raw_segment_path, frames)
```

> **刻意在目录锁之外。** 锁内那三段（transcode + playlist append + metadata）正是上述两篇改造要重写的部分；sidecar 是段私有文件，与它们无竞争、不参与 tfdt 累计，没有进锁的理由。独立模块 + 一行调用把 merge 冲突面压到最小。

转码失败时段不进 playlist 但 sidecar 已写 —— 刻意如此：读侧靠「段是否在 playlist」判定可取帧，sidecar 存在不代表帧可取。

### 3. `app/services/inference/offline/export/`（新增子包）— 导出器独立空间

offline 段的**第二条管线**（第一条是 `runner → segment → FactLedger`）：那条产事实，这条产模型输入。两条同吃稳定存储键 `(task_id, step_id)`、同不接 client/CQ/DB，产物与消费方不同，故各自独立编排、独立入口。

```
offline/
├── segmenter.py / runner.py / cli.py    # 既有：分割管线（未改动）
├── impl/clean.py                        # 既有 + 本次新增 export_r0
└── export/                              # 新增：纯框架，零业务知识
    ├── models.py    ExportSpec / VisualFrames / ExportQuality / ExportResult
    ├── runner.py    编排 + 产物落盘
    └── cli.py       手动入口
```

**recipe 统一签名**：`(frames: Sequence[FrameFeature], visual: Optional[VisualFrames]) -> ModelInput`。

这条签名是关键接缝：`export/` 完全不认识某业务的特征列名，业务 recipe（住 `offline/impl/<业务>.py`）完全不认识 HLS/sidecar/backbone。recipe 由 `ExportSpec.recipe` 的**全限定路径**经 `importlib` 取用，与 [StageFactory](../../app/services/inference/stage_factory.py) 取 `offline.class`、CLI `--strategy` 完全同款。因此同一份 recipe 既产训练样例、又做将来融合 Segmenter 的线上特征转换 —— **单一真源是结构性保证，不是纪律要求**。

`VisualFrames` 的 `deep`(stride-32) / `shallow`(stride-8) 双层壳已立，本次恒为 `None`；传 `--backbone` 会显式抛 `NotImplementedError` 而非静默降级。

### 4. `app/services/inference/offline/impl/clean.py` — 新增 `export_r0` + 三个兜底常量

`export_r0` 是**薄封装**，直接转调既有 `build_base_features`，不复制一行特征工程。顺带把 `fps=7.5 / 640 / 480` 三个兜底默认值提成模块级常量 `_DEFAULT_FPS` / `_DEFAULT_FRAME_WIDTH` / `_DEFAULT_FRAME_HEIGHT`，供 Segmenter 构造默认值与导出 recipe 共用一处（此前只存在于 `_CleanTorchSegmenter.__init__` 签名里，加导出入口后会变成两份）。

### 5. 产物落点：不新增配置项

```
{storage_base_dir}/.offline_exports/{task_id}/{step_id}/{recipe}@{backbone}/
    input.npz        features [T,F] + timestamps [T]
    manifest.json    recipe/backbone/feature_version/feature_names/frame_count/ts 范围/质量统计
```

默认路径由 `settings.storage_base_dir` 派生（模块级常量 `_EXPORT_SUBDIR`），CLI `--out-dir` 覆盖 —— 导出器是手动跑的实验工具，CLI 参数就是它的旋钮。与 [ClipBuilder](../../app/services/lab/clip_builder.py) 把 `temp_root` 默认成 `{base_dir}/.lab_exports` 同款约定。

> **两个 TTL 陷阱，都已规避并有测试守住**：
> 1. 产物**不能**落在 `{base}/{task_id}/{step_id}/` —— 那里受 `cleanup_days`（默认 7 天）回收；
> 2. manifest **绝不可命名为 `metadata.json`** —— [StorageCleanupWorker](../../app/services/persistence/workers/cleanup_worker.py) 按 `{base}/*/*/metadata.json` 判定过期 step 目录并 `rmtree` **整个目录**，重名会让导出产物在 TTL 到期时被静默删掉。

### 6. `export/cli.py` — 设备隔离可配

与离线分割 CLI 取向一致但**不照抄**：那边硬置 `CUDA_VISIBLE_DEVICES=""` 永不碰 GPU；这边 `--device cpu`（默认）同样禁 GPU 不抢在线资源，`--device cuda` 时不置。隔离仍须在**任何 torch import 之前**生效，故 runner/recipe 的 import 全放在 `_isolate()` 之后。

## 保留项（刻意不动）

- `features.jsonl` 契约、在线推理链路、6 类动作标签集；
- processed 轨落盘（不写 sidecar）；
- 既有 `offline/runner.py` / `cli.py` / `segmenter.py` 三件套 —— 导出器不复用也不修改它们；
- `inference_config.yaml`：导出器是手动实验工具，不进生产配置。

## 验证

| 项 | 结果 |
|----|------|
| 新增 `tests/test_offline_export.py` | 32 passed |
| 全量 `pytest tests/` | **452 passed**（此前 420，新增 32，无回归） |
| R0 真实数据端到端 | `database/1785995202505/2`（1886 帧）→ `input.npz` 105KB：`features (1886,113) float32`、全有限值、`timestamps` 严格递增、`frame_count` 与 `features.jsonl` 行数一致、`feature_dim == len(feature_names) == 113` |
| sidecar 真实落盘路径 | 直调 `persist_segment` 跑通 `cv2.VideoWriter` + ffmpeg 转码：sidecar 与段并列落盘，**非等间隔原始 ts 被合成 CFR 抹掉后仍原样留存**，键集恰为 `{frame_ts}` |
| 单一真源自证 | 测试断言 `export_r0(frames)` 与 `build_base_features(frames)` 逐值相等 |

## 后续计划

1. `export/frame_source.py`（sidecar → ordinal → HLS demuxer 顺序解码，复用 [ClipBuilder](../../app/services/lab/clip_builder.py) 的临时 m3u8 + `EXT-X-MAP` 手法）+ `export/backbone.py`（单次前向同时吐 stride-8 / stride-32 两层）+ R1a/R1b；
2. R2 手部 RoIAlign token，K=2（实测覆盖 99.7% 帧）；
3. 实验平台：lab 页面改名 + 离线实验区 + 清洗时间线可视化；
4. 实验有结论后再写融合 Segmenter。

> 注意：sidecar 只对**本次改动落地后**新产生的数据有效。存量 step 无精确帧对齐，按需求文档 §2.3 一律不迁移、不修复；新数据用 `integration_tests/test_single_client.py --video_path` 推视频现产。
