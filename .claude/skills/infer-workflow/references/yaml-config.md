# YAML 装配机制

检测任务通过 [config/inference_config.yaml](../../../../config/inference_config.yaml) 装配 —— Detector/Operator 真正生效的地方（[stage_factory.py](../../../../app/services/inference/stage_factory.py) 按 `class` 全路径 importlib 实例化）。**写了代码不在此登记 = 任务不会被加载。**

每个 stage（主键 = `step_id` 字符串，`alias` 为可读名）拆两段：`detectors[]`（流源，分组粒度）+ `rules[]`（流算子，规则粒度）：

```yaml
stages:
  "1":                                # 主键 = step_id（task.current_step）
    alias: LEAK                       # 可读名（写告警 step_name + 可视化叠字）
    detectors:
      - name: bubble                                                          # 产出流名 = slide_window key
        class: app.services.inference.detection.impl.bubble.BubbleDetector    # Detector 全路径
        params:                          # → 传给 Detector.__init__
          model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/bubble-best.pt
          conf_threshold: 0.1
          iou_threshold: 0.45
          enabled: true
    rules:
      - name: bubble_leak                                              # 算子自身/输出身份（≠ 流名）
        subscribes: [bubble]                                           # 输入流清单，显式必填（= detector.name）
        realtime: true                   # 实时信号，纳入 signals_10s
        class: app.services.inference.temporal.impl.bubble.BubbleOperator  # Operator 全路径
        params:                          # → 传给 Operator.__init__（name/subscribes 由 factory 注入，不重复写）
          window_seconds: 3.0            # 感受野（秒）
          birth_rate_threshold: 0.5      # 阈值等规则参数
    offline: {}                          # 离线段占位，本次未实现
```

| 字段 | 作用 |
|------|------|
| stage 主键 | = `step_id`（`task.current_step`）；`MOCK` 为未知 step 的 fallback |
| `alias` | 可读名，仅出口用（告警 step_name + 可视化叠字），功能性标识一律用主键 |
| `detectors[].name` | 产出流名 = `slide_window` key；被 `rules[].subscribes` 引用 |
| `detectors[].class` | Detector 全路径，**多 Client 共享一个实例** |
| `detectors[].params` | Detector 构造参数（`**kwargs`） |
| `rules[].name` | 算子自身/输出身份（日志/告警归属），**≠ 流名** |
| `rules[].subscribes` | 输入流清单，**显式必填**；元素 = 上游 `detector.name`（缺失则该规则 fail-fast 跳过） |
| `rules[].realtime` | `true` = 纳入 signals_10s；纯结算/纯 overlay 设 `false` |
| `rules[].class` | Operator 全路径，**每 Client 实例化一个** |
| `rules[].params` | Operator 构造参数；阈值/required/window_seconds 放这里（`name`/`subscribes` 由 factory 注入） |

> 环境变量用 `${VAR:default}` 语法展开（如 `model_path`）。
> `rules` 留空（如 CLEAN 仅画框）= 不建 Operator，只由 detector 提供检测框可视化。

## 多流订阅

一个算子可订阅多条流（`subscribes: [clean_large, clean_small]`），基类 `_zip_by_ts` 按 ts 对齐。要求各流对应的 detector 都在同 stage 的 `detectors[]` 里、名字与 subscribes 严格一致。参考 CLEAN stage 的 `clean_monitor` 规则。

## 内嵌序列模型算子的 params

`GRUOperator` 子类（见 templates.md 模板 D / [temporal/impl/clean.py](../../../../app/services/inference/temporal/impl/clean.py)）当前把 `model_path` / `objects` / `actions` / `hidden` / `num_layers` / `window_seconds` / `min_frames` 走 `rules[].params`：

```yaml
      - name: clean_monitor
        subscribes: [clean_large, clean_small]
        realtime: true
        class: app.services.inference.temporal.impl.clean.CleanOperator
        params:
          window_seconds: 10.0
          model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/gru-final.pt
          objects: {0: hand, 1: scope_control_body, ...}   # 检测目标词表（定 input_dim=count*4）
          actions: {0: idle, 1: air_injection, ...}        # 动作类别词表（定 num_classes）
          hidden: 256
          num_layers: 4
```

> ⚠️ `hidden`/`num_layers` 及 `objects`/`actions` 的**数量**必须逐位等于 .pt 训练配置，否则 `load_state_dict` 直接抛 shape/key mismatch。理想做法是让这些随 checkpoint 走（单一真源），审查取舍见 `/temporal-review` 的「YAML 装配」节。

## 无需改 `__init__.py`

各 `impl/__init__.py`（`detection/impl` / `temporal/impl` / `offline/impl` 各一个）是**纯包标记**，不 re-export。StageFactory 用 `class` 全路径 importlib 实例化，消费方走单文件深路径导入（`from app.services.inference.detection.impl.bubble import BubbleDetector`）。**别**往 `__init__.py` 加 import / `__all__`——那会让 import 本包即 eager 拉起全部任务模块。
