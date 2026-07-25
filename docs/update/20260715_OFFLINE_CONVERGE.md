# 离线推理链路收敛：删平行 worker 路径，统一到 Runner + 复用现有数据契约

> **变更状态**：待提交 PR（2026-07-15）
> **知识库**：已沉淀 → [kb/ARCHITECTURE_DATA_FLOW.md](../kb/ARCHITECTURE_DATA_FLOW.md)(2026-07-21)
>
> 承接并**取代** [20260714_OFFLINE_INFER_BASELINE.md](20260714_OFFLINE_INFER_BASELINE.md)：该记录描述的 `worker.py` / `interfaces.py` / 单数 `segmenter/` 包 / 短名注册表已删；离线 baseline 收敛为本记录所述的单一 Runner 路径。[20260707_OFFLINE_SEGMENTER_ENTRYPOINT.md](20260707_OFFLINE_SEGMENTER_ENTRYPOINT.md) 的框架骨架保留，仅基类模块更名与数据契约收敛。

## 概述

- **改了什么**：离线链路两次提交叠出两套平行实现（框架 Runner 路径 + worker baseline 路径），本次删掉更弱的 worker 平行管线，把有价值的特征工程 + 规则分类器统一收进走 Runner 的自包含策略；并复用现有 `FrameDetections`/`SegmentFact` 契约、删掉重复的 `interfaces.py` 数据壳。
- **为什么改**：worker 绕过框架自建「读文件→切段→写文件」，与 `FeatureStore.load_many`/`FactLedger.replace_segments`/`OfflineRunner` 重复且更弱（`replace_model_segments` 无锁、按 model_version 而非 producer 过滤）；`interfaces.py` 的 `OfflineFrame`/`OfflineFeatureSequence`/`TimelineSegment`/`OfflineInferenceResult` 大多是 `FrameDetections`/`SegmentFact` 换壳。
- **影响面**：仅 `offline/` 与 `store.py`/`config.py` 的离线读写；在线链路零改动。生产 stage 1/2 的 `offline` 保持 `{}`；仅 MOCK stage 启用作配置化路由样例。

## 做了哪些整合（两套 → 一套）

每一行都是一处「07-07 框架 + 07-14 baseline 各造了一份」的重复，本次合并到左边保留项、删掉右边淘汰项：

| 能力 | 两套并存（改前） | 收敛为（改后） | 处置 |
|------|------------------|----------------|------|
| 编排 Runner | `OfflineRunner`(runner.py) ＋ `OfflineInferenceWorker`(worker.py) | `OfflineRunner` | 删 worker |
| 读特征序列 | `FeatureStore.load_many` ＋ `load_sequence_from_features_jsonl`(worker) | `load_many`（并入 BOM 容忍） | 删 worker 版 |
| 幂等写事实 | `FactLedger.replace_segments`（持锁+原子+按 producer）＋ `replace_model_segments`(worker，无锁、按 model_version) | `replace_segments` | 删更弱的 worker 版 |
| 逐帧切段 | 策略 `segment()` 内归并 ＋ `predictions_to_timeline`(worker) | 策略 `segment()` | 删 worker 版 |
| 组件路由 | `stage_factory` 全限定 `class:` 路径 ＋ `SEGMENTER_REGISTRY`/`create_segmenter` 短名表 | 全限定 `class:` 路径（与在线 Detector/Operator 一致） | 删短名表 |
| 策略目录 | 单数 `segmenter/`(模型件) ＋ 复数 `segmenters/`(适配层) | 复数 `segmenters/` 一处；每策略一自包含单文件 | 删单数包、消命名碰撞 |
| brush 规则类 | `segmenter/brush_rule.py`(模型) ＋ `segmenters/brush_rule.py`(适配) | `segmenters/clean.py` 的 `CleanSegmenter`（模型+特征+适配内聚一处） | 二合一 |
| 特征转换层 | `segmenter/features.py`（看似共享的顶层模块） | 下沉进 `CleanSegmenter.preprocess`（clean 私有） | 归位 |
| CLI 入口 | `cli.py`(run) ＋ `worker.py`(run/query) | `cli.py` 的 `run` / `query` 两子命令 | 折回单一入口 |
| 数据契约 | `interfaces.py`(`OfflineFrame`/`OfflineFeatureSequence`/`TimelineSegment`/`OfflineInferenceResult`/`OfflineTemporalModel`) | 复用 `FrameDetections`(入) / `SegmentFact`(出)；只留 `ModelInput` | 删整个 interfaces |
| mock/占位策略 | `CleanActionSegmenter`(segmenters/clean_action.py) | `MockSegmenter`(segmenters/mock.py)，照 MockDetector 归位 | 更名归位 |

> 净效果：`offline/` 只剩 `segmenter.py`(基类) + `runner.py` + `cli.py` + `__init__.py` 与 `segmenters/{clean,mock}.py`；删 ~1291 行、增 ~284 行。

## 改动详情

### 1. 删平行管线与重复数据壳
- 删 `worker.py`（`OfflineInferenceWorker` 等整套）、`interfaces.py`（整个文件）、`heuristic.py`（无用别名）、单数 `segmenter/` 包（含 `create_segmenter`/`SEGMENTER_REGISTRY` 第二套短名路由）。
- **复用契约**：离线输入吃 `FrameDetections`/`Detection`（`app.domain.detection`），输出吐 `SegmentFact`（`app.services.inference.models`）。仅保留 `ModelInput`（62 维数值矩阵）一个新表示，下沉进 clean 策略私有。

### 2. 基类模块更名 + 两个自包含策略
- `offline/base.py` → `offline/segmenter.py`（类名 `OfflineSegmenter` 不变；`base` 不直观）。新增**可选** `debug_result() -> dict | None`（默认 None）。
- `segmenters/clean.py`（`CleanSegmenter`，CLEAN 离线 baseline）：单文件自包含「62 维 `FeatureVectorizer`/`ModelInput` + 规则薄分类器 + adapter」。`preprocess` 把订阅流**按 ts 跨 source 拍平成 `List[FrameDetections]`** → 62 维 `ModelInput`（不再经中间数据壳；`FrameInference` 带 cq 在线包袱故不复用）；`segment` 逐帧规则分类 → 归并 `SegmentFact`；`debug_result` 返回逐帧纯 dict。
- `segmenters/mock.py`（`MockSegmenter`，MOCK 链路 stand-in）：由 `CleanActionSegmenter` 更名归位，presence 型（照 `workflows/mock.py` MockDetector 定位），`debug_result` 恒 None。

### 3. runner 解耦「存储 step_id」与「stage 配置 key」
- `config.py` 新增纯方法 `InferenceConfig.resolve_stage(step_id)`：命中即恒等、未配回退 `"MOCK"`——与 `InferenceManager.resolve_stage` 同规则（同源同义）。
- `runner.py` `run()` 改 `stage_key = config.resolve_stage(spec.step_id)`；**存储读写仍用原数字 `spec.step_id`**。于是未配数字 step（如 `-1`）经 resolve 回退 MOCK.offline、仍读写 `{task}/-1/` 分区。另在 `replace_segments` 后按 `segmenter.debug_result()` 可选落 `offline_inference_result.json`。

### 4. 折回 worker 独有能力
- `FeatureStore.load_many` 读文件改 `utf-8-sig`，容忍 Windows 手写 features.jsonl 的 UTF-8 BOM。
- `cli.py` 从单命令改 `run` / `query` 两子命令；`query` 复用 `FactLedger.load` 打印 SegmentFact 时间线（不碰 torch/runner）。`--step-id` 保持 `type=int`（数字存储键）。

### 5. 配置：仅启用 MOCK.offline
`config/inference_config.yaml` 的 MOCK stage `offline` 从 `{}` 改为启用（`class: ...segmenters.mock.MockSegmenter`, `subscribes: [mock]`），作「能端到端跑的配置化路由样例」。生产 stage 1/2 `offline` 保持 `{}` 不动（守 CLAUDE.md 硬规矩）。

## 使用

```bash
# MOCK.offline 配置化路由：用未配数字 step（-1）→ resolve_stage 回退 MOCK
python -m app.services.inference.offline.cli run   --task-id <t> --step-id -1
python -m app.services.inference.offline.cli query --task-id <t> --step-id -1
```

> **CleanSegmenter（CLEAN baseline）的运行门槛**：`--strategy` 只覆盖 `offline.class`，不越过工厂的
> `enabled` 门；生产 `CLEAN(step2).offline` 保持 `{}`，故 CLI 不能直接对 step 2 跑 CleanSegmenter
> （skip「offline 未启用」——正是硬规矩的守卫）。CleanSegmenter 由单测覆盖；开发期手动跑需在 dev
> 配置里临时把 `CLEAN.offline` 配上 `enabled/name/subscribes`。

## 数据通道

| 通道 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| `{base}/{task}/{step}/features.jsonl` | 在线 `FeatureStore.append`（常开） | 离线 `load_many`（`FrameDetections`） | 只加 BOM 容忍，不改写口径 |
| `{base}/{task}/{step}/facts.jsonl` | 离线 `FactLedger.replace_segments` | `FactLedger.load` / cli query | 无 worker 平行写方 |
| `{base}/{task}/{step}/offline_inference_result.json` | Runner（策略 `debug_result` 非 None 时） | 人工调试/对比 | 逐帧调试可选产物 |

## 验证

| 项 | 结果 |
|----|------|
| `tests/test_offline_pipeline.py`（存储/工厂/Runner/mock+clean/resolve/BOM/cli/query） | 37 passed |
| 全量 `pytest tests/`（CLEANSIGHT_ENV=test + dummy DB/API env） | 330 passed |
| 全仓无 `offline.base`/`interfaces`/`worker`/`clean_action`/`create_segmenter` 等残存引用 | grep 0 命中 |
| 手动烟测 A：`cli run --step-id -1` | `completed`、resolve 回退 MOCK、facts 落盘、无 debug JSON |
| 手动烟测 B：CleanSegmenter | 4 帧 → 62 维 → `short_brush_cleaning`（0.1-0.4）、debug 4 帧 |

## 后续（不变）

自动调度、离线 Judge（`SegmentFact` → 合规判断/告警）、结果入库仍未实现——本次只做链路收敛与 baseline 工程闭环，不判合规、不告警。真实时序模型（MS-TCN/ASFormer）接入 = 新增一个自包含单文件 `segmenters/<stage>.py` + YAML `offline.class` 一行，输入吃 `FrameDetections`、输出吐 `SegmentFact`，不再自定义中间数据壳。
