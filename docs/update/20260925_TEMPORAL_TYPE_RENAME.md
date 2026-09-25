# 时序层产出改名：`fact` → `temporal`，与检测层对称

> **变更状态**：生效中（2026-09-25）　<!-- 纯改名，零行为变更 -->
> **知识库**：待沉淀

## 概述

`app/domain/fact.py` 改名 `app/domain/temporal.py`，`EventFact` → `TemporalEvent`、`SegmentFact` → `TemporalSegment`，
删去 `Fact` 并集别名；推理域的 `facts.jsonl` → `temporal.jsonl`、`read_facts` / `write_facts` → `read_temporal` /
`write_temporal`。字段、行语义、写路线不变；盘上旧 `facts.jsonl` 改名后不可见。

## 变更背景

- **现状 / 痛点**：
  - 检测层（L1）的契约在 `domain/detection.py`、落盘编解码在 `storage/inference/_detection.py`；时序层（L3）的契约却叫
    `domain/fact.py`，编解码叫 `_temporal.py`。同一层两个名字，找东西要记两套规则。
  - `Fact` 看不出出自哪一层；`DetBox` / `DetectorOutput` / `FrameDetection` 以 `Det` 标明层级，时序侧没有对应前缀。
  - 本仓「segment」大量指 HLS 视频段（`SegmentRef`、`list_segments`、`segment_path`）。`SegmentFact` 靠后缀区分，
    `TemporalSegment` 靠前缀区分，扫读时更早分辨出来。
- **触发来源**：离线推理结果可视化设计中，要给时序层新增逐帧类别分布（`LabelProbs`），需要先定它和既有两型的归属文件。
- **承接**：建立在 [20260924_DETECTION_TYPE_RENAME](20260924_DETECTION_TYPE_RENAME.md) 之上（检测侧三型已改名）及其后续
  [20260925_FRAME_DETECTION_MERGE](20260925_FRAME_DETECTION_MERGE.md)、[20260925_DETECTION_WRITE_PATH_RENAME](20260925_DETECTION_WRITE_PATH_RENAME.md)。

## 方案详情

### 全景：两层契约与两个编解码模块一一对应

```text
层   domain 契约                                    storage 编解码                     落盘
L1   domain/detection.py  DetBox / DetectorOutput /   storage/inference/_detection.py   detections.jsonl
                          FrameDetection
L3   domain/temporal.py   TemporalEvent /             storage/inference/_temporal.py    temporal.jsonl
                          TemporalSegment
```

规则只剩一条：**按产出层命名，domain 与 storage 同名配对。**

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 类型与模块改名 | `app/domain/temporal.py` | §1 |
| 存储成员与落盘文件改名 | `app/storage/inference/` | §2 |
| 调用点与注释 | `app/services/inference/`、`app/services/recording/`、`app/storage/` | §3 |
| 测试与门禁 | `tests/` | §4 |
| 现行文档与 skill | `docs/`、`.claude/skills/` | §5 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A（采用）类名、模块名、存储成员、落盘文件名一起改 | 盘上旧 `facts.jsonl` 不可见 | 不留「fact 就是 temporal」这层映射给读者记 |
| B 只改类名与模块名，保留 `facts.jsonl` / `read_facts` | 零数据代价 | 否：`fact` 一词残留在存储层，改名只做了一半 |
| C 缩写 `TemporalSeg` | 更短 | 否：与 `TemporalEvent` 全称不对称；`seg` 在 HLS 代码里已作变量名 |

### 1. `app/domain/fact.py` → `app/domain/temporal.py`

| 旧 | 新 |
|----|----|
| `app/domain/fact.py` | `app/domain/temporal.py`（`git mv`，保留历史） |
| `EventFact` | `TemporalEvent` |
| `SegmentFact` | `TemporalSegment` |
| `Fact = Union[EventFact, SegmentFact]` | 删除。仅存储层两三处签名使用，直接写 `TemporalEvent \| TemporalSegment` |

字段一个不动（`producer` / `signal` / `value` / `ts` / `conf` / `meta`；`producer` / `label` / `start` / `end` / `conf` / `meta`），
三条硬约束（帧捕获墙钟时间轴、`producer` 唯一真源、`meta` 不放判断用键）原样保留在模块 docstring。
本批 `temporal.py` 仍是纯 stdlib。

### 2. 存储域 `app/storage/inference/`

| 位置 | 旧 | 新 |
|------|----|----|
| `_layout.py` | `FACTS_NAME = "facts.jsonl"` | `TEMPORAL_NAME = "temporal.jsonl"` |
| `_temporal.py` | `read_facts` / `write_facts` | `read_temporal` / `write_temporal` |
| `_temporal.py` | `_fact_to_record` / `_record_to_fact` | `_temporal_to_record` / `_record_to_temporal` |
| `__init__.py` | re-export 与 docstring | 同步 |

- 行内 `type` 判别字段的取值 `"event"` / `"segment"` **不改**：它描述的是行的形状，不带层级前缀也不歧义。
- `write_debug_result` / `DEBUG_NAME` / `offline_debug.json` **本批不动**，随后续 `LabelProbs` 那一批一起换掉（那一批改的是格式，不是纯改名）。

### 3. 调用点与注释

| 文件 | 改动 |
|------|------|
| `app/services/inference/offline/segmenter.py` | 基类返回类型、docstring |
| `app/services/inference/offline/runner.py` | 构造、`isinstance`、`_validate`、`_replace_own_segments` 内的读写调用 |
| `app/services/inference/offline/cli.py` | `query` 子命令的读取与过滤 |
| `app/services/inference/offline/impl/clean.py`、`impl/mock.py` | 构造 `TemporalSegment` |
| `app/services/inference/offline/__init__.py`、`frame_tracker.py` | docstring 中的模块引用 |
| `app/services/inference/stage_factory.py` | docstring（`= SegmentFact.producer`） |
| `app/services/inference/types.py` | docstring 中的契约清单 |
| `app/services/inference/temporal/operator.py` | docstring（「不再有 EventFact 作为对象间传输」） |
| `app/domain/detection.py` | docstring（「与时序层的 `Fact` 同级」） |
| `app/services/recording/service.py` | 首写自清注释中的 `facts.jsonl` |
| `app/storage/__init__.py`、`app/storage/tasks.py`、`app/storage/inference/_jsonl.py` | 落盘结构注释 |
| `scripts/migrate_hls_layout.py` | 注释中的 `facts.jsonl`（该脚本尚未入库，本批未改；入库时按新名写） |

`EventFact` 今天零生产者，`TemporalEvent` 同样零生产者；本批不增删类型。

### 4. 测试与门禁

- `tests/test_storage_inference.py`、`tests/test_offline_pipeline.py`：符号与文件名替换，**断言值不改**——断言要改说明混进了行为变更。
- `tests/test_storage_tasks.py`：种子文件名 `facts.jsonl` → `temporal.jsonl`。
- `tests/test_import_hygiene.py`：`BUDGET` 两处注释（「`_temporal` 的货币 Fact 是纯 stdlib」）改为新名，预算值不变。

### 5. 现行文档与 skill

- `.claude/skills/infer-workflow/SKILL.md` 与 `evals/evals.json`：`EventFact` 提法。
- `docs/STORAGE_REFACTOR_MAP.md`：落盘结构与成员表（该文件尚未入库，本批未改；入库时按新名写）。
- `docs/update/` 历史记录不回溯改写；`docs/kb/`（6 篇命中）不在本批动，留给 KB 融合流程。

### 6. 保留项（不改动）

- `producer` 字段名、按 producer 幂等替换的语义、`write_temporal` 整体替换的写路线。
- CLI 子命令名 `query` 与 `--producer` 参数；输出 JSON 的 `timeline` 键。
- `offline_debug.json` 及其读写成员（见 §2）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 时序层契约模块 | `app/domain/fact.py` | `app/domain/temporal.py` |
| 类名 | `EventFact` / `SegmentFact` / `Fact` | `TemporalEvent` / `TemporalSegment` / 无别名 |
| 落盘文件 | `{step}/inference/facts.jsonl` | `{step}/inference/temporal.jsonl` |
| 存储成员 | `read_facts` / `write_facts` | `read_temporal` / `write_temporal` |
| 运行时行为 | — | 无变化 |

**自测结果**

| 项 | 方法 | 结果 |
|----|------|------|
| 零行为变更 | `.venv` 下全量 `pytest tests/` | 858 passed / 8 skipped（skip 均为「需要 cv2 与项目自带 ffmpeg」，与本批无关）；断言只换了符号与文件名字符串，期望值未动 |
| 无残留 | `grep -rnE 'EventFact\|SegmentFact\|\bFact\b\|domain\.fact\|read_facts\|write_facts\|facts\.jsonl\|FACTS_NAME'` 于 `app/ tests/ scripts/ config/ .claude/` | 零命中 |
| 导入门禁 | `tests/test_import_hygiene.py` | 通过（`temporal.py` 仍纯 stdlib，预算值未改） |
| 手工回环 | `offline.cli run` → `query` | 未跑；`tests/test_offline_pipeline.py` 已覆盖 runner 写 `temporal.jsonl` 与回读 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 盘上旧 `facts.jsonl` 改名后读侧不可见 | dev 机器上已有的离线分段结果读不到 | 按既定政策不做兼容，随 TTL 消失；需要时重跑离线 |
| KB 6 篇仍用旧名 | KB 与代码暂时不一致 | 下次 KB 融合时按本记录替换 |
| 后续：`LabelProbs` 进 `domain/temporal.py`，`offline_debug.json` → `label_probs.npz`，`debug_result()` → `label_probs()` | 届时 `temporal.py` 引入 numpy，§4 的门禁注释需再改一次 | 单独一批，属格式变更 |
