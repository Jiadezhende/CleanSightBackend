# 时序分析事实升格到 `app/domain/fact.py`：产出者身份归一、时间轴钉死

> **变更状态**：进行中（2026-09-21）　<!-- 新契约已落地且零调用点；旧型仍在 inference/types.py 里跑，codec 与调用点迁移未开工 -->
> **知识库**：待沉淀
>
> <!-- 推理域数据层接线的第 1 步：先定数据模型，再写 codec，最后迁调用点 -->

## 概述

新建 `app/domain/fact.py`，把 `EventFact` / `SegmentFact` 从 `app/services/inference/types.py`
升格为跨服务契约，同时做三处订正：`ts` 必填、`source` + `meta["producer"]` 合成 `producer`、
`to_json` / `type` 判别字段剥离。本批**只写形状**，零调用点，运行时行为不变；旧型原样留着。

## 变更背景

### 现状 / 痛点

`{task}/{step}/` 下有两份推理产物：`features.jsonl`（L1 目标检测，每帧一行）与
`facts.jsonl`（L3 时序分析，每条一行）。前者的落盘能力已经进了数据层
（[`app/storage/feature.py`](../../app/storage/feature.py)），后者**刻意没迁**——理由写在那个模块的
docstring 里：`EventFact` / `SegmentFact` 住在 `app.services.inference.types`，
`app/storage/` 的依赖白名单不许 import 服务层，货币只能退成 `dict`，与 features 侧收发
`FrameFeature` 不对称。于是一份产物的路径知识劈成两处。

这是 storage 分层重构里一直挂着的待拍板项（facts 的货币），本批定案：**升格**。
另有两条在定模型时发现的缺陷，一并订正（见下表 #2 #3）。

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | 事实契约在服务层，数据层够不着 | 阻断 `facts.jsonl` 进数据层 |
| #2 | `EventFact.ts` 默认 `time.time()` | 潜在正确性：默认值是「构造这条记录的墙钟」而非帧捕获 ts，与录像差一个推理延迟，漏传不报错 |
| #3 | `source` 与 `meta["producer"]` 装同一个值 | 两个真源，runner 要写一条校验去比对二者相等 |

### 触发来源

定推理域的数据模型（`storage` 层该收发什么形状）时的评审结论。

## 方案详情

### 全景：三份产物、两个形状，本批只落左下角

```text
L1 目标检测  ──▶  features.jsonl        FrameFeature           app/domain/detection.py  （不动）
L3 时序分析  ──▶  facts.jsonl           EventFact / SegmentFact app/domain/fact.py      ← 本批
离线策略调试 ──▶  offline_debug.json    Mapping[str, Any]       不给形状（有意）

           ▲ 三者共用一条时间轴：帧捕获墙钟 ts（epoch 秒），与 HLS sidecar .idx 同源同值
```

| 步骤 | 落在哪 | 状态 |
|------|--------|------|
| 1. 事实形状升格 + 三处订正 | `app/domain/fact.py` | ✅ 本批 |
| 2. 域 codec（`_fact_to_record` / `_record_to_fact`，`type` 判别在这里） | `app/storage/inference/_temporal.py` | ✅ 见 [20260921_STORAGE_INFERENCE_DOMAIN](20260921_STORAGE_INFERENCE_DOMAIN.md) |
| 3. 域成员（`read_facts` / `write_facts` / `write_debug_result` / `delete`） | 同上 | ✅ 同上 |
| 4. 调用点迁移：`FactLedger` 退场，`offline/runner.py` 直调数据层 | `inference/offline/` | ✅ 见 [20260922_INFERENCE_WRITE_PATH](20260922_INFERENCE_WRITE_PATH.md) |
| 5. 删 `inference/types.py` 的旧两型 | — | ✅ 同上（`inference/feature/` 子包一并删除） |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A（采用）升格到 `app/domain/fact.py` | 改 6 处 import；`app.domain` 多一个 stdlib-only 模块（比 `detection.py` 还轻，不带 numpy） | 与 features 侧对称：domain 出形状，storage 出 codec |
| B 数据层只收发 `dict` | 零 import 改动 | 否：`type` 判别字段是落盘格式知识却留在服务层，codec 劈成两半 |

### 1. `app/domain/fact.py` — 形状与三条硬约束

```python
@dataclass
class EventFact:      # 点：某信号在某一帧上的电平
    producer: str; signal: str; value: Any; ts: float
    conf: float = 1.0; meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SegmentFact:    # 区间：一段时间里的一个动作 / 状态，闭区间 [start, end]
    producer: str; label: str; start: float; end: float
    conf: float = 1.0; meta: Dict[str, Any] = field(default_factory=dict)

Fact = Union[EventFact, SegmentFact]    # 读写两侧的货币
```

- **时间轴**：`ts` / `start` / `end` 均为帧捕获墙钟 ts（epoch 秒），与 `FrameFeature.ts` 和
  HLS sidecar `.idx` 的逐帧数组同源同值。整条回溯链路（事实 ↔ 录像互相定位）建立在这条上。
- **`producer` 是产出者身份唯一真源**：幂等替换按它过滤，不再往 `meta` 盖第二份。
- **`meta` 只放伴随观测量**：任何被代码读来做判断的键都不许进去（`meta["producer"]` 正是反例）。

身份键 `(task_id, step_id)` 不进形状——由落盘路径携带，同 `SegmentRef` 不带两个 id。

### 2. 三处订正（旧 → 新）

| # | 旧 | 新 | 理由 |
|---|----|----|------|
| 1 | `ts: float = field(default_factory=time.time)` | `ts: float` 必填 | 涉及正确性的参数不给默认值；漏传现在是 `TypeError` |
| 2 | `source`（自述「来源检测点」）+ `meta["producer"]` | 单个 `producer` | 实际语义早就是 producer——[runner.py](../../app/services/inference/offline/runner.py) 强制 `f.source == segmenter.name`。一个算子订阅多条流时（`clean_monitor` 订阅 `clean_large` + `clean_small`）也填不出单个检测点 |
| 3 | dataclass 自带 `to_json` / `from_json`，`type` 字段写在里面 | 都不要，留给数据层 codec | 落盘格式知识不进 domain。内存里判别就是 `isinstance` |

订正 3 连带两个非落盘调用方要换 `dataclasses.asdict`：`offline/cli.py` 的查询打印、
`offline/impl/clean.py` 的调试 payload。**本批未动**，见遗留。

### 3. 保留项（不改动）

- `app/services/inference/types.py` 的旧 `EventFact` / `SegmentFact` / `fact_from_json` 原样留着，
  在线与离线链路仍走它们。字段已改名，两边混用是 `TypeError` 而不是静默错。
- `FrameFeature` 不动：落盘投影（mask / keypoints / extra / metadata / success 不落）维持现状。
- 两型都保留：`EventFact` 今天零生产者（`temporal/operator.py` 改共享 `_sm` 后不再产事实对象），
  但实时信号那一档在跑（配置里 `realtime: true` → `signals_10s` 推前端），它是那条流的落盘形状。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 事实契约位置 | `app/services/inference/types.py`（数据层够不着） | `app/domain/fact.py`（白名单内） |
| 产出者身份 | `source` 字段 + `meta["producer"]` 两处 | `producer` 一处 |
| `EventFact.ts` 漏传 | 静默取构造时墙钟 | `TypeError` |
| 运行时行为 | — | 无变化（零调用点） |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_import_hygiene.py` | 31 passed（新模块 stdlib-only，不引 numpy，import 35 ms） |
| 全量 `pytest tests/` | 827 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 同名两型并存 | 混用时构造失败 | 字段改名使其必然报 `TypeError`；第 5 步删旧型后消失 |
| 域 codec 与成员未写 | `facts.jsonl` 仍由 `FactLedger` 读写 | 全景第 2 – 3 步，下一批 |
| `FactLedger.open_fresh` 从来没人调 | 同 `(task, step)` 重启一次 run，旧 facts 原样留着（离线链路休眠中，今天不咬人） | 随第 4 步，由推理域的 `delete(task, step)` 整域清掉 |
| `offline_inference_result.json` 落在 step 根 | 违反「step 根下只有域目录、没有文件」，且绕过定位收口 | 随第 3 步收进域目录 |
