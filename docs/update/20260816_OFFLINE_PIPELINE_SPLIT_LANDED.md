# 离线链路重排落地（P0）：blocks 降为纯工具、Segmenter 自取块、数据壳三合一

> **变更状态**：已落地（2026-08-16）　<!-- 提案见 20260815_OFFLINE_PIPELINE_SPLIT.md，本篇是实施记录 -->
> **知识库**：待沉淀（KB 四处需订正，见文末）
>
> 落地范围 = 提案的 **P0 + P1 的磁盘/缓存部分 + P2**。唯一未做的是 **R1 Segmenter 子类**——它要等训练仓产出对应 checkpoint，没有权重就没有类。

## 一句话

`offline/` 从「分割管线 + 导出管线」两条并行结构，重排成 `blocks`（工具）→ `infer`（策略）→ `runner`（编排）三层；离线侧数据壳从 3 个减到 1 个；顺带修掉一个现网的 O(T × 检测框数) 性能坑。

## 做了什么

| # | 改动 | 效果 |
|---|------|------|
| 1 | **`blocks/` 降为纯工具**：对外只有 `BlockKind` / `load()` / `sweep_cache()` / `NoFeatures` / `load_frames()` | Segmenter 按 `needs` 自取块；编排层不认识块也不碰 FeatureStore |
| 2 | **基类取消 `preprocess`**，只剩 `segment(task_id, step_id)` | D1 解除：融合 Segmenter 要视觉块就在 `needs` 里加一项，没有签名挡路 |
| 3 | **`FeatureBlock` 一个壳吃掉三个**：`VisualFrames` + `ModelInput` + `ExportQuality` 全删 | 离线侧 `@dataclass` 3 → 1（`ExportSpec`/`ExportResult`/`OfflineRunSpec` 一并消失，参数就是 CLI 的 args） |
| 4 | **修 D6**：`_collect_object_boxes` 改按帧装桶，不再给每个检测框分配全长 `[T,5]` 稀疏数组 | 1886 帧 bbox 特征 **6349.8 ms → 16.0 ms（397×）**，峰值内存不再随框数线性增长 |
| 5 | **补两个特征拼装函数**：`bbox_v3_priors`(73) / `bbox_v3_window_priors`(151) | ASFormer 与 BiGRU 的模型输入**从此导得出来**——早先只有 R0/R1，训练仓只能自己另算一份 |
| 6 | **导出改吃 `--segmenter`**（原 `--recipe`） | 导出与推理调**同一个 `build_input`**：导出的字节 = 该模型推理时实际吃的字节 |
| 7 | **`offline/` 目录与 `database/` 平级**，其下只有 `.cache/` 一层；视觉块缓存 + 每次执行自动 gc | 解 D4（导出产物永不回收）；raw 段的 7 天 TTL 碰不到它 |
| 8 | **逐帧预测挪进 `.cache` 并补溯源头** | 解 D5：早先 `offline_inference_result.json` 落在受 TTL 的 step 目录，跑完一周就蒸发且看不出出处 |
| 9 | **mock 降格为测试夹具**（`tests/offline_mock_segmenter.py`），生产配置 `offline` 全部 `{}` | 与「离线只由手动 CLI 触发、在线链路零引用」的实际语义一致 |
| 10 | **1001 行的 `impl/clean.py` 裂成三份** | 特征工程 / 网络结构 / Segmenter 各归其位 |

## 目录与磁盘

```
app/services/inference/offline/
├── cli.py          224  参数与进程：argparse / 设备隔离 / 退出码 / 触发 gc
├── runner.py       219  编排：造 Segmenter → 校验事实 → 幂等写 FactLedger
├── models.py       141  唯一货币壳 FeatureBlock（仅 numpy，import 不拖 torch）
├── diagnose.py     194  特征健康诊断（跨 step 聚合 + 先验契约检查）
├── blocks/              工具：构建 / 加载 / 缓存 / 回收
│   ├── __init__.py 130  BlockKind / load / sweep_cache / NoFeatures / load_frames
│   ├── bbox.py     381  71 维 v3 特征工程（含 D6 修复）
│   ├── visual.py   149  取像素 + backbone 前向 + 缓存
│   ├── cache.py    130  .cache 路径 / npz 读写 / 过期清理
│   ├── frame_source.py 293  ← 原样搬
│   └── backbone.py 212  ← 原样搬
└── infer/               策略：吃块出事实
    ├── segmenter.py 55  基类，只剩 segment()
    └── impl/
        ├── clean.py     435  三个 Segmenter + 特征拼装纯函数 + 解码
        └── clean_nets.py 173 三个 `make_*` 网络结构
```

```
<项目根>/
├── database/                          ← storage_base_dir，受 cleanup_days 7 天 TTL
│   └── {task}/{step}/facts.jsonl      正式结果：SegmentFact（契约完全不变）
└── offline/                       ★  不受 TTL，离线自管（新增 settings.offline_dir）
    └── .cache/{task}/{step}/
        ├── vglobal_{backbone}.npz     视觉块（贵，唯一值得缓存的一路）
        ├── input_{Segmenter}.npz      导出的训练样例 + manifest_{Segmenter}.json
        └── infer_{Segmenter}.json     逐帧 label/conf + 溯源头
```

`offline/` 下**只有 `.cache/` 一层**是刻意的：目录名即语义——此下全部可重建，gc 不需要人判断，因此也不需要内容寻址与引用计数。

## 与提案的两处偏差

1. **runner 没有并进 cli。** 提案写「合并进 cli，实测超过 350 行就拆回」。实测合并后 cli.py **415 行**，超了，按判据拆开——现在 cli 224 行（参数与进程）、runner 219 行（编排）。
2. **`load_frames()` 是块层的公开逃生口。** 规则型/调试型策略要看原始检测框而非 71 维特征（mock 夹具就是），给它一个具名公开口子好过让它去够私有函数。模型型 Segmenter 一律不得走这条路，写进了 docstring。

## 验证

| 项 | 结果 |
|---|---|
| **N6 数值等价** | 两条真实 step（`1785995202505/2` 1886 帧、`99/2` 428 帧）× 三套特征（71/73/151 维），经 `blocks.load → Segmenter.build_input` 产出的矩阵与拆前 `np.array_equal` **全等**；列名 sha、`feature_version`、`spans` 全一致 |
| **D6 提速** | 1886 帧 `6349.8 ms → 16.0 ms`；428 帧 `≈1400 ms → 7.2 ms` |
| **`pytest tests/`** | **465 passed** |
| **CLI 真实回环** | `export` 对 1886 帧 step 产出 151 维 npz + manifest（rc=0）；`infer` 用规则夹具产 9 段写进 FactLedger，`query` 读回（rc=0）。测试用的 `smoke_offline` 事实已从 dev `database/` 清除 |
| **数据壳清点** | 离线侧 `@dataclass`：`FeatureBlock` 1 个新增；`VisualFrames`/`ModelInput`/`ExportQuality`/`ExportSpec`/`ExportResult`/`OfflineRunSpec` 6 个删除。`FetchStats`、`ColumnHealth`/`TagReport`、`RunResult`、`_SegmentPlan` 原样保留 |
| **pyflakes** | 离线包与三个测试文件零告警 |

`infer` 对 CLEAN 模型在本机硬失败（`未配置 model_path`）——**这是设计的正确行为**，离线模型不做规则降级，本地无权重的回环走测试夹具。

## 迁移面

| 影响点 | 状态 |
|---|---|
| `settings.offline_dir` + `offline_base_dir` property | 新增；`.gitignore` 已含 `offline/` |
| `config/inference_config.yaml` | CLEAN 注释里的 class 路径改新包；MOCK 的 `offline` 改回 `{}` |
| `stage_factory.py` 的 `OfflineSegmenter` import | 一行 |
| `tests/conftest.py` | 新增 `tmp_offline` fixture（与 `tmp_storage` 并列） |
| 两个离线测试文件 | 重写，53 + 31 用例 |
| `detection/impl/`、`temporal/impl/` 等 docstring 里的 `offline/impl/<x>.py` 路径 | 已改为 `offline/infer/impl/<x>.py` |
| 存量 `.offline_exports` 产物 | 可直接删（实验中间产物，可重建） |

## KB 待订正（沉淀时处理）

以下四处与代码已不符：

- [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md) L26/27/85/89/92/127：`offline/impl/`、`OfflineRunner`、`preprocess`/`segment` 两段接口、mock 为「真兜底」、`impl/clean.py` 的 v2/113/121/249 维（现为 v3/71/73/151）
- [DESIGN_EXTENDING_DETECTION.md](../kb/DESIGN_EXTENDING_DETECTION.md) L9/67/86：新增 Segmenter 的落点与 `preprocess` 扩展范式
- [ARCHITECTURE_DATA_FLOW.md](../kb/ARCHITECTURE_DATA_FLOW.md) L57/81：`preprocess(streams)` 预留层与 62 维 ModelInput
- [BUSINESS_DETECTION_STANDARDS.md](../kb/BUSINESS_DETECTION_STANDARDS.md) L71、[ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) L26：文件路径与 offline 特征维数

## 下一步

- **R1 Segmenter 子类**：`needs = (BBOX, VGLOBAL)` + `backbone = "yolo"` + `build_input = bbox_v3_visual`，等训练仓的 R1 checkpoint。接它**需要 1 个新类**，不是提案初版说的 18 个。
- **R2（hand token）**：不留接缝，真做时改 `blocks/visual.py` 一个函数 + 加一个 `BlockKind.VHAND` 分支。
- `.cache` TTL 目前默认 30 天（`--cache-ttl-days` 可覆盖），刻意长于 raw 段的 7 天——raw 一过期视觉块就永久不可重建。
