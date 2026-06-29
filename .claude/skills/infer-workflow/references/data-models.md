# 数据模型速查

来自 [data_models.py](../../../../app/services/inference/data_models.py)。构造对象时照抄字段，不要臆造。

```python
# 单个检测框
Detection(bbox=[x1, y1, x2, y2], confidence=0.9, class_id=0, class_name="bubble")

# 检测输出（Detector 返回值）
DetectionOutput(detections=[...], metadata={...}, timestamp=time.time(), success=True)

# 事实（L3 Analyzer.post_process 产出）—— 某信号在 ts 的当前电平
EventFact(
    source="bubble",        # = Analyzer.name
    signal="birth_rate",    # 信号名，Judge 按此取值（一个 Analyzer 一 tick 可产多信号）
    value=0.7,              # 该信号在 ts 的当前值（数值/字符串/任意）
    ts=time.time(),
    conf=1.0,               # 可选
    meta={...},             # 可选；⚠️ 不放阈值/required（那是 Judge 的事）
)

# 告警（L4 Judge.step 实时上升沿 / Judge.finalize 结算时产出）—— 只有这 4 个字段
AlarmInfo(
    alarm_type=AlarmType.PROCESS_VIOLATION,   # PROCESS_VIOLATION="流程违规" / TASK_TIMEOUT="任务超时" / MOCK
    alarm_level="high",                       # "low" / "medium" / "high" / "critical" / "warning"
    alarm_message="...",
    metadata={...},
)

# 可视化数据（prepare_visualization_data 返回值）
VisualizationData(type=VisualizationType.BBOX, items=[...],
                  status_text="...", status_color=(B,G,R), status_position="top-right")

# 可视化条目
VisItem(bbox=[x1,y1,x2,y2], label="...", confidence=0.9, color=(B,G,R))   # 颜色是 BGR
```

> **告警由 Judge 产出**：实时告警走 `Judge.step()` 返回的 `alarms`（上升沿锁存），结算告警走 `Judge.finalize()`。**Analyzer 只产 `EventFact`，绝不产 `AlarmInfo`**。

> Judge 在 `step()` 里先 `frame = self._frame(facts)` 建 `{signal: fact}` 快照，再 `frame.get("birth_rate")` 按信号名取值。

> `alarm_mode`（REALTIME / SETTLEMENT）和 `alarm_metric` 由 InferenceManager 在持久化时**自动补上**，**不要**写进 `AlarmInfo`。

> 颜色都是 **BGR**（不是 RGB）：红 `(0,0,255)`、绿 `(0,255,0)`、蓝 `(255,0,0)`。
