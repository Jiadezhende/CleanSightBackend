# 时序推理模块更新说明

> **变更状态**：生效中（2026-07-13）
> **知识库**：待沉淀

## 概述

本次更新在 `temporal` 模块中引入了时序推理模块，用于进行动作识别和状态判断。

## 变更内容

### 1. 新增 TemporalOperator 基类

**文件**: `app/services/inference/temporal/operator.py`

#### 1.1 构造函数参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `model_path` | str | 模型权重文件路径 |
| `objects` | Dict[int, str] | 物体类别映射 {class_id: name} |
| `actions` | Dict[int, str] | 动作类别映射 {class_id: name} |

#### 1.2 核心方法

```python
def infer(self, features: "torch.Tensor") -> List[int]:
    """时序模型推理：返回每个时间步的预测类别。"""
```

#### 1.3 惰性模型加载

惰性加载时序模型, 首次推理时触发, 双重检查锁保证线程安全.

```python
def _ensure_model_loaded(self) -> None:
```

通过 torch.jit.load 加载模型文件.

#### 1.4 映射表支持

提供物体类别和动作类别映射表.

```python
def _object_id(self, object_name: str) -> int:

def _object_name(self, object_id: int) -> str:

def _action_id(self, action_name: str) -> int:

def _action_name(self, action_id: int) -> str:

```

### 2. 新增 CleanOperator 子类

**文件**: `app/services/inference/workflows/clean.py`

#### 2.1 窗口管理

窗口管理是时序推理的核心，负责从流式数据中截取固定长度的上下文窗口供模型推理。

##### 2.1.1 流式数据输入

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

##### 2.1.2 多流对齐

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

#### 2.2 特征提取

将检测框转换为特征矩阵：

- 特征格式: `(T, num_objects × 6)`
- 每个物体特征: `(nums, cx, cy, w, h, area)` 
  其中 `nums` 是同类别物体数量，其他特征归一化到 [0, 1]
- 支持多流合并：同一时刻的多流检测结果合并到同一个 feature vector

## 配置示例

```yaml
rules:
  - name: clean_monitor
    subscribes: [clean_large, clean_small]
    class: app.services.inference.workflows.clean.CleanOperator
    params:
      model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/gru-final.pt
      window_seconds: 2.5
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

## 后续改进方向

1. 优化模型评估方案，需仿真线上场景下（而非数据集离线场景）的推理效果
2. 根据实际情况做预测结果平滑处理，避免短时间内预测结果频繁切换
