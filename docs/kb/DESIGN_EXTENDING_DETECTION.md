> 更新时间：2026-10-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 新增检测任务

新增检测点 = 一个 Detector 子类 + 一个 Operator 子类 + YAML 各加一条，可选再加一个离线 Segmenter。代码骨架照现有实现写（`online/detection/impl/`、`online/temporal/impl/` 下的 `bubble.py` / `bending.py` / `clean.py`），接口签名见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)「Detector / Operator 框架接口」，分层判据见 [DESIGN_DETECTION_WORKFLOW.md](DESIGN_DETECTION_WORKFLOW.md)。本文只列步骤与容易静默出错的约束。

## 落点：三段同名文件，一文件一基类

均在 `app/services/inference/` 下：Detector 写 `online/detection/impl/<业务>.py`，Operator 写 `online/temporal/impl/<业务>.py`，离线 Segmenter 是一个策略一个自包含包 `offline/impl/<业务>_<模型>/`（见下「新增离线 Segmenter」）。业务聚合由 YAML 的 stage 绑定表达；`StageFactory` 按 `class` 全路径 importlib 实例化，不用改任何 `impl/__init__.py`。online 与 offline 互不 import，两边都要用的纯函数放推理包顶层（如 `resample.py`）。

## 步骤

1. **Detector**：YOLO 模型继承 `YOLODetector`（只需实现 `prepare_visualization_data`），否则继承 `Detector` 并实现 `infer_batch`。`name` 写死为产出流名。
2. **Operator**：继承 `Operator`，`__init__` 完整初始化 `self._sm`（含游标 `last_ts`），实现 `analyze` / `judge`，结算逻辑 override `finalize`。内嵌时序模型时继承 `TemporalOperator`，帧率与类别词表约束见下「容易静默出错的约束」。
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

- **一个策略一个自包含包** `offline/impl/<业务>_<模型>/`（`contract.py` / `features.py` / `model.py` / `segmenter.py`），只许 import 自身 + `offline.segmenter` / `inference.resample` / `app.types`（门禁 `test_offline_impl_imports_only_framework_contract`）。要用兄弟策略的代码就复制一份进本包：历史策略要长期保留做对照，共用基类或特征函数会改一处影响全部。
- 目标 stage 的 `offline` 块填 `class` + `params`（只有这两个键，`params` 原样作构造参数）。一个 stage 至多一个离线模型。`segmenter.py` 的 docstring 给一段可直接粘贴的**块格式** YAML（`params: {model_dir: ${…}}` 流式写法里的花括号会让 YAML 解析失败），`tests/test_offline_impl_catalog.py` 会校验它能装配。
- `name` = 类名，自动成为 `TemporalSegment.producer`，子类不要覆盖。`segment` 产出的时间是帧捕获墙钟 ts；Runner 统一校验（producer、有限数、start ≤ end、conf 值域，任一非法整批失败）并替换该 run 的全部分段。
- 策略是纯算法：不碰 `app.storage` / CQ / DB，`frames` 只读。要原始画面或跨运行缓存时声明 `uses_context = True`，经注入的 `OfflineContext`（`video` 只给 raw 轨、`load_cache` / `save_cache`）取；缓存有效性按内容逐行核对，不按时间。
- **随权重固定的量全进 `contract.py`**：类序、特征列序与维度、网络结构、输入分辨率、物料文件名、checkpoint 格式。改任何一项都必须换权重；特征数值有任何变化都要换特征版本并重训，所以移植训练侧参考实现时原样复制、只改输入边界，并与训练侧特征逐位核对。
- **物料一个模型一个目录**：构造参数收 `model_dir`（`${CLEANSIGHT_MODEL_PATH:./app/data}/<业务>-offline-<模型>`），目录内文件名在 `contract.py` 写死；实现 `materials()` 返回必需文件路径（只拼路径、不读盘、不 import torch），供 `cli materials` 与作业服务启动预检。权重缺失须在 import torch 之前抛 `FileNotFoundError`。
- 加载：网络结构写死、`strict=True`；新物料必须 `torch.load(weights_only=True)` / `np.load(allow_pickle=False)` 可读，不读旁挂 `.json`、不做格式转换。不做规则降级。
- **入模帧率是模型契约**：按训练帧率 `resample_by_ts` 抽稀；帧率判据用 `resample.input_fps`（剔断流缺口的平均间隔，不用中位数）。检测帧率不够时 strict 报错还是只告警由策略按业务定；选告警就要写明按帧计的窗口会在时间上拉长。
- 本地回环用 `tests/doubles.py::BrushRulesSegmenter`（纯规则、无权重）写进注入的 config；验证入口是 CLI `run` 或 admin「运行离线推理」。

## 测试清单

- Detector：输出 `DetectorOutput` 格式正确，`timestamp` 原样回写。
- Operator：实时告警的上升沿触发与复位、结算逻辑、游标不重复计数。
- YAML 能被 `StageFactory` 加载；目标 step 的 `resolve_stage` 恒等返回，未配置 step 抛 `ValidationError`（`tests/test_inference_stage_routing.py`）。
- 新指标：`/task/message/{task_id}` 的 `signals_10s` 含该 metric。
- 离线策略：`require_offline` 通过，Runner 产出 `producer` = 类名（`tests/test_offline_pipeline.py`，框架测试只用替身，不引用真实策略）；每个策略包一个 `tests/test_offline_<包名>.py`（特征口径、解码、缺权重先于 torch 报错、物料清单），并在 `test_offline_impl_catalog.py` 登记。

## 代码来源

- `app/services/inference/online/detection/detector.py`、`online/temporal/operator.py`
- `app/services/inference/online/{detection,temporal}/impl/{bubble,bending,clean}.py`
- `app/services/inference/offline/{segmenter,runner,materials}.py`、`offline/impl/clean_*/`
- `app/services/inference/{stage_factory,config,resample}.py`、`online/service.py`
- `app/types/{detection,temporal,alarm}.py`
- `config/inference_config.yaml`
- `tests/test_inference_stage_routing.py`、`tests/test_offline_pipeline.py`、`tests/test_offline_impl_catalog.py`、`tests/test_import_hygiene.py`、`tests/doubles.py`
