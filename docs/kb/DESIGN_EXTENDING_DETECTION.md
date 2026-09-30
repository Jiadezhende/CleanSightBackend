> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 新增检测任务

新增检测点 = 一个 Detector 子类 + 一个 Operator 子类 + YAML 各加一条，可选再加一个离线 Segmenter。代码骨架、字段速查和逐项检查清单在 `/infer-workflow` skill（`.claude/skills/infer-workflow/`），接口签名见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)「Detector / Operator 框架接口」，分层判据见 [DESIGN_DETECTION_WORKFLOW.md](DESIGN_DETECTION_WORKFLOW.md)。本文只列步骤与容易静默出错的约束。

## 落点：三段同名文件，一文件一基类

均在 `app/services/inference/` 下：Detector 写 `online/detection/impl/<业务>.py`，Operator 写 `online/temporal/impl/<业务>.py`，离线 Segmenter 写 `offline/impl/<业务>.py`。业务聚合由 YAML 的 stage 绑定表达；`StageFactory` 按 `class` 全路径 importlib 实例化，不用改任何 `impl/__init__.py`。online 与 offline 互不 import，两边都要用的纯函数放推理包顶层（如 `resample.py`）。

## 步骤

1. **Detector**：YOLO 模型继承 `YOLODetector`（只需实现 `prepare_visualization_data`），否则继承 `Detector` 并实现 `infer_batch`。`name` 写死为产出流名。
2. **Operator**：继承 `Operator`，`__init__` 完整初始化 `self._sm`（含游标 `last_ts`），实现 `analyze` / `judge`，结算逻辑 override `finalize`。内嵌时序模型时继承 `TemporalOperator`，接入走 `/temporal-review` 审查清单。
3. **YAML**：`config/inference_config.yaml` 对应 stage 的 `detectors[]` 加流源、`rules[]` 加算子（`name` / `subscribes` / `realtime` / `class` / `params`）。`name` 与 `subscribes` 由工厂注入，不写进 `params`。
4. **新 stage**（新洗消步骤）：加一个 step_id 键并至少配一个 detector 才生效；`rules: []` 的 stage 只画检测框、不建 Actor。
5. **告警指标**：新检测点需要新指标时，先在 `app/types/alarm.py::AlarmMetric` 补枚举，算子产 `Alarm` 时显式填 `metric`。
6. **离线 Segmenter**（可选）：见下节。
7. **测试**：见文末。

## 容易静默出错的约束

- **时间戳原样回写**：`infer_batch` 必须把 `timestamps[i]` 写进对应 `DetectorOutput.timestamp`。自造时间戳不报错，但多流对齐和算子游标会错位。
- **类别名严格一致**：`class_name` 直接取模型 `result.names`、不归一化；算子里匹配的字符串（如 `"bent"`）和 `TemporalOperator` 的 `objects` 词表都必须与训练类别名逐字相同。
- **`DetectorOutput` 不加领域字段**：单框派生量放 `DetBox.extra`（不落盘），时序统计放 Operator。
- **跨帧累加必须用游标**：帧窗每 tick 重叠，按 `last_ts` 只处理新帧；自己派生的历史要按 `window_seconds` 裁剪。
- **`signals_10s` 要求流名能映射到指标**：只有 `realtime: true` 规则订阅的流、且 `AlarmMetric(流名.upper())` 存在时才进映射，否则只打 warning 跳过（CLEAN 的两条流即如此）。
- **`TemporalOperator` 的 `model_input_fps`**：须等于训练帧率，且 ≤ `settings.inference_fps`。配错帧率不报错、静默误分类；超上界则在 `start_workflow` 构造时抛错，表现为每次 `/api/start` 失败，后端本身照常启动。
- **YAML 结构错误让后端起不来**：detector 导入 / 构造失败、rule 缺 `class` 或 `subscribes`、operator 类导入失败，都会在启动期抛出，不会静默少一个组件。

## 新增离线 Segmenter

离线段独立于在线链路（CLI 子进程跑，不接 CQ / 告警），对整段检测序列做全序列分割。

- 往 `offline/impl/` 加一个自包含单文件的 `OfflineSegmenter` 子类，目标 stage 的 `offline` 块填 `class` + `params`（只有这两个键，`params` 原样作构造参数）。一个 stage 至多一个离线模型。
- `name` = 类名，自动成为 `TemporalSegment.producer`，子类不要覆盖。`segment` 产出的时间是帧捕获墙钟 ts；Runner 统一校验（producer、有限数、start ≤ end、conf 值域，任一非法整批失败）并替换该 run 的全部分段。
- 策略是纯算法：不碰 `app.storage` / CQ / DB，`frames` 只读。
- 按训练帧率入模的模型用 `resample_by_ts(frames, fps, strict=True)`，检测帧率不够即报错。
- 权重类模型只读单个 `.pt`，网络结构与 window 写死在类里，`strict=True` 加载；无 `model_path` 硬失败（`ValueError`），不做规则降级。权重与特征 recipe 一一对应，换策略时 `class` 与 `params` 一起换。
- 本地回环用 `tests/doubles.py::BrushRulesSegmenter`（纯规则、无权重）写进注入的 config；验证入口是 CLI `run` 或 admin「运行离线推理」。

## 测试清单

- Detector：输出 `DetectorOutput` 格式正确，`timestamp` 原样回写。
- Operator：实时告警的上升沿触发与复位、结算逻辑、游标不重复计数。
- YAML 能被 `StageFactory` 加载；目标 step 的 `resolve_stage` 恒等返回，未配置 step 抛 `ValidationError`（`tests/test_inference_stage_routing.py`）。
- 新指标：`/task/message/{task_id}` 的 `signals_10s` 含该 metric。
- 离线策略：`require_offline` 通过，Runner 产出 `producer` = 类名（`tests/test_offline_pipeline.py`）。

## 代码来源

- `app/services/inference/online/detection/detector.py`、`online/temporal/operator.py`
- `app/services/inference/online/{detection,temporal}/impl/{bubble,bending,clean}.py`
- `app/services/inference/offline/{segmenter,runner}.py`、`offline/impl/clean.py`
- `app/services/inference/{stage_factory,config,resample}.py`、`online/service.py`
- `app/types/{detection,temporal,alarm}.py`
- `config/inference_config.yaml`
- `tests/test_inference_stage_routing.py`、`tests/test_offline_pipeline.py`、`tests/doubles.py`
