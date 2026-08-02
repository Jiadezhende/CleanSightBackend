# 20260802 推理 impl 按契约包归位（解散 workflows/，每包内建 impl/）

> 增量记录，供后续融合进 KB。校正 KB 里所有 `workflows/` 与 `offline/segmenters/` 落点描述。

## 动机

三个同范式策略基类 `Detector` / `Operator` / `OfflineSegmenter` 的实现落点此前混了两根轴：
`detection/`、`temporal/` 只留基类+框架、业务 impl 抽去独立顶层 `workflows/`；唯独 `offline/`
把业务 impl 就地放 `offline/segmenters/`。同时 `workflows/*.py` 每文件把 Detector 与
Operator 两个不同基类混在一起。config 侧（`inference_config.yaml`）本就按 stage 把
detectors+rules+offline 绑成一个业务单元——业务聚合由 config 表达，代码目录应按契约组织。

## 变更：三契约包对称

每个契约包 = 顶层「基类 + 框架管件」 + `impl/` 子层放业务实现；一个 impl 文件只实现一个基类的子类。

- `detection/`：`detector.py`(基类) + dispatcher/service/stage_worker/infer_proxy(框架) + **`impl/`**（Detector 子类）
- `temporal/`：`operator.py`(基类) + actor/alarm_sink(框架) + **`impl/`**（Operator 子类）
- `offline/`：`segmenter.py`(基类) + runner/cli(框架) + **`impl/`**（原 `segmenters/` 改名，Segmenter 子类）
- `workflows/` **删除**；其架构图文档 `workflows/CLAUDE.md` 迁为 `docs/kb/DESIGN_DETECTION_WORKFLOW.md`。

一个检测点（业务）的三段实现放各包 `impl/` 下的**同名文件**：
`detection/impl/<x>.py` + `temporal/impl/<x>.py` + 可选 `offline/impl/<x>.py`。

## 路径映射（旧 → 新）

| 类 | 旧 | 新 |
|----|----|----|
| BubbleDetector / BendingDetector / CleanLargeDetector / CleanSmallDetector / MockDetector | `workflows.<x>.<X>Detector` | `detection.impl.<x>.<X>Detector` |
| BubbleOperator / BendingOperator / CleanOperator / MockOperator | `workflows.<x>.<X>Operator` | `temporal.impl.<x>.<X>Operator` |
| Clean*Segmenter / BrushRulesSegmenter | `offline.segmenters.<x>.*` | `offline.impl.<x>.*` |

- clean 的 `_PALETTE`/`_bbox_items` 随两个 Detector 进 `detection/impl/clean.py`；
  bubble 的 `_BYTETRACK_ARGS`/`_BBoxAdapter` 随 Operator 进 `temporal/impl/bubble.py`。
  无 helper 跨 Detector/Operator 边界共享，拆分未引入 shared util。
- `offline/impl/clean.py` 内容整体平移（含 `CleanSegmenter` 别名、3 个模型变体、feature 函数）。

## 同步改动

- `config/inference_config.yaml`：9 处生效 + 注释 class_path 全部改到新落点；开发者注记同步。
- 测试：`test_temporal_debounce.py`、`test_pool_ts_anchor.py`、`test_offline_pipeline.py`（含 `_MOCK_CLASS`/`_CLEAN_CLASS` 字符串常量）。
- `inference/__init__.py` 顶注；`infer-workflow` skill（SKILL.md / templates.md / yaml-config.md）；
  `temporal-review` skill；README；KB（SERVICE_INFERENCE / DESIGN_EXTENDING_DETECTION /
  BUSINESS_DETECTION_STANDARDS / ARCHITECTURE_STORAGE_AND_SCHEMA）。

## 验证

纯落点迁移无逻辑改动。导入冒烟（10 模块）、config 装配冒烟（3 stage detectors/operators + 5 offline 类解析）、`pytest tests/` 全绿（338 passed）。
