# 时序推理模块更新说明

> **变更状态**：生效中（2026-07-13）
> **知识库**：待沉淀

## 概述

本次更新在 `temporal` 模块中引入了基于 GRU 的时序推理能力，用于进行动作识别和状态判断。

## 变更内容

### 1. 新增 GRU 模型定义

**文件**: `app/services/inference/temporal/model/gru.py`

```python
class GRUClassifier(nn.Module):
    def __init__(self, input_dim, num_classes, hidden=128, num_layers=3):
        self.rnn = nn.GRU(input_size=input_dim, hidden_size=hidden, ...)
        self.head = nn.Linear(hidden, num_classes)
    
    def forward(self, x):
        # x: (B, T, F) → logits: (B, T, num_classes)
        out, _ = self.rnn(x)
        return self.head(out)
```

- 输入: `(B, T, F)` —— batch × 时间步 × 特征维度
- 输出: `(B, T, C)` —— 每个时间步的类别概率

### 2. 新增 GRUOperator 基类

**文件**: `app/services/inference/temporal/operator.py`

#### 2.1 构造函数参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `model_path` | str | 模型权重文件路径 |
| `objects` | Dict[int, str] | 物体类别映射 {class_id: name} |
| `actions` | Dict[int, str] | 动作类别映射 {class_id: name} |
| `hidden` | int | GRU 隐藏层维度（默认 128） |
| `num_layers` | int | GRU 层数（默认 3） |

#### 2.2 核心方法

```python
def infer(self, features: torch.Tensor) -> List[int]:
    """GRUClassifier 推理：返回每个时间步的预测类别。"""
```

#### 2.3 惰性模型加载

惰性加载 GRUClassifier 模型, 首次推理时触发, 双重检查锁保证线程安全.

```python
def _ensure_model_loaded(self) -> None:
```

#### 2.4 映射表支持

```python
def get_action_name(self, class_id: int) -> str:
    """获取动作名称（安全访问，不存在时返回默认值）"""

def get_object_name(self, class_id: int) -> str:
    """获取物体名称（安全访问，不存在时返回默认值）"""
```

### 3. 新增 CleanOperator 子类

**文件**: `app/services/inference/workflows/clean.py`

#### 3.1 窗口管理

窗口管理是时序推理的核心，负责从流式数据中截取固定长度的上下文窗口供模型推理。

##### 3.1.1 流式数据输入

每次 `analyze()` 调用时，`windows` 参数是一个字典：

```python
windows: Dict[str, List[FrameDetections]]
# {流名: 该流的滑窗快照列表（按 timestamp 升序排列）}
```

例如，`CleanOperator` 订阅了 `clean_large` 和 `clean_small` 两个流：

```python
windows = {
    "clean_large": [FrameDetections(ts=1.0), FrameDetections(ts=2.0), ...],
    "clean_small": [FrameDetections(ts=1.0), FrameDetections(ts=2.0), ...],
}
```

##### 3.1.2 多流对齐

通过 `_zip_by_ts()` 方法按时间戳对齐多流（inner-join）：

```python
aligned_frames = self._zip_by_ts(windows)
# 返回: List[AlignedFrame]
# 每个 AlignedFrame 包含同一时刻所有订阅流的检测结果
```

`AlignedFrame` 结构：

```python
@dataclass
class AlignedFrame:
    ts: float  # 时间戳
    by_source: Dict[str, FrameDetections]  # {流名: 该流的检测结果}
```

对齐后，同一时刻的多流检测结果合并到同一个 `AlignedFrame` 中。

##### 3.1.3 历史窗口维护

`history_frames` 维护当前算子的历史帧序列：

```python
self.history_frames = []  # List[AlignedFrame]，按时间戳升序
```

每次 `_advance()` 调用时：

1. **过滤新帧**：通过 `last_ts` 过滤出本次 tick 新增的帧
2. **扩展历史**：将新帧追加到 `history_frames`
3. **窗口裁剪**：保持 `history_frames` 长度不超过 `window_size`

```python
# 过滤新帧（timestamp > last_ts）
new_frames = [f for f in aligned_frames if f.ts > self._sm["last_ts"]]

# 扩展历史窗口
self.history_frames.extend(new_frames)

# 裁剪到固定窗口大小
self.history_frames = self.history_frames[-window_size:]
```

##### 3.1.4 滑动窗口推理

当 `history_frames` 长度达到 `window_size` 后，对每个新帧进行推理：

以示例说明：**历史窗口 [1, 2, 3]，新帧 [4, 5]，窗口大小 4**

```
扩展后历史: [1, 2, 3, 4, 5]

滑动窗口截取：
  i=0: window_start=0, window_end=4 → [1, 2, 3, 4] → 预测帧4的动作
  i=1: window_start=1, window_end=5 → [2, 3, 4, 5] → 预测帧5的动作

推理完成后裁剪历史: [2, 3, 4, 5]
```

核心计算逻辑：

```python
num_new = len(new_frames)
# 计算起始索引，确保每个新帧都有完整的上下文窗口
start_idx = max(0, len(self.history_frames) - num_new - window_size + 1)

for i in range(num_new):
    window_start = start_idx + i
    window_end = window_start + window_size
    
    # 截取滑动窗口
    window = self.history_frames[window_start:window_end]
    
    # 特征提取 → 推理
    features = self._adapt_to_features(window)
    predictions = self.infer(features)
    self._sm["latest_action"] = predictions[-1]  # 取最后时间步的预测
```

##### 3.1.5 窗口管理流程总结

```
┌──────────────────────────────────────────────────────────────┐
│                    窗口管理完整流程                            │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  1. 输入: windows = {stream_name: [FrameDetections, ...]}    │
│                          ↓                                   │
│  2. 多流对齐: _zip_by_ts(windows) → List[AlignedFrame]       │
│                          ↓                                   │
│  3. 过滤新帧: new_frames = [f for f in aligned if f.ts > last]│
│                          ↓                                   │
│  4. 扩展历史: history_frames.extend(new_frames)              │
│                          ↓                                   │
│  5. 判断窗口是否已满: len(history) >= window_size?           │
│         ├─ NO → 更新 last_ts，等待下一帧                      │
│         └─ YES → 继续                                        │
│                          ↓                                   │
│  6. 滑动窗口推理:                                             │
│     for i in range(num_new):                                 │
│         window = history[start+i : start+i+window_size]      │
│         features = _adapt_to_features(window)                │
│         predictions = infer(features)                        │
│                          ↓                                   │
│  7. 裁剪历史: history_frames = history_frames[-window_size:] │
│                          ↓                                   │
│  8. 更新状态: _sm["last_ts"] = new_frames[-1].ts             │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

#### 3.2 特征提取

将检测框转换为特征矩阵：

- 特征格式: `(T, num_objects × 4)`
- 每个物体特征: `(cx, cy, w, h)` 归一化到 [0, 1]
- 支持多流合并：同一时刻的多流检测结果合并到同一个 feature vector

## 配置示例

```yaml
rules:
  - name: clean_monitor
    subscribes: [clean_large, clean_small]
    class: app.services.inference.workflows.clean.CleanOperator
    params:
      model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/gru-final.pt
      window_seconds: 10.0
      actions:
        0: idle
        1: air_injection
        2: flush
        3: long_brush_insert
        4: long_brush_withdraw
      objects:
        0: hand
        1: scope_control_body
        2: scope_mid_section
        3: scope_distal_end
        4: syringe
        5: air_gun
        6: short_brush
        7: brush_tip_out
      hidden: 256
      num_layers: 4
```

## 数据流

```
检测结果 (FrameDetections)
    ↓
多流对齐 (_zip_by_ts)
    ↓
特征提取 (_adapt_to_features)
    ↓
滑动窗口推理 (infer)
    ↓
动作预测 → judge() → events + alarms
```

## 注意事项

1. **模型兼容性**: 模型权重需与 `GRUClassifier` 结构一致
2. **特征维度**: `input_dim = num_objects × 4`，需与训练时保持一致
3. **设备要求**: 建议使用 GPU 加速推理（CUDA）

## 后续改进方向

1. 优化模型评估方案，需仿真线上场景下（而非数据集离线场景）的推理效果
2. 支持模型动态切换，根据场景需求选择不同模型推理
3. 根据实际情况做预测结果平滑处理，避免短时间内预测结果频繁切换
