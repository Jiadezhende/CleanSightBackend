# 实时推理链路重构为流处理框架：流源 Detector / 流算子 Operator 两粒度解耦

> **变更状态**：生效中（2026-06-27）
> **知识库**：待沉淀
>
> 相关：[workflows/CLAUDE.md](../../app/services/inference/workflows/CLAUDE.md)（开发者指南已同步）、[20260627_INFRA_ASSEMBLY_DECOUPLE.md](20260627_INFRA_ASSEMBLY_DECOUPLE.md)（装配解耦前序）、[20260627_INFER_CONTRACT_PURITY.md](20260627_INFER_CONTRACT_PURITY.md)（stage 主键/契约前序）。

## 概述

- **改了什么**：把写死的 `Detector + TemporalAnalyzer + Judge` 三元组（按 `name` 1:1 绑定）重构为**流处理框架**——流源 `Detector`（分组粒度）+ 流算子 `Operator`（规则粒度，`analyze`+`judge` 合并为一个对象、共享一份状态机），Actor 注册多个算子、各自按 `subscribes` 订阅上游流。
- **为什么改**：三元组把「检测分组」「时序规则」两种不同粒度绑死，clean 已被迫用两个独立 mock 三元组硬撑；且 analyzer/judge 双对象各存一份状态（bending 的 `bend_actions` 重复且每 tick 拷贝同步）。需要一套可插拔机制快速接入新模型，并为不同因果时序模型支持不同感受野。
- **影响面**：推理 workflow 全量（基类 + bubble/bending/mock）、YAML schema（`models[]` → `detectors[]`/`rules[]`/`offline`）、config/factory 装配、temporal Actor、ClientQueues slide_window。**干净切换，不保留旧 schema**。离线 segmenter 段本次仅留占位。

## 改动详情

### 1. `workflows/operator.py`（新增）— 流算子基类，合并 analyze+judge

`Operator` 单一抽象，两接口共享 `self._sm`：
- `analyze(windows)`：按 `subscribes` 收订阅流、`_clip` 到 `window_seconds` 感受野、推进 `_sm`（不返回）。
- `judge()`：读 `_sm` 出 `(overlay 文案, 告警)`。`finalize()`：结算。
- 基类工具：`_clip`（裁到感受野）、`_zip_by_ts`（多流按 ts inner-join 对齐，同帧 ts 精确相等）、`primary_window`（单订阅便捷）。

三个正交维度：`name`（自身/输出身份）、`subscribes`（输入流清单，**显式必填，无 `[name]` 默认**）、`window_seconds`（感受野）。删除 `workflows/analyzer.py`、`workflows/judge.py`。

### 2. `workflows/{bubble,bending,mock}.py` — 三件套合并为 `XxxOperator`

`BubbleOperator` / `BendingOperator` / `MockOperator`：detector 不变，analyzer+judge 合并。

> **关键**：`BendingOperator` 的 `bend_actions` 由「Analyzer._sm + Judge._sm 双份 + 每 tick 拷贝」收敛为**单份共享状态**，消除双状态机同步。

### 3. `config/inference_config.yaml` — schema 拆分（干净切换）

#### 旧
```yaml
models:
  - name: bubble
    class: ...BubbleDetector
    analyzer_class: ...BubbleAnalyzer
    judge_class: ...BubbleJudge
    params: {...}
    analyzer_params: {...}
    judge_params: {...}
```

#### 新
```yaml
detectors:
  - {name: bubble, class: ...BubbleDetector, params: {...}}
rules:
  - name: bubble_leak
    subscribes: [bubble]          # 输入流名 = detector.name
    realtime: true
    class: ...BubbleOperator
    params: {window_seconds: 3.0, birth_rate_threshold: 0.5}
offline: {}                       # 占位，本次未实现
```

- `realtime` flag 从 model 级移到 rule 级。
- **CLEAN `rules: []`**：不建 Operator/Actor，仅 detector 叠检测框，消除两个 `trigger=999` 空转 mock。
- **MOCK 保留 `mock_passthrough`**：未知 step 透传行为不变。

### 4. `config.py` / `stage_factory.py` — 装配

- `StageConfig` 读 `detectors`/`rules`/`offline`，删 `models`。
- `create_operators_for_stage`：产 `(OperatorClass, kwargs)`，注入 `subscribes`（缺失 fail-fast 跳过）。
- [`build_task_metric_map`](../../app/services/inference/stage_factory.py)：遍历 `realtime:true` 规则的 `subscribes`，**map key = detector 流名**（非 rule 名）。

### 5. `workers/temporal.py` — Actor 持算子列表 + per-operator 隔离

`_tick` 按 `op.subscribes` 收多流 → `op.analyze` → `op.judge`；**每个算子独立 try/except**（修复旧版整 tick 一个 try、单算子异常中断同 tick 其余的缺陷）。

### 6. `core/manager.py` — 按感受野配流

set_task 实例化算子后，按 `{流: max(订阅算子 window_seconds)}` 调 `cq.set_stream_windows(...)`。

### 7. `client/queues.py` — slide_window 按感受野

`push_detection` 淘汰 cutoff 改为 `ts - max(底线 10s, 该流感受野)`。新增 `set_stream_windows`。

> **不变式**：保留 10s 底线，感受野只**向上扩展**，故 `signals_10s`（复用 slide_window）不受影响；算子在 `analyze` 内自行 `_clip` 到各自感受野。

### 8. 保留项（刻意不改）

- `EventFact` / `SegmentFact` 数据模型与 `FactLedger` 保留（离线契约，本次在线链路已不产 EventFact）。
- `signals_10s` / `get_signals_10s` 行为与 10s 语义不变。

## 数据通道 / 行为说明

| 概念 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| 流（slide_window，key=detector.name） | 推理写回 `push_detection` | 算子 `analyze` 按 subscribes 取 | 缓冲长度改为 max(10s, 感受野) |
| 算子状态 `_sm` | `analyze` 推进 | `judge`/`finalize` 读 | analyze+judge 共享单份（原双份） |
| 实时告警 | `operator.judge()` 上升沿 | persist → 30s 批次上报 | 来源方法改名，行为不变 |
| 结算告警 | `operator.finalize()` | `finalize_and_stop` 收集 | 同上 |
| 事实（EventFact） | —— | —— | **在线链路下线**（状态共享替代传输） |

## 后续计划

1. **离线 segmenter**：`offline` 字段已占位；接入单 segmenter（stage 粒度）消费 FeatureStore 全序列 → `SegmentFact` 落 FactLedger，规则层后置。
2. **`/infer-workflow` skill**：仍按旧三件套模板生成，需同步到 Operator 模板（detectors[]/rules[]/subscribes）。

## 验证

| 项 | 结果 |
|----|------|
| 流框架新增单测 `tests/test_operator_framework.py` | 8 passed（subscribes 注入 / `_clip` / 多流 zip / 缓冲底线与扩展 / per-operator 隔离） |
| 行为对齐 `tests/test_temporal_debounce.py`（重写为 Operator API） | 全绿（bubble 上升沿、bending 去抖/结算逐位一致） |
| 路由 `tests/test_inference_stage_routing.py` / signals `tests/test_alarm_increment.py` | 全绿 |
| 配置加载 + 工厂装配自检 | 三 stage 正确产出 detectors/operator_specs，metric_map={'bubble': BUBBLE} |
| 全量 `pytest tests/` | 211 passed |
