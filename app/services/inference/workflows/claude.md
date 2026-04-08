# 检测 Workflow

## 整体流程

```mermaid
flowchart TD
    CAM["📷 视频流输入<br/>(4路并发)"]
    DECODE["帧拆分<br/>FFmpeg / OpenCV"]
    GPU["🖥️ GPU 推理<br/>YOLO11n 单帧检测"]
    TRACK_Q{"需要跟踪？"}
    TRACK["ByteTrack<br/>跨帧关联 / 分配 track_id<br/>标记 is_new"]
    SM["状态机 & 时序分析<br/>滑动窗口 / 计数器 / 阶段管理"]
    EVENTS["events 列表<br/>→ VisualizationWorker overlay<br/>→ WebSocket 视频帧"]
    ALARM["📢 实时告警<br/>AlarmInfo → persist_alarm<br/>30s 批次 → HTTP POST"]
    VIZ["可视化渲染<br/>OpenCV overlay"]

    CAM --> DECODE --> GPU --> TRACK_Q
    TRACK_Q -- "是（气泡）" --> TRACK --> SM
    TRACK_Q -- "否（弯曲）" --> SM
    SM --> EVENTS
    SM -- "触发实时告警条件" --> ALARM
    EVENTS --> VIZ
```

> **CPU / GPU 分工**：GPU 推理只做无状态单帧处理；ByteTrack、状态机、告警全在 CPU 上完成。

---

## 两种告警模式

| 模式 | 触发时机 | 来源方法 | 去向 |
| ------ | --------- | --------- | ------ |
| 实时告警 | TemporalWorker 1Hz 轮询，条件满足时上升沿触发 | `analyze_temporal()` | persist_alarm → 30s 批次 → HTTP POST 外部数据库 |
| 结算告警 | 任务 terminate 时调用一次 | `finalize()` | 同上，由 `InferenceManager._emit_settlement_alarms()` 驱动 |

> **UI 通知**：`events`（字符串列表）经 VisualizationWorker 渲染为视频帧 overlay，通过 WebSocket 实时推送给前端。`AlarmInfo` 只走持久化，不直接推给前端。

---

## 各检测点详细流程

### 1.1 漏气检测

```mermaid
flowchart TD
    F1["单帧 RGB"]
    D1["YOLO11n-seg<br/>检测 bubble 实例<br/>输出 bbox + conf"]
    T1["ByteTrack<br/>分配 track_id<br/>与 seen_ids 比对标记 new"]
    W1["滑动窗口 3s<br/>统计 new_count_history"]
    M1["birth_rate =<br/>sum(new_count) / 窗口帧数"]
    J1{"birth_rate<br/>> 阈值？"}
    A1["🔴 实时告警<br/>process_violation / high<br/>上升沿触发，持久化上报"]
    N1["✅ 正常<br/>events 显示当前气泡数"]

    F1 --> D1 --> T1 --> W1 --> M1 --> J1
    J1 -- "是（且未锁存）" --> A1
    J1 -- "否" --> N1
```

> 无结算告警，漏气是实时检测，任务结束时 `finalize()` 返回空列表。

---

### 1.2 弯曲动作检测

```mermaid
flowchart TD
    F3["单帧 RGB"]
    D3["YOLO11n-det<br/>检测先端状态<br/>straight / bent"]
    DB["5 帧去抖<br/>连续 5 帧一致才切换状态"]
    SM3{"状态转移<br/>STRAIGHT → BENT？"}
    INC["bend_actions += 1"]
    EV["events：显示进度<br/>弯曲动作 N/required<br/>（overlay，无告警上报）"]
    TERM{"任务 terminate？"}
    J3{"bend_actions<br/>< required？"}
    A3["🟡 结算告警<br/>process_violation / warning<br/>持久化上报"]
    N3["✅ 合格<br/>无结算告警"]

    F3 --> D3 --> DB --> SM3
    SM3 -- "是" --> INC --> EV
    SM3 -- "否" --> EV
    EV --> TERM
    TERM -- "是，调用 finalize()" --> J3
    TERM -- "否" --> F3
    J3 -- "是" --> A3
    J3 -- "否" --> N3
```

> 实时阶段只产出 `events`（进度 overlay），**不上报告警**。  
> 合格（`bend_actions >= required`）无告警；不合格才在 terminate 时上报 warning。
