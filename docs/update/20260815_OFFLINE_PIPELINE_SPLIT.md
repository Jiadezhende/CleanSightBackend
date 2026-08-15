# 离线链路重排：blocks 降为纯工具、Segmenter 自取块、数据壳三合一

> **变更状态**：**已落地**（2026-08-16，见[实施记录](20260816_OFFLINE_PIPELINE_SPLIT_LANDED.md)）　<!-- 提案原文保留，供追溯当时的判断 -->
> **知识库**：待沉淀
>
> **落地偏差两处**（详见实施记录）：① runner 未并进 cli——实测合并后 415 行，超过本文 §3.4 自己定的 350 行判据，故拆开；② 块层多一个公开符号 `load_frames()`，给规则型策略看原始检测框用。其余全按本文执行，N6 逐值相等成立。
>
> 承接：[离线导出器接入视觉分支 R1](20260815_OFFLINE_EXPORT_VISUAL_R1.md) 把视觉分支打通后，`offline/` 里形成了**两条各自从 FeatureStore 起算特征的管线**。本篇解决这个结构问题。需求约束仍以[离线特征融合实验需求](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md)为准，本篇不改其中任何一条不变式。
>
> **本篇是改写版。** 初版为解 D1/D3 引入了 6 个新数据壳（`FusionRecipe` / `BlockSpec` / `Extractor` / `RunRecord` / `FeatureBlock` / 块化 `ModelInput`），其中多数替掉的只是**一行代码**——三个 Segmenter 的 `preprocess` override 各只有一行（[clean.py:977](../../app/services/inference/offline/impl/clean.py#L977)、[clean.py:994](../../app/services/inference/offline/impl/clean.py#L994)）。判据已固化进 [CLAUDE.md](../../CLAUDE.md)「技术债治理：控制数据模型数量」，本篇按它重写。

## 概述

- **改了什么**：三条。① `blocks/` 是**纯工具**不是管线阶段——Segmenter 按需自取特征块，编排层不认识块，基类不设 `preprocess`；② **只换 Segmenter**——特征方案与网络结构由 checkpoint 绑死，不拆成两个配置项，配置里始终只有 `class:` 一个旋钮；③ **数据壳只留一个**——`FeatureBlock` 吃掉现有的 `VisualFrames` + `ModelInput` + `ExportQuality`。
- **为什么改**：现在推理路径走 `segmenter.preprocess(frames)`、导出路径走 `recipe(frames, visual)`，两套入口；而 `preprocess` 的签名里**没有 visual 参数**——按当前结构，融合 Segmenter 根本写不出来。
- **影响面**：只在 `app/services/inference/offline/` 内重排 + `config/inference_config.yaml` 的 MOCK stage `offline` 段改回 `{}` + 新增一个 `settings.offline_dir` + 两个测试文件。**不改**在线链路、**不改** `features.jsonl` / `FactLedger` 契约、**不改** 6 类动作标签集、**不新增**任何特征方案。
- **数据壳净变化：−2**（进 `FeatureBlock`；出 `VisualFrames`、`ModelInput`、`ExportQuality`）。

## 一、诊断：六条代码事实

| # | 事实 | 后果 |
|---|------|------|
| **D1** | [runner.py:91](../../app/services/inference/offline/runner.py#L91) 走 `segmenter.preprocess(frames)`，[export/runner.py:132](../../app/services/inference/offline/export/runner.py#L132) 走 `recipe(frames, visual)` | 两套特征入口。`preprocess` 无 visual 形参 → **融合 Segmenter 写不出来**，这是最硬的一条 |
| **D2** | [`_build_visual`](../../app/services/inference/offline/export/runner.py#L152) 里 `deep, _shallow = backbone.forward(batch)`，浅层当场丢弃；`global_pool` 硬编码 | 框架层声称「零业务知识」，实际写死了 R1 的做法。R2 要 hand token 得改 `blocks/visual.py`——**本期不预留接缝**，真做时改那一个函数 |
| **D3** | ~~特征方案 × 网络结构在类上做乘法，接 R1 需 18 个新子类~~ | **伪问题，见 §3.3**：两者由 checkpoint 绑死，真实组合数 = 实际训出的 ckpt 数。接 R1 是**新增 1 个类**不是 18 个 |
| **D4** | `.offline_exports` 不在 `StorageCleanupWorker` 扫描范围（它只扫 `{base}/*/*/metadata.json`） | 导出产物**永不回收**。当初躲开 TTL 是对的，代价是没人管 |
| **D5** | [`offline_inference_result.json`](../../app/services/inference/offline/runner.py#L115) 落在 `{base}/{task}/{step}/` 下 | 反而**受 7 天 TTL**，且无溯源——看不出是哪份特征、哪个 backbone、哪个 ckpt 产的 |
| **D6** | **本次实测新发现**：[`_collect_object_arrays`](../../app/services/inference/offline/impl/clean.py#L189) 给**每个检测框**都 `np.zeros((T, 5))` 建一条全长数组，只写一行 | bbox 特征退化成 O(T × 实例数)。实测见下 |

### D6 实测（`database/1785995202505/2`，1886 帧）

| 项 | 耗时 |
|---|---|
| `build_base_features`（71 维） | **6.2–6.4 秒**（三次稳定复现） |
| `add_business_priors`（+2 维） | 3.9 ms |
| `add_centered_window_stats + add_business_priors`（+80 维） | 27.5 ms |

profile 归因：[`_select_hand_slots`](../../app/services/inference/offline/impl/clean.py#L310) 占 5.7 s，`_as_box5` 被调 **650 万次**。原因是一条 step 里 hand 有 **2661 个检测框 → 2661 条 `[1886,5]` 数组**（光 hand 约 100 MB），而 [clean.py:315](../../app/services/inference/offline/impl/clean.py#L315) 每帧对同一行还调了**两次** `_as_box5`。

三点结论：

1. 这是**现网就有的问题**，不是本提案引入的；
2. 它随 step 时长二次增长——10 分钟 step（4500 帧）按此趋势会到几十秒；
3. **正确处置是修它，不是拿缓存盖住**。修法是按**槽位**建数组而非按实例（每帧最多留 top-k 候选），`_as_box5` 单次调用后复用。修完预计回到毫秒级，于是 §3.1 的「bbox 块不缓存」成立。列入 P0，受 N6「逐值相等」约束。

## 二、边界：本期做什么、不做什么

| 决策 | 结论 | 依据 |
|------|------|------|
| 行为变化 | **纯重构，零行为变化**。验收标准可测：同参数跑出的特征矩阵与拆前**逐值相等** | 一次只动一个变量。结构和特征方案同时变，出问题无法归因 |
| 新特征方案 | **一个都不加**。只补两个「现有模型已在吃、但导出器导不出来」的拼装函数（§3.3） | 同上 |
| 新数据壳 | **净 −2**。`FeatureBlock` 进，`VisualFrames` / `ModelInput` / `ExportQuality` 出 | [CLAUDE.md](../../CLAUDE.md)「控制数据模型数量」 |
| R2（hand token） | **不做，也不留接缝** | 真做时改 `blocks/visual.py` 一个函数即可，预留 `List[Block]` 返回值是为未验证设计提前配套 |
| F1~F3 融合结构 | **本仓一行不写** | 等宽投影是模型侧结构（[需求 §4.4](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md) 已定），在训练仓实现。本仓只出 concat + `spans` 列区间 |
| `fuse` 作为独立阶段 | **不做**，也不作为独立模块存在 | 融合产物是上游块的完全冗余副本；它结构上不可能变贵（禁止拟合统计量，见 N2） |
| 块的内容寻址 / manifest / 引用计数 | **不做** | 缓存 key 就是 `(task, step, backbone)`，直接当文件名。内容寻址是为「gc 反查引用」服务的，而 §3.5 之后**整棵缓存树都可重建**，不需要引用关系 |
| 存量 `.offline_exports` 产物 | **直接删，不迁移** | 实验中间产物，可重建 |
| `gc` 注册进 lifespan | **不做**，每次离线执行时自动跑 | 在线值守节奏 ≠ 实验节奏（同[需求 §5](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md)「不进运维面板」的道理） |
| 并发 / 排队 / 自动触发 | **不做**，沿用手动单次 | 与现有 CLI 一致 |
| 实验平台 UI | **不做**，留下一期 | [需求 §5](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md) 独立一块 |

## 三、设计

```
blocks（纯工具，非阶段）            infer                         编排（cli.py）
  load(kind, task, step)   ──→   Segmenter.segment(task, step)  ──→  校验 → FactLedger
       ↑ 视觉块读写 .cache             ↑ 自己按 needs 取块              ↑ 看不见块
```

### 3.1 `blocks/`：纯工具，不是管线阶段

对外只有三个符号：

```python
class BlockKind(str, Enum):
    BBOX = "bbox"; VGLOBAL = "vglobal"; VHAND = "vhand"

def load(kind: BlockKind, task_id: int, step_id: int, *,
         backbone: str | None = None) -> FeatureBlock:
    """取一块特征。该 step 无特征时抛 NoFeatures。"""

def sweep_cache(ttl_days: int = 30) -> int:
    """每次执行开头调一次，返回释放字节。"""
```

- **kind 用枚举而非字符串 map**：调用点是 `load(BlockKind.VGLOBAL, ...)`，拼错在导入期就炸，不必等运行时 KeyError。
- **视觉块贵（backbone 前向，分钟级）→ 内部读写 `.cache`**；**bbox 块修完 D6 后是毫秒级 → 直算不缓存**。缓存只给真正贵的那一路，不搞通用块存储。
- 块的**构建、加载、缓存、回收全部收在 `blocks/`**。`infer` 侧要落 debug 产物时向它要目录，方向是 `infer → blocks` 单向。

### 3.2 Segmenter 自取块，`preprocess` 这层中转取消

基类回到最小：

```python
class OfflineSegmenter(ABC):
    name: str
    @abstractmethod
    def segment(self, task_id: int, step_id: int) -> List[SegmentFact]: ...
    def debug_result(self) -> Optional[dict]: return None
```

clean 家族的基类自己按需取块、拼输入、前向：

```python
class _CleanTorchSegmenter(OfflineSegmenter):
    needs = (BlockKind.BBOX,)     # R1 子类改成 (BBOX, VGLOBAL)
    backbone = None               # R1 子类填 "yolo:clean-large-best"

    def build_input(self, blocks) -> FeatureBlock:   # 子类一行覆盖（正如今天的 preprocess）
        return bbox_v3(blocks)

    def segment(self, task_id, step_id):
        bl = {k: blocks.load(k, task_id, step_id, backbone=self.backbone) for k in self.needs}
        x = self.build_input(bl)
        ...                        # 前向 + 解码 SegmentFact
```

**D1 不是靠改签名解决的，是那层接口不存在了**：`build_input` 是 clean 家族的实现细节，不是 `OfflineSegmenter` 的抽象方法。融合 Segmenter 想要视觉块，就在 `needs` 里加一项，没有签名挡路。

> 单一真源（[需求 F3](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md)）由此**加强**：Segmenter 拿到的是 `blocks.load()` 的产物，`frames` 根本不在它手上，想自己算一份特征也无从算起。

### 3.3 只换 Segmenter：为什么不拆「特征 × 网络」两个配置项

初版据 D3 打算把二者拆成正交轴。**那个乘法是伪问题**：

- 特征方案与网络结构**由 checkpoint 绑死**。一份 `.pt` 是用某套特征训出来的，[clean.py:720](../../app/services/inference/offline/impl/clean.py#L720) 会拿 `feature_names` 与 checkpoint 里的逐项比对，配错组合直接抛。
- 所以真实存在的组合数 = **实际训出来的 checkpoint 数**，不是笛卡尔积。今天 3 个 ckpt → 3 个类，一一对应；接 R1 是**新增 1 个类**（配它训出来的那个 ckpt）。
- 拆成两个配置项，等于把 18 种组合暴露给配置、其中 15 种必然报错，还把配置摊散到多处。

因此**配置里只有 `class:` 一个旋钮**（现状不变），`backbone` 是 R1 子类的类属性而非配置项——它同样由 ckpt 绑死：

```yaml
offline:
  name: clean_offline
  subscribes: [clean_large, clean_small]
  class: app.services.inference.offline.impl.clean.CleanBiGRUSegmenter
  params:
    model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-bigru.pt
```

特征拼装函数仍是**模块级纯函数**，供 Segmenter 与导出器共用——那是函数复用，不是配置维度。命名从看不出用途的 `recipe` 改为按内容命名，实验编号 R0/R1 只留在文档里不进代码符号：

| 现名 | 新名 | 维度 | 谁在吃 |
|---|---|---|---|
| `export_r0` | `bbox_v3` | 71 | MSTCN-BiLSTM |
| **（缺）** | `bbox_v3_priors` | 73 | ASFormer |
| **（缺）** | `bbox_v3_window_priors` | 151 | BiGRU |
| `export_r1` | `bbox_v3_visual` | 71+C+1 | R1 待训 |

**中间两行是补的真实缺口**：ASFormer 吃 73 维、BiGRU 吃 151 维，而现有导出函数只有 R0/R1——**这两份模型输入今天导不出来**，训练仓要样例只能自己另算一份，正是单一真源要防的漂移。

**导出 CLI 也只认 Segmenter**：`--recipe` 换成 `--segmenter <全限定路径>`，导出器实例化它（不加载权重）后调 `build_input`，在前向之前落盘。于是「导出的训练样例」与「推理实际吃的字节」严格同源，且配置不分叉成两套。

> 「全限定路径寻址」不是新发明：[stage_factory.py:113](../../app/services/inference/stage_factory.py#L113) 的 `offline.class` 与 [export/runner.py:53](../../app/services/inference/offline/export/runner.py#L53) 的 `_load_recipe` 已在用。框架靠 importlib 取对象，因而零业务知识、加载失败 fail-fast——**这也是不需要注册表 dataclass 的原因**。

### 3.4 编排：runner 合并进 cli，且不出现 blocks

```
cli infer --task-id N --step-id M
    ├─ blocks.sweep_cache()
    ├─ 解析 stage 配置 → StageFactory 造 segmenter（未启用 → skipped）
    ├─ facts = segmenter.segment(task, step)      ← 块加载在这层里面，编排层看不见
    ├─ 校验 + 补 producer + 排序（沿用现 _validate_and_stamp，零改动）
    └─ FactLedger.replace_segments(...)           ← 契约完全不变
```

`NoFeatures` 由 cli 捕获翻译成 `skipped`（不覆盖旧事实），保住现有「有数据但结果为空 = completed 并清旧分段」与「无数据 = skipped」的语义差别。

现 `runner.py` 147 行，减去 preprocess 调用与订阅探测后只剩配置解析 + 校验 + 写 ledger，合并进 `cli.py`（现 121 行）后预计 **≈280 行**。**判据：实测超过 350 行就拆回独立 runner**，不硬凑。

**逐帧 label/conf 不是正式结果**——正式结果是 `FactLedger` 里的 `SegmentFact`（下游业务读的稳定契约，位置与格式全不变）。逐帧预测只用于 debug 与训练对比，跑一次就能重建，因此落 `.cache`（解 D5，见 §5 磁盘布局）。

### 3.5 gc：每次执行时自动清 `.cache`

不做 CLI 子命令。`blocks/cache.py` 在每次 infer / export 开头扫 `offline/.cache/`，按 mtime 删过期项，日志记一行释放量。因为整棵树都可重建，**没有需要人判断的例外**——这正是把逐帧结果也放进 `.cache` 换来的简化，也是不需要内容寻址与引用计数的原因。

顺带清 [frame_source.py:224](../../app/services/inference/offline/export/frame_source.py#L224) 的残留 `.export_*.m3u8`（`finally` 里 unlink，但进程被 kill 时不执行）。

> 对「raw 段已不在」的视觉缓存打 WARNING 再删，不静默——raw 一过期该缓存就永久不可重建。TTL 取值见 §十。

## 四、mock：不是兜底，降格为测试夹具

[config/inference_config.yaml:135](../../config/inference_config.yaml#L135) 的 MOCK stage 是**唯一启用的 `offline` 配置**，KB 三处（[SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)、[SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md)、[DESIGN_EXTENDING_DETECTION.md](../kb/DESIGN_EXTENDING_DETECTION.md)）都写它是「离线的真兜底而非脚手架」。**与代码不符**：

- `OfflineRunner` 在 `app/` 里的唯一调用点是 [cli.py:48](../../app/services/inference/offline/cli.py#L48)，**在线链路零引用**——没有会崩的在线路径需要它兜；
- `resolve_stage` 回退 MOCK 确实存在，但只在**有人手动敲 `--step-id -1`** 时走到，那是 smoke 入口不是兜底；
- 配错 `class` 路径时 `create_offline_segmenter` 是 **fail-fast 抛 ValueError**，根本不退 mock。

配置自己的注释其实已说破：「离线是独立进程手动跑，**不会被在线链路自动执行**」。

**处置是降格不是删**——「不依赖 torch 就能验证 `blocks → Segmenter → FactLedger` 回环」这个能力真有价值：

| 对象 | 动作 |
|---|---|
| `impl/mock.py` | 搬进 `tests/`（单一消费者，不进 `factories.py`——那是数据构造真源，这是行为 stub） |
| `config/inference_config.yaml` MOCK 的 `offline:` | 改回 `{}`。**生产配置里离线段全部不启用**，与「只有手动 CLI 跑」的实际语义一致 |
| KB 三处 | 一并订正 |

**保留不动**：`InferenceConfig.resolve_stage` 与在线 `InferenceManager.resolve_stage` 的双实现。两者故意不合并——在线查「有 detector 的活跃 stage」集合、离线查「所有已定义 stage」集合，合并会改在线语义（detector-less stage 会从回退 MOCK 变成恒等命中）。

## 五、目标结构

### 代码

```
app/services/inference/offline/
├── cli.py                  子命令 infer / export（+ 保留 query / diagnose）；编排合并于此
├── models.py           ★  只有 FeatureBlock —— 仅 numpy，import 不拖 torch
├── blocks/                 纯工具：构建 / 加载 / 缓存 / 回收
│   ├── __init__.py     ★  对外三符号：BlockKind / load() / sweep_cache()（+ NoFeatures）
│   ├── bbox.py            ← clean.py:116-418 特征工程纯函数（含 D6 修复）
│   ├── visual.py          ← export/runner.py:_build_visual
│   ├── cache.py        ★  .cache 路径规则 + npz 读写 + 过期清理
│   ├── frame_source.py    ← 原样搬（含 FetchStats）
│   └── backbone.py        ← 原样搬
├── segmenter.py            ← 基类，只剩 segment()
└── impl/                   策略：吃块出事实
    ├── clean.py            三个 Segmenter 子类 + 特征拼装纯函数 + 解码
    └── clean_nets.py       ← clean.py:787-950 三个 `_make_*`
```

★ = 新增，← = 从现有文件搬。`export/` 包整体删除，能力并入 `blocks/` 与 `cli.py`。**1001 行的 `impl/clean.py` 按这套自然裂成三份**——它现在混着特征工程、网络结构、Segmenter、导出函数四种东西，是「乱」的最大单点。不建 `common/` 或 `utils/`（见 N4）。

### 磁盘

```
<项目根>/
├── database/                          ← storage_base_dir，受 cleanup_days 7 天 TTL
│   ├── {task}/{step}/features.jsonl / raw_segment_*.mp4 / *.idx.json
│   ├── {task}/{step}/facts.jsonl      正式结果：SegmentFact（契约不变）
│   └── ✗ offline_inference_result.json   ← 删除（D5：错落在这，7 天蒸发）
└── offline/                       ★  不受 TTL，离线链路独管（原 .offline_exports）
    └── .cache/{task}/{step}/
        ├── vglobal_{backbone}.npz     视觉块（values·ts·valid + 取帧统计写进 npz 头）
        ├── input_{segmenter}.npz      导出的训练样例
        └── infer_{tag}.npz            逐帧 label/conf + 溯源头（segmenter / backbone / ckpt sha8
                                       / feature_version）→ 解 D5
```

`offline/` 下**只有 `.cache/` 一层**，刻意为之：目录名即语义——此下全部可重建、随时可删、gc 不需要人判断。

新增 `settings.offline_dir: str = "offline"` + `offline_base_dir` property，与 [settings.py:170](../../app/settings.py#L170) 的 `storage_base_dir` 同款相对路径解析（避免读写两侧因 cwd 不同而分叉）。

三条不变式沿用不动：产物不落受 TTL 的 step 目录（[需求 F6](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md)）；任何 manifest **绝不命名为 `metadata.json`**（否则该目录会被 `StorageCleanupWorker` 当成 step 目录 rmtree）；`ts` 是唯一 join key（F1）。

## 六、唯一数据壳：`FeatureBlock`

今天离线侧有三个壳在描述同一类东西：

| 现有壳 | 问题 |
|---|---|
| [`VisualFrames`](../../app/services/inference/offline/export/models.py#L28) | 揉两个粒度，命名不一致：`valid: [T]` 与 `hand_mask: [T,K]` 极性相同却一个 valid 一个 mask（见 N7，这是会**静默出错**的坑） |
| [`ModelInput`](../../app/services/inference/offline/impl/clean.py#L81) | 就是一块 concat 后的特征 + 几个标量 |
| [`ExportQuality`](../../app/services/inference/offline/export/models.py#L64) | **是已有 [`FetchStats`](../../app/services/inference/offline/export/frame_source.py#L47) 的冗余重壳**：同一批计数抄一遍（[export/runner.py:100](../../app/services/inference/offline/export/runner.py#L100) 逐字段搬运），且**抄漏了 `decode_short`**——manifest 至今看不到这项缺帧原因 |

合成一个，字段砍到最少：

```python
@dataclass(frozen=True)
class FeatureBlock:
    values: np.ndarray                  # [T, C]（2D）或 [T, K, C]（3D，如 hand tokens）
    names: List[str]                    # 列名，len == C
    ts: List[float]                     # 唯一 join key（需求 F1）
    valid: Optional[np.ndarray] = None  # values 去掉通道维的形状；None = 该块恒有效
    version: str = ""                   # 特征版本，与 checkpoint 对齐用
    spans: Dict[str, List[int]] = ...   # 列区间 {"bbox":[0,71],"vglobal":[71,327]}

    @property
    def frame_count(self) -> int: return self.values.shape[0]
    @property
    def feature_dim(self) -> int: return self.values.shape[-1]
```

逐字段交代**为什么留 / 为什么删**（这一节的存在本身就是「控制数据模型数量」的作业）：

- **`names` 留，有实打实的门禁用途**：[clean.py:720](../../app/services/inference/offline/impl/clean.py#L720) 拿它与 checkpoint 里的 `feature_names` **逐项比对**，不一致直接抛——这是防 train/serve skew 的真门禁；[diagnose.py:101](../../app/services/inference/offline/export/diagnose.py#L101) 也按列名做无效特征诊断。不是装饰。
- **`spans` 留**：训练仓按分支切列做等宽投影要它（= 现有 `ModelInput.blocks`，非新增）。让下游靠列名前缀猜是脆的。
- **`kind` 删**：块由具名枚举参数取（`load(BlockKind.VGLOBAL, ...)`），调用方本来就知道拿的是什么，字段冗余。
- **`fps` 删**：可由 `ts` 反推，[`_effective_fps`](../../app/services/inference/offline/impl/clean.py#L201) 已存在。
- **取帧统计不进壳**：它只有两个消费者——日志与缓存 npz 头，没有内存消费者。直接用已有的 `FetchStats`，不再重壳，顺带把丢失的 `decode_short` 找回来。

> **`valid` 的形状规则是「values 去掉通道维」**，这一条同时解释两种粒度：`vglobal` 的 values 是 `[T, C]` → valid 是 `[T]`（帧级）；将来的 `vhand` 是 `[T, K, C]` → valid 是 `[T, K]`（手级）。粒度不是拍脑袋定的，是形状推出来的。

**不建的壳**：`FusionRecipe`（配方是函数，靠全限定路径寻址，不需要注册表）、`BlockSpec`（缓存 key 就是文件名）、`Extractor` ABC（两个纯函数）、`RunRecord`（溯源写进 npz 头）。

## 七、分期

| 期 | 内容 | 为什么是这个顺序 |
|---|------|------------------|
| **P0** | `models.py` 立起（`FeatureBlock` 替掉三个壳）+ `impl/clean.py` 拆三份 + **修 D6** + 基类去掉 `preprocess`、Segmenter 自取块 + 补两个特征拼装函数 + 导出改 `--segmenter` + runner 并入 cli + mock 降格 | D1 是「融合 Segmenter 写不出来」的唯一堵点，其余各期都建立在它之上。D6 与特征工程搬家同一次做完，避免动两遍同一段代码 |
| **P1** | `offline/.cache/` + `blocks.load` 视觉缓存 + `sweep_cache` + R1 Segmenter 子类落地 | 组合一多，「换 backbone 重跑一遍 backbone 前向」的浪费按组合数放大。这是省算力的唯一结构性手段 |
| **P2** | 逐帧预测从受 TTL 的 step 目录迁进 `.cache`，JSON→npz 并补溯源头 | 解 D5。组合少时可以拖，组合多时当天就是刚需 |

P0 是唯一改变现有 Segmenter 语义的一期，也是风险集中点；P1/P2 都是纯增量。

## 八、迁移面

| 影响点 | 成本 |
|---|---|
| [config/inference_config.yaml:107-110](../../config/inference_config.yaml#L107) 的 CLEAN `offline.class` 路径 | ≈0，**那几行现在是注释**（生产 `offline: {}` 未启用） |
| [config/inference_config.yaml:135](../../config/inference_config.yaml#L135) MOCK 的 `offline` | 改回 `{}` |
| [stage_factory.py:11](../../app/services/inference/stage_factory.py#L11) import 路径 | 一行 |
| `app/settings.py` | 新增 `offline_dir` 字段 + `offline_base_dir` property |
| [tests/test_offline_pipeline.py](../../tests/test_offline_pipeline.py)（509 行 / 37 用例）、[tests/test_offline_export.py](../../tests/test_offline_export.py)（409 行 / 29 用例） | **主要工作量**。要改 import + 适配「Segmenter 自取块」 |
| `export/` 包整体（含 `__init__.py` 对外导出） | 删除，能力并入 `blocks/` 与 `cli.py` |
| KB 四处（SERVICE_INFERENCE / SERVICE_CONFIG / DESIGN_EXTENDING_DETECTION / ARCHITECTURE_DATA_FLOW） | 随沉淀订正，尤其 mock「真兜底」与 `preprocess` 扩展范式两处 |
| 存量 `.offline_exports` 产物 | 直接删 |

## 九、硬不变式

| | 不变式 |
|---|---|
| **N1** | **Segmenter 不得接触 `frames`。** 特征只能来自 `blocks.load()`。这是 [需求 F3](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md) 单一真源的保证——推理拿不到原料，就无从自己算一份 |
| **N2** | 特征拼装函数**只做确定性纯函数变换**，禁止拟合任何统计量（normalizer / PCA）——那属训练侧真源 |
| **N3** | `models.py` 与 `blocks/` 里不得出现 `torch`（`blocks/visual.py` 的延迟 import 除外）。`import` 它们不该拖起 torch（[export/models.py](../../app/services/inference/offline/export/models.py#L15) 已有此约定，扩到整个块层） |
| **N4** | 不建 `offline/common/` 或 `utils/`。顶层 `cli.py` + `models.py` 就是公共层；有 common 就会长出横向依赖 |
| **N5** | 一次执行只驱动一路像素解码。「一次前向出多块」的正确落点是 `blocks/visual.py` 内部一次前向返回多块，不是两个函数共享一条流 |
| **N6** | 重构期间产出与拆前**逐值相等**（含 D6 修复后的 bbox 特征）。任何特征数值变化都属越界 |
| **N7** | **有效性数组一律命名 `*_valid`，语义恒为「True = 真实有效」。** 禁止叫 `*_mask`——PyTorch 的 `key_padding_mask` / `attn_mask` 约定是 **True = 忽略**，极性正好相反 |

### N7 补充：现有命名已经不一致，且埋着一个静默 bug

[export/models.py:52-55](../../app/services/inference/offline/export/models.py#L52) 里同一个概念用了两个名字：

```python
valid: Optional[np.ndarray] = None        # [T]    True = 该帧取到像素
hand_mask: Optional[np.ndarray] = None    # [T, K] True = 该手检到了
```

极性相同（True = 有效），名字一个 `valid` 一个 `mask`。

> **为什么这不只是洁癖**：PyTorch 的 `nn.MultiheadAttention(key_padding_mask=m)` 约定 `m[i]=True → 第 i 个位置被 ignore`，与我们的极性**正好相反**。训练仓若把 `hand_mask` 直接传进去，结果是**屏蔽掉所有真手、保留所有空位**——loss 照常下降，只是学不出东西，**静默出错**。而 F3 的手部 query 交叉注意力正是要用它。
>
> 叫 `*_valid` 则无极性歧义（True = 有效是唯一读法），并逼消费侧显式写一次 `~valid`——那个取反动作本身就是提醒。

`hand_mask` 随 `VisualFrames` 一起删除，统一由 `FeatureBlock.valid` 承载（§六）。

## 十、待确认

| # | 问题 | 倾向 |
|---|------|------|
| 1 | `.cache` 的 TTL 取多少？ | **30 天**。需长于 raw 段的 7 天 TTL——raw 一过期，视觉缓存就永久不可重建，那时它反而更宝贵 |
| 2 | runner 并进 cli 后行数是否可接受？ | 预计 ≈280 行；**实测超过 350 行就拆回独立 runner** |
| 3 | P0 是否连同 `CleanSegmenter` 兼容别名一起清掉？ | 倾向清掉。它只服务「旧文档/旧测试」，本次两个测试文件本来就要重写 |

## 验证

本次只改提案文档，未修改代码、配置或模型。

已跑的实测（支撑 §一 D6 与 §3.1「bbox 块不缓存」）：

```bash
source .venv/bin/activate && python -c "
import time
from app.services.inference.feature.store import FeatureStore
from app.services.inference.offline.impl.clean import build_base_features
frames = FeatureStore('database').load(1785995202505, 2)
t=time.perf_counter(); mi=build_base_features(frames,7.5,640,480)
print('frames',len(frames),'ms',round((time.perf_counter()-t)*1000,1),'dim',mi.feature_dim)
"
# → frames 1886 ms 6349.8 dim 71（三次复现 6.2~6.4s；cProfile 归因见 §一 D6）
```

落地后的验收标准：

| 项 | 标准 |
|----|------|
| 数值等价（N6） | 同参数产出的特征矩阵与拆前**逐值相等**（`np.array_equal`），bbox_v3 / bbox_v3_priors / bbox_v3_window_priors / bbox_v3_visual 四份各验一次。**D6 修复必须落在这条下面**——它是性能改动，不许改数值 |
| D6 修复效果 | 同一条 step（1886 帧）`build_base_features` 从 6.3 s 降到**百毫秒以内**，峰值内存不再随检测框数线性增长 |
| 三个 Segmenter | 同 ckpt 同输入，产出的 `SegmentFact` 列表与拆前完全一致 |
| 数据壳清点 | 离线侧 `@dataclass` 由 3 个（`VisualFrames`/`ModelInput`/`ExportQuality`）降到 1 个（`FeatureBlock`）；`FetchStats` 与 `ExportSpec`/`ExportResult`/`OfflineRunSpec`/`OfflineRunResult` 这些参数壳不动 |
| `pytest tests/` | 全量 passed |
| 离线 CLI 回环 | `export → infer` 手动跑通一条真实 step，`.cache` 命中与过期清理各验一次 |
