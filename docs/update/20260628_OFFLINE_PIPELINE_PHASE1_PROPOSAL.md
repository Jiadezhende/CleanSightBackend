# 离线链路一期开发提案：显式 task + step 驱动的特征分段流水线

> **变更状态**：提案（2026-06-28）　<!-- 可立即开发的一期范围；不接自动触发与在线运行态 -->
> **知识库**：无需沉淀（提案；落地后另行沉淀）
>
> 相关：[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（online/offline 分叉与现有落盘契约）、[20260627_STREAM_OPERATOR_FRAMEWORK.md](20260627_STREAM_OPERATOR_FRAMEWORK.md)（offline stage 占位来源）。

## 概述

- **做什么**：实现第一条可运行的离线流水线：调用方显式给出 `(task_id, step_id)`，系统从 FeatureStore 读取完整检测序列，经 stage 配置实例化一个 `OfflineSegmenter`，产出 `SegmentFact` 并幂等写入 FactLedger。
- **为什么现在可以做**：FeatureStore、FactLedger、FrameDetections、SegmentFact 和 `stages.*.offline` 配置占位均已存在；纯离线算法不需要 client、CQ 或在线 Actor。
- **一期边界**：只提供同进程 service API + 手动 CLI，不接任务结束自动触发，不访问 ClientManager，不产告警，不修改数据库。

## 现状基座

当前已有：

```text
{storage}/{task_id}/{step_id}/features.jsonl
    ↓ FeatureStore.load(...)
List[FrameDetections]

SegmentFact
    ↓ FactLedger.append/load(...)
{storage}/{task_id}/{step_id}/facts.jsonl
```

- `FeatureStore.load(task_id, step_id, source)` 可以按 detector name 回读完整序列；
- `FactLedger` 可以序列化/反序列化 `SegmentFact`；
- `StageConfig.offline` 已解析为 dict，但没有工厂和执行器；
- `offline: {}` 当前表示未配置；
- 在线 Operator 是 per-client、实时、有状态组件，不应被离线链路复用。

当前缺失：

- 离线处理基类；
- offline 配置 schema 校验和工厂；
- 多 source 单次加载；
- runner / CLI；
- 幂等覆盖旧分段结果；
- 离线链路单元与组件测试。

## 一期架构

```text
CLI / Python API
      │ OfflineRunSpec(task_id, step_id)
      ▼
OfflineRunner
      ├─ stage_key = str(step_id)
      ├─ StageFactory.create_offline_segmenter(stage_key)
      ├─ FeatureStore.load_many(task_id, step_id, subscribes)
      ▼
OfflineSegmenter.segment(streams)
      │
      ▼ List[SegmentFact]
校验 + 排序 + producer metadata
      │
      ▼
FactLedger.replace_segments(task_id, step_id, producer, facts)
```

离线链路只识别稳定存储键 `(task_id, step_id)`；`client_id` 不进入接口。

## 核心接口

### 1. `OfflineSegmenter`

新增 `app/services/inference/offline/base.py`：

```python
from abc import ABC, abstractmethod
from typing import Mapping, Sequence

from app.domain.detection import FrameDetections
from app.services.inference.models import SegmentFact


class OfflineSegmenter(ABC):
    def __init__(self, name: str, subscribes: list[str]):
        if not name:
            raise ValueError("offline segmenter name is required")
        if not subscribes:
            raise ValueError("offline segmenter subscribes is required")
        self.name = name
        self.subscribes = list(subscribes)

    @abstractmethod
    def segment(
        self,
        streams: Mapping[str, Sequence[FrameDetections]],
    ) -> list[SegmentFact]:
        """消费按 timestamp 升序的完整检测序列，返回动作分段事实。"""
```

约束：

- 一个 stage 一期只允许配置一个 segmenter；
- segmenter 不访问 FeatureStore、FactLedger、ClientManager、CQ 或数据库；
- 输入序列只读，按 timestamp 升序；
- segmenter 自己决定多流如何对齐，基类不强制 inner join；
- 输出 `SegmentFact.source` 必须等于 segmenter name；
- 输出必须满足 `start <= end`、时间为有限数、`0 <= conf <= 1`；
- Runner 统一按 `(start, end, label)` 排序；
- 不禁止不同 label 的时间段重叠，业务模型自行决定。

### 2. offline 配置

沿用 `stages.<step_id>.offline`，一期 schema：

```yaml
offline:
  enabled: true
  name: clean_action_segmenter
  subscribes: [clean_large, clean_small]
  class: app.services.inference.offline.clean.CleanActionSegmenter
  params:
    # 算法私有参数
```

规则：

- `{}` 或 `enabled: false` 表示该 stage 不启用离线分段；
- `name / subscribes / class` 在 enabled 时必填；
- `subscribes` 必须全部命中同 stage 的 detector name；
- `params` 原样传入实现类；
- 工厂自动注入 `name` 和 `subscribes`，算法配置不得重复声明；
- 配置或类加载错误 fail-fast，不静默跳过。

`StageFactory` 增加：

```python
def create_offline_segmenter(
    self, stage_name: str
) -> OfflineSegmenter | None:
    ...
```

现有生产配置暂不启用具体 segmenter；框架测试使用临时配置和 fake 实现。算法实现合并时再启用对应 stage。

### 3. FeatureStore 多流读取

增加：

```python
def load_many(
    self,
    task_id: int,
    step_id: int,
    sources: Sequence[str],
) -> dict[str, list[FrameDetections]]:
    ...
```

要求：

- 单次顺序扫描 `features.jsonl`，不能为每个 source 重复读取整文件；
- 每个 source 保留所有包含该 key 的帧，包括 detections 为空的帧；
- 返回的每条序列按 `ts` 升序；
- 文件不存在返回所有 source 对应的空列表；
- 单行损坏记录 warning 后跳过，不中断其余数据；
- `load()` 改为委托 `load_many(..., [source])`，保持现有接口兼容。

一期采用全量内存加载；流式/分块读取留后续性能任务。

### 4. Runner

新增 `app/services/inference/offline/runner.py`：

```python
@dataclass(frozen=True)
class OfflineRunSpec:
    task_id: int
    step_id: int


@dataclass(frozen=True)
class OfflineRunResult:
    status: str
    producer: str | None
    segment_count: int
    message: str = ""


class OfflineRunner:
    def run(self, spec: OfflineRunSpec) -> OfflineRunResult:
        ...
```

执行规则：

1. `stage_key = str(spec.step_id)`；
2. stage 不存在：返回 `status="skipped"`；
3. offline 为空或 disabled：返回 `status="skipped"`；
4. 按 `subscribes` 调用 `FeatureStore.load_many()`；
5. 任一订阅 source 序列为空：返回 `status="skipped"`，不覆盖旧事实；
6. 调用 segmenter；算法异常记日志并向调用方抛出，不写 FactLedger；
7. 完整校验所有 SegmentFact；任一非法则整批失败，不部分写；
8. 为每条事实补 `meta.producer`，已有不同值视为契约错误；
9. 按统一规则排序；
10. 原子替换该 producer 之前的 SegmentFact；
11. 返回 `status="completed"` 和 segment_count；空结果也是 completed，并清除该 producer 的旧分段。

Runner 不查询任务是否在线，也不调用 FeatureStore 在线单例执行 flush。调用方必须保证输入已封口。

### 5. FactLedger 幂等替换

增加：

```python
def replace_segments(
    self,
    task_id: int,
    step_id: int,
    producer: str,
    facts: list[SegmentFact],
) -> None:
    ...
```

语义：

- 先 flush 当前 `(task_id, step_id)` 缓冲；
- 读取既有 `facts.jsonl`；
- 保留所有 EventFact；
- 保留 `meta.producer != producer` 的其他 SegmentFact；
- 删除相同 producer 的旧 SegmentFact；
- 追加本次新事实；
- 写同目录临时文件并 `os.replace()` 原子替换；
- 写入或校验失败时保留旧文件；
- 同一进程内使用 per-(task,step) 锁串行 replace；
- 一期不支持多个进程同时运行同一个 task+step，CLI 文档明确该限制。

该设计保证重复手动执行不会不断追加重复分段。

## 手动入口

新增：

```bash
python -m app.services.inference.offline.cli \
  --task-id 100 \
  --step-id 2
```

CLI：

- 使用 `settings.storage_base_dir`；
- 使用正式 inference config；
- 输出单行结果：status、producer、segment_count；
- completed/skipped 返回退出码 0；
- 配置错误、输入损坏、算法异常、写入失败返回非 0；
- 不启动 FastAPI、InferenceManager、ClientManager 或任何在线 Worker；
- 不新增 HTTP 路由。

同时保留可测试的 Python API：

```python
OfflineRunner(...).run(OfflineRunSpec(task_id=100, step_id=2))
```

## 并行开发拆分

### 工作包 A：框架与配置

- `OfflineSegmenter`；
- offline schema 校验；
- `StageFactory.create_offline_segmenter()`；
- fake segmenter 和工厂测试。

### 工作包 B：存储与 Runner

- `FeatureStore.load_many()`；
- `FactLedger.replace_segments()`；
- `OfflineRunner`；
- CLI；
- 幂等、异常和原子替换测试。

### 工作包 C：业务算法

- 基于稳定的 `OfflineSegmenter.segment(streams)` 开发具体 stage 算法；
- 使用导出的 `features.jsonl` 样本离线调试；
- 产出 `SegmentFact`，不接触 Runner/Store；
- 算法验收数据集、标签定义和阈值由业务侧另行确认。

A 与 B 可并行；C 只依赖本提案中的基类接口，可先用本地 adapter 开发，待 A 合并后接入。

## 明确不做

- 不在任务 terminate 时自动触发；
- 不判断 CQ ACTIVE/DRAINING/CLOSED；
- 不查询 client、ClientManager 或数据库任务状态；
- 不执行 Offline Judge、不生成 Alarm、不上报告警；
- 不新增后台队列、WorkerPool、定时扫描或 FastAPI endpoint；
- 不修改在线 Operator；
- 不把 client_id 写入离线文件；
- 不引入 `{task}/{step}/{run}` 新目录层级；
- 不承诺对仍在写入的 features.jsonl 得到完整、稳定结果。

## 与运行身份/CQ 改造的接口

一期可以独立开发，但自动化接入必须等待：

1. 消息上下文传播保证 FeatureStore 不会把迟到结果写入错误 task+step；
2. CQ 生命周期提供“在线输入封口且 FeatureStore flush 完成”的完成信号；
3. 运行身份任务明确同一个 `(task_id, step_id)` 是否允许多次运行；
4. 如果允许多 run，另开存储迁移任务决定是否引入 run 分区。

在这些问题完成前，CLI 只允许针对人工确认已经停止写入的数据运行。

## 测试与验收

### 契约测试

- offline `{}` / disabled / enabled 配置；
- 缺 name、class、subscribes 或引用未知 detector 时 fail-fast；
- fake segmenter 收到按 source 分组、按时间排序的完整序列；
- 非法 SegmentFact 整批拒绝。

### 存储测试

- `load_many` 单次读取多 source，空检测帧不丢；
- 文件不存在、空文件、单行损坏；
- `replace_segments` 重跑不重复；
- 更换 producer 不互相覆盖；
- EventFact 保留；
- 空结果清除该 producer 旧分段；
- 写入异常不破坏旧 facts 文件。

### Runner / CLI 测试

- unknown stage、offline disabled、缺输入、正常完成、算法异常；
- 同一 spec 连跑两次结果一致；
- CLI 参数、退出码和输出；
- 不 import/启动 ClientManager、InferenceManager 或在线 Worker。

### 一期验收

- `pytest tests/` 全量通过；
- 新增离线测试使用临时 storage/config，不依赖 GPU、RTSP、数据库和网络；
- 给定固定 features fixture，CLI 可稳定生成预期 SegmentFact；
- 连续执行两次 facts.jsonl 无重复；
- 现有 online 数据流、HTTP、HLS 与告警行为不变。

## 已知限制

- 当前存储键只有 `(task_id, step_id)`；如果同一组合包含多次运行的数据，一期无法拆分；
- Runner 无法证明在线写入已结束；
- 全量加载可能占用较多内存；
- 不支持同一 task+step 的跨进程并发执行；
- 只产分段事实，不决定合规性或告警。
