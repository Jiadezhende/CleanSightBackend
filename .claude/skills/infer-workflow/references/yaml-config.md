# YAML 装配机制

检测任务通过 [config/inference_config.yaml](../../../../config/inference_config.yaml) 装配 —— 这是 Detector/Analyzer 真正生效的地方（系统按 `class` 路径动态 import 并实例化）。**写了代码不在此登记 = 任务不会被加载。** 在对应 stage 的 `models` 下加一项：

```yaml
stages:
  LEAK:
    models:
      - name: bubble                                                    # 任务名 = slide_window key，须与代码 name 一致
        class: app.services.inference.workflows.bubble.BubbleDetector   # Detector 类全路径
        analyzer_class: app.services.inference.workflows.bubble.BirthRateAnalyzer  # Analyzer 类全路径
        params:                          # → 传给 Detector.__init__
          model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/bubble-best.pt
          conf_threshold: 0.1
          iou_threshold: 0.45
          enabled: true
        analyzer_params:                 # → 传给 Analyzer.__init__
          birth_rate_threshold: 0.5
          window_seconds: 3.0

      - name: bending
        realtime: false                  # 纯结算告警：不纳入 signals_10s 滑动窗口
        class: app.services.inference.workflows.bending.BendingDetector
        analyzer_class: app.services.inference.workflows.bending.DebounceAnalyzer
        params:
          model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/bend-best.pt
          conf_threshold: 0.1
          iou_threshold: 0.45
          enabled: true
        analyzer_params:
          debounce_frames: 5
          required_bend_actions: 4
```

| 字段 | 作用 |
|------|------|
| `name` | 任务标识；= `slide_window` key；**必须等于** Detector / Analyzer 的 `name` |
| `class` | Detector 类全路径，**多 Client 共享一个实例** |
| `analyzer_class` | Analyzer 类全路径，**每 Client 实例化一个** |
| `params` | Detector 构造参数（`**kwargs`） |
| `analyzer_params` | Analyzer 构造参数（`**kwargs`） |
| `realtime` | 默认 `true`；纯结算告警任务设 `false`，不计入 signals_10s |

> 环境变量用 `${VAR:default}` 语法展开（如 `model_path`）。

## 模块注册

在 [workflows/\_\_init\_\_.py](../../../../app/services/inference/workflows/__init__.py) 补 import 和 `__all__`，否则动态 import 找不到类。
