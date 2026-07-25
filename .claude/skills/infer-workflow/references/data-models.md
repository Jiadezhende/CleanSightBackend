# 数据模型速查

构造对象时照抄字段，不要臆造。契约分散在 `app/domain/`：检测 [detection.py](../../../../app/domain/detection.py)、告警 [alarm.py](../../../../app/domain/alarm.py)、渲染 [render.py](../../../../app/domain/render.py)。

```python
# ── 检测（Detector 产出）── app.domain.detection
Detection(bbox=[x1, y1, x2, y2], confidence=0.9, class_id=0, class_name="bubble",
          extra={...})                          # mask/keypoints/extra 可选

FrameDetections(                                # 一帧里某检测器的全部框，亦作推理最终输出
    detections=[Detection(...), ...],
    metadata={"model": "yolo", "frame_shape": frame.shape},
    timestamp=ts,                               # = 帧捕获真值锚点（infer_batch 的 timestamps[i]）
    success=True, error=None,
)

# ── 告警（Operator.judge 实时上升沿 / finalize 结算时产出）── app.domain.alarm
Alarm(
    alarm_type=AlarmType.PROCESS_VIOLATION,     # PROCESS_VIOLATION="流程违规" / TASK_TIMEOUT / MOCK(仅测试)
    alarm_level="high",                         # "low" / "medium" / "high" / "critical" / "warning"
    alarm_message="...",
    metric=AlarmMetric.BUBBLE,                  # BUBBLE / BENDING / TASK_TIMEOUT / UNKNOWN；产出方显式填
    metadata={...},                             # 触发证据，落库 detection_result
)                                               # mode/stage/seq/timestamp 落库时自动补，别填

# ── 渲染（prepare_visualization_data 返回）── app.domain.render
RenderSpec(type=RenderType.BBOX, items=[RenderItem(...), ...],
           status_text="...", status_color=(B, G, R), status_position="top-right")
RenderItem(bbox=[x1, y1, x2, y2], label="...", confidence=0.9, color=(B, G, R))  # 颜色 BGR
```

> **告警由 Operator 产出**：实时告警走 `judge()` 返回的 `alarms`（上升沿锁存 `_sm["alarming"]`），结算告警走 `finalize()`。analyze 只推 `_sm`、绝不产 `Alarm`。

> `metric` 由产出方**显式填**（`metric=AlarmMetric.XXX`），下游持久化直接读 `alarm.metric`，不靠文案反推。新检测点要新指标 → 先在 `AlarmMetric` 枚举补一项。

> `Alarm.metadata` 里**别放阈值/required 之外的领域字段**当契约；派生量放 `Detection.extra` / `FrameDetections.metadata`。

> `mode`（REALTIME / SETTLEMENT）、`stage`、`seq`、`timestamp` 在落 alarm_log 时由持久化侧/环形缓冲**自动补**，**不要**写进 `Alarm`。

> 颜色都是 **BGR**（不是 RGB）：红 `(0,0,255)`、绿 `(0,255,0)`、蓝 `(255,0,0)`。
