# CLEAN 离线接入新基准 mstcn2-role-embed（手部角色化 MS-TCN2），设为默认离线模型；推理物料改为一个模型一个目录

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

新增离线策略 [`CleanMSTCN2RoleEmbedSegmenter`](../../app/services/inference/offline/impl/clean_mstcn2_role_embed/segmenter.py)，接入训练侧交付的
`role-hands-c7-mstcn2-v41`：检测框 + 手部裁剪（DINOv2 + 手部状态分类器）→ 106 维特征 → MS-TCN2 → pp20。
CLEAN（step `2`）`offline.class` 切到该类。物料按新规范放 `app/data/<模型目录>/`。旧策略保留未删。

## 变更背景

- **新模型**：交付包主指标（insert / withdraw 段级 F1@0.25 均值，pp20）79.8，旧基准 70.7。和此前离线模型的关键差别是
  **要原始画面**：每只手按检测框裁剪，过冻结的 DINOv2 ViT-S/14 得到 768 维嵌入，再经 PCA + 逻辑回归分成
  scope / long_brush / short_brush_hat / syringe / none 五类；p(握镜) 决定握镜手 / 送刷手，送刷手速度和角色序槽位进特征。
- **承接**：建立在 [20261017_OFFLINE_CONTEXT_GPU_PIN.md](20261017_OFFLINE_CONTEXT_GPU_PIN.md) 之上（离线子进程钉在线推理那张卡、
  策略经 `OfflineContext.video` 取 run 的 raw 帧）。
- **人定的约束**：离线开 GPU 但限速；和 YOLO 同一张卡；权重一个模型一个目录、只从 data 读；先不管部署。

## 方案详情

### 全景

```text
read_detections(run)                                   15 fps
  │ preprocess(frames, context)
  ├─ resample_by_ts(15, strict) + 640×480 校验
  ├─ detection_rows：每帧每类面积 top-K（hand 2 / 其余 1），类别号升序、类内面积降序，归一化 6 位小数   ← 训练框文件口径
  ├─ 有手的帧：context.video(640×480, [首, 末]) 按 ts == 取帧 → 水印涂灰 → 1.25 倍方形裁剪 → 224
  │     → DINOv2（GPU fp16，batch 16，限速 max_crops_per_s）→ [N,768] fp16       缺任何一帧 → ValueError，不跳手
  ├─ HandStateClassifier(hand_state.npz) → [N,5]
  └─ build_features → [T,106]
  │ segment
  ├─ MS-TCN2（CPU，整段一次前向）→ softmax [T,6] → label_probs.npz（后处理前）
  └─ argmax → pp20（短于 20 帧并入相邻较长段）→ TemporalSegment（跳过 idle，conf = 段内该类概率均值）
```

| 部件 | 落在哪 |
|------|--------|
| 策略类、取帧裁剪编排、分段解码 | [`clean_mstcn2_role_embed/segmenter.py`](../../app/services/inference/offline/impl/clean_mstcn2_role_embed/segmenter.py) |
| 检测 → 训练口径检测行 | [`clean_mstcn2_role_embed/frames.py`](../../app/services/inference/offline/impl/clean_mstcn2_role_embed/frames.py) |
| 裁剪、DINOv2 加载与限速嵌入、分类器 | [`clean_mstcn2_role_embed/hand_state.py`](../../app/services/inference/offline/impl/clean_mstcn2_role_embed/hand_state.py) |
| 特征（bundle / 角色 / 槽位 / 手部状态） | `clean_mstcn2_role_embed/{features,bundle,roles,slots}.py` + `features_ama/` |
| 列序与类序契约 | [`clean_mstcn2_role_embed/contract.py`](../../app/services/inference/offline/impl/clean_mstcn2_role_embed/contract.py) |
| 网络（结构写死）与 pp20 | `clean_mstcn2_role_embed/{model,postprocess}.py` |
| 配置 | [`config/inference_config.yaml`](../../config/inference_config.yaml) step `2` offline |

### 1. 移植方式：参考实现原样复制，只改输入边界

`clean_mstcn2_role_embed/` 下除 `segmenter.py` / `frames.py` 外均复制自交付包 `c7ref/`，只做了这些改动：

- `features_ama/clean_bbox_v2._read_object_arrays`：读框文件 → 收已解析的逐帧检测行（参数名 `frame_paths` → `frame_rows`）；
- `features.py` / `bundle.py`：同上换入参；列名从 `feature_columns.json` 改为 `contract.FEATURE_COLUMNS` 常量；
- `hand_state.py`：去掉 `cv2.VideoCapture` 读视频，拆成 `mask_overlay` / `square_crop` / `embed_crops`（加限速）；
  DINOv2 从本地骨干目录建网络（`pretrained=False`）再 strict 载权重，不读 torch.hub 缓存；
- `model.py`：不读 `.pt.json` 旁挂文件、不校验 sha，结构超参写死在 `STRUCTURE`；类名挪到 `contract.py`。

特征计算一行未动：任何数值变化都要换特征版本并重训。

### 2. 权重规范：一个模型一个目录

| 规则 | 内容 |
|------|------|
| 根 | 只认 `${CLEANSIGHT_MODEL_PATH:./app/data}`；不联网下载、不读 torch.hub 缓存 |
| 组织 | 一个模型一个目录 `<业务>-<online\|offline>-<模型>-<数据版本>/`，目录内文件名由策略类写死，YAML 只给目录；共享骨干放 `backbones/<名>/`，YAML 单独给路径 |
| 格式 | `.pt` 须 `torch.load(weights_only=True)` 可读；`.npz` 须 `allow_pickle=False` 可读；结构写死、strict 加载；不读旁挂 `.json`；后端不做格式转换 |
| 来源 | 选用哪个折 / 种子由人定，改名放进目录；来源记在 update 记录里（下表） |
| 存量 | 现有扁平文件（`clean-large-best.pt` 等）不动，各自被替换时再按本规范放 |

本次物料（`.gitignore` 新增 `app/data/*/`）：

| 文件 | 来源（交付包 `role-hands-c7-mstcn2-v41`） | sha256 |
|------|------|--------|
| `clean-offline-mstcn2-role-embed/temporal.pt` | `weights/temporal/fold0-seed42.pt` | `7dec83e2…102c9` |
| `clean-offline-mstcn2-role-embed/hand_state.npz` | `weights/hand_state/all.npz` | `2dc194cb…363d02a4` |
| `backbones/dinov2_vits14/` | torch.hub `facebookresearch_dinov2_main` 代码目录 + `dinov2_vits14_pretrain.pth` | 权重 `b938bf1b…60cd9` |

`app/data` 下会有骨干自带的第三方 `.py`，导入规范门禁（`test_import_hygiene`）的 app 源码扫描已排除 `app/data`。

### 3. GPU 开销与限速默认值

实测于 RTX 5060 Laptop 8GB（生产 4090 级卡绝对值不同，比例可参考）：

```text
显存     CUDA 上下文 115 MiB（Windows）+ DINOv2 权重 84 MiB + batch 16 前向 → 进程合计 ≈ 343 MiB（batch 64 ≈ 699 MiB）
速度     DINOv2 fp16 0.82 ms/次（batch 16 已饱和）；CPU 2 线程 68 ms/次；MS-TCN2 9000 帧 CPU 16 ms
共卡     clean-large YOLO batch 4 单独 232 img/s；DINOv2 不限速 → 76–82 img/s；限 300/s → 164；限 150/s → 201（−13%）
         限 150/s 时 batch 16 / 64 对 YOLO 吞吐相同，p95 批延迟 31 / 42 ms → 默认 batch 16
```

默认 `max_crops_per_s: 150`、`batch_size: 16`。10 分钟 step（约 1.7 万次裁剪）的 DINOv2 段约 2 min。

## 变更效果

**与训练侧逐位核对**（示例视频 c5d477f3，一次性核对脚本不入库）

| 输入 | 结果 |
|------|------|
| 训练框文件 + 交付的手部嵌入 → 本实现特征 | 106 列与训练特征**逐位一致**；概率 max\|d\| 4.8e-7，argmax 100% |
| 原视频 → 本实现裁剪 + DINOv2（cv2 解码，batch 64） | 嵌入与训练**逐位一致** |
| 原视频 → ffmpeg bgr24 解码（= HLS 读侧口径）/ cv2 解码，batch 16 | 两种解码结果相同；嵌入 max\|d\| 0.02（fp16 批次差异），模型概率 max\|d\| 3.2e-6，argmax 100% |
| 真实 Runner 全链路：视频写成 raw HLS 段 + 框文件转成像素整数框的 detections，GPU 限速 150 | 14.7 s 完成；5 段与参考实现同标签同顺序，边界差 ≤ 2 帧；逐帧 argmax 一致 99.5%（差异来自 detections 的整数像素框） |

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `tests/test_offline_clean_mstcn2_role_embed.py` | 13 passed（检测行口径、取帧顺序 / 缺帧失败、分段解码、输入契约、参数校验、随仓 YAML 装配） |
| 全量 `pytest tests/` | 1049 passed |
| 集成测试（真实流） | 未跑 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 上线权重 fold0-seed42 只见过 4/5 训练视频，交付包没有全量重训版 | 实际效果可能略低于 79.8 | 请训练侧按同配方在全部 43 个视频上重训，到货后只换目录内文件 |
| 限速默认值按开发机定 | 生产 15 路满载时在线余量未知 | 生产机复测后调 `max_crops_per_s` |
| 每次运行都重算手部嵌入（占耗时约 90%） | 换权重重跑历史 run 时 GPU 开销重复 | 下一批：嵌入缓存落 `inference/` 域 |
| 部署物料（目录与骨干代码）未进 `/deploy` 分发流程 | 新机器缺物料时离线作业失败（不影响在线） | 按人定，本批不管部署 |
| 旧离线策略（`impl/clean.py` 里的 nodep GRU 等）仍在 | 无 | mstcn2-role-embed 定型后单独删除 |

## 追加（2026-10-17）：输入帧率门禁放宽

- **起因**：线上作业失败「输入帧率 12.994fps 低于契约帧率 15.0fps」。原实现对检测帧率做 strict 校验（低于 15fps 的 1% 即失败），
  而线上检测帧率随推理负载波动是常态。
- **改动**：检测帧率（相邻 ts 差的中位数，零星丢帧不影响）≥ 15fps → 按 ts 抽稀到 15；低于 15fps → 原样放行并 WARNING，
  **不设下限、不失败**（人定）。中位帧率计算抽成 [`resample.input_fps`](../../app/services/inference/resample.py)，`resample_by_ts` 行为不变。
- **代价**：模型与特征里的时间窗口按帧计、按 15fps 标定（角色平滑 ±7 帧、送刷手速度 2/5/10/20 帧差分、pp20 = 20 帧）。
  13fps 输入时这些窗口在时间上拉长约 15%、速度类特征按帧换算会偏低约 13%；这次没有评估对段级 F1 的影响。
  线上若长期低于 15fps，应先查在线推理为什么掉帧，而不是继续调低下限。
- **自测**：新增 / 改写用例（12.99 / 5fps 只告警不失败、15fps 无告警、`input_fps` 对零星丢帧稳健）；全量 `pytest tests/` 1068 passed。
- **更正（[20261017_RESAMPLE_RATE_BY_STEADY_MEAN.md](20261017_RESAMPLE_RATE_BY_STEADY_MEAN.md)）**：12.994fps 很可能是 25fps 源的 80/80/40 ms 到达节律被中位数误判，平均帧率其实是 15fps；上面「13fps 会拉长窗口」只在真实欠采样时成立。判据已改为剔缺口平均间隔。

## 追加（2026-10-17）：模型改名 mstcn2-role-embed

- **改动**：后端侧模型名由训练侧代号 C7 改为 `mstcn2-role-embed`。包 `impl/clean_c7/` → `impl/clean_mstcn2_role_embed/`，
  类 `CleanC7Segmenter` → `CleanMSTCN2RoleEmbedSegmenter`（即 producer），物料目录 `clean-offline-c7-v41` → `clean-offline-mstcn2-role-embed`
  （目录内文件不变）。交付包原名（`role-hands-c7-mstcn2-v41`、`c7ref`）是外部名，保留。
- **影响**：已部署机器的物料目录要同步改名；改名前产出的离线分段 producer 仍是 `CleanC7Segmenter`，按 producer 查询时不会命中。
- **自测**：全量 `pytest tests/` 1071 passed。

## 追加（2026-10-17）：同 ts 多帧只留首帧

- **起因**（PR #115 review）：`_compute_embeddings` 按 ts 建字典取帧、算嵌入，同 ts 多帧互相覆盖；`hand_rows` 按行计手。
  两者行数对不上时 `build_features` 报「手部状态概率 N 行，但检测里有 M 只手」，整次失败。
  触发要两个条件同时成立：检测本身有重复 ts，且输入慢于 7.5fps（`resample_by_ts` 的 tol = 半个中位间隔 ≥ 1/15s 才放行重复 ts）。
- **改动**：`preprocess` 在 `resample_by_ts` 之后接 `_dedup_ts`：同 ts 只留首帧，有丢帧时 WARNING；无重复时原样返回。
- **自测**：新增用例（5fps 含重复 ts 经 resample + 去重后 ts 唯一且留首帧、无重复不动）；全量 `pytest tests/` 1117 passed。
