# 离线逐帧类别概率旁路：`offline_debug.json` → `label_probs.npz`

> **变更状态**：生效中（2026-09-25）
> **知识库**：待沉淀

## 概述

离线分割模型的逐帧 softmax 以 `LabelProbs`（`app/domain/temporal.py`）落 `{step}/inference/label_probs.npz`，
取代无形状的 `offline_debug.json`。它是可视化旁路，不进分割契约：模型契约仍是输出 `TemporalSegment`，
`label_probs()` 为可选钩子。影响 `app/domain/temporal.py`、`app/storage/inference/`、`offline/` 三处。

## 变更背景

- **现状 / 痛点**：`offline_debug.json` 只存每帧 top1 的 label 与 conf，且无固定形状（`Mapping`）、无读函数。
  admin 页离线推理 tab 的「全量模式」要画各 label 的逐帧概率曲线，这份数据既不完整也读不了。
- **触发来源**：离线推理结果可视化设计（全量 / 简洁两种显示模式；简洁模式读 `temporal.jsonl`，全量模式读本产物）。
- **承接**：建立在 [20260925_TEMPORAL_TYPE_RENAME](20260925_TEMPORAL_TYPE_RENAME.md)（时序层产出统一到
  `domain/temporal.py` ↔ `storage/inference/_temporal.py`）与 [20260925_OFFLINE_STAGE_CONFIG](20260925_OFFLINE_STAGE_CONFIG.md) 之上。

## 方案详情

### 全景

```text
segmenter.segment(model_input) → List[TemporalSegment]         契约：idle 不产出，后处理归模型类
segmenter.label_probs()        → LabelProbs | None              旁路：上一次 segment() 的逐帧 softmax
        │
        ▼ OfflineRunner.run
  ① 校验分段（整批非法即抛，不写）
  ② 旁路：label_probs 非 None → 形状检查 → write_label_probs     形状不一致 / 写失败只告警，不影响 ③
  ③ 事实：read_temporal → 丢全部旧分段、留 TemporalEvent → write_temporal
```

**② 必须先于 ③**：事实是结果的真源，它落盘即表示本次运行完成。旁路在前，页面读到新事实时对应的概率必然已是
同一次运行的；反序会有一个窗口让新分段配上旧概率。

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| `LabelProbs` 契约 | `app/domain/temporal.py` | §1 |
| npz 编解码与落位 | `app/storage/inference/_temporal.py`、`_layout.py`、facade | §2 |
| 钩子与产出 | `offline/segmenter.py`、`offline/impl/clean.py` | §3 |
| 落盘编排 | `offline/runner.py` | §4 |

### 1. `LabelProbs`

```python
@dataclass(frozen=True, eq=False)
class LabelProbs:
    ts: np.ndarray            # [T] float64，与 detections.jsonl 位级相等
    probs: np.ndarray         # [T, C]
    labels: Tuple[str, ...]   # C 个类名，含背景类
```

与 `TemporalEvent` / `TemporalSegment` 同住 `temporal.py`（按产出层归档）。代价：该模块引入 numpy，不再是纯 stdlib；
`app.domain` 本来就经 `detection.py` 依赖 numpy，import 开销不变。`eq=False` 因 ndarray 不支持逐值 `==`。
形状一致性不自检，由产出侧（Runner）检查。

### 2. 存储

| 旧 | 新 |
|----|----|
| `DEBUG_NAME = "offline_debug.json"` | `LABEL_PROBS_NAME = "label_probs.npz"` |
| `write_debug_result(task, step, payload: Mapping)` | `write_label_probs(task, step, LabelProbs)` |
| 无读函数 | `read_label_probs(task, step) -> LabelProbs \| None` |

- 三个键：`ts` float64（无损）、`probs` **float16**（有损，可视化够用；读回转 float32）、`labels` unicode。
  10 分钟 step（9000 帧 × 6 类）约 110 KB。
- 路线 C：同目录 tmp → `os.replace`；失败删 tmp、旧文件保留。`np.savez` 收文件对象而非路径——传路径时它会给
  不以 `.npz` 结尾的 tmp 名自动补后缀。
- 读写 `allow_pickle=False`。文件不存在返回 `None`；损坏直接抛（npz 是整体，不做逐行容错）。
- `delete(task, step)` 本就删整域，无需改。

### 3. 钩子与产出

- `OfflineSegmenter.debug_result() -> dict | None` → `label_probs() -> LabelProbs | None`，默认 None。
  `BrushRulesSegmenter` 不实现。
- `_CleanTorchSegmenter._predict_with_model` 由返回 `(labels, confs)` 改为返回完整 softmax `[T, 6]`；
  argmax / max 挪到 `segment()`。分段结果逐值不变。`_last_result` 调试 dict 与 `asdict` import 删除。

### 4. Runner

`_maybe_write_debug` → `_maybe_write_label_probs`，顺序移到写事实之前（理由见全景）。新增模块级
`_label_probs_problem` 检查 `ts` 一维、`probs` 二维、行数相等、`labels` 数等于列数；不通过记 warning 不落盘。

### 5. 保留项

- 分割契约：`segment()` 输出 `List[TemporalSegment]`，不变。
- `temporal.jsonl` 格式与替换规则不变。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 逐帧产物 | `offline_debug.json`：每帧 top1 label + conf，无形状、无读口 | `label_probs.npz`：每帧全部类别概率，`LabelProbs` 往返 |
| 旁路与事实的落盘顺序 | 事实先、调试件后 | 旁路先、事实后 |
| 旁路失败 | 写失败告警 | 形状不一致或写失败均告警，事实照写 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_storage_inference.py` | `TestDebugResult` 3 条 → `TestLabelProbs` 7 条：ts 位级往返、float16 精度、仅落域目录、覆盖、缺失返回 None、空序列、禁 pickle 可读、换名失败保留旧文件且无 tmp 残留 |
| `tests/test_offline_pipeline.py` | 钩子改名；CLEAN 前向打桩返回 softmax，验证 `label_probs` 内容与 Runner 落盘；新增「旁路形状不一致 → 不落 npz、事实照写」 |
| `tests/test_import_hygiene.py` | 通过；`_temporal` 条目注释改为带 numpy，预算值不变 |
| 全量 `pytest tests/` | **868 passed, 8 skipped**（基线 863；跳过项均需 cv2 与项目自带 ffmpeg） |
| 残留 | `grep -rnE 'offline_debug\|debug_result\|write_debug\|DEBUG_NAME' app tests .claude README.md config` 零命中 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 盘上旧 `offline_debug.json` 无人再读写 | 残留文件，无害 | 随 TTL 或下次同 step 首写自清消失 |
| float16 有损 | 概率显示精度约 3 位有效数字 | 可视化足够；若有分析用途再改 float32（体积翻倍） |
| `label_probs.npz` 的消费方尚未存在 | 本批只落盘 | admin 离线推理 tab（`GET /admin-f3m8/offline/label-probs`）另起一批 |
