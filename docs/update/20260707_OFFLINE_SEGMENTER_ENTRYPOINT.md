# 离线分割入口落地（第一步）：独立进程 + 可插拔策略 + 输入预处理预留层

> **变更状态**：生效中（2026-07-07）　<!-- 离线链路开发第一步；框架 + 占位策略已可端到端跑，真实模型与调度层后续接 -->
> **知识库**：待沉淀　<!-- 沉淀时更新 [ARCHITECTURE_DATA_FLOW.md](../kb/ARCHITECTURE_DATA_FLOW.md) 的「online/offline 分离」段：离线消费端由「待实现」改为「入口已落地，见本记录」 -->
>
> 相关真源：[kb/ARCHITECTURE_DATA_FLOW.md](../kb/ARCHITECTURE_DATA_FLOW.md)（`FeatureStore.load → OfflineSegmenter → FactLedger` 链路定名）、[kb/DESIGN_EXTENDING_DETECTION.md](../kb/DESIGN_EXTENDING_DETECTION.md)（「加一子类 + YAML 一行」扩展范式）。
> 承接：建立在 [20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md](20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md) 提案之上，并按开发期实际需求把范围收敛为「先出一个干净的离线推理入口」，把排队/自动调度显式推后（见文末后续计划）。

## 概述

- **改了什么**：填上 KB 里「离线消费端待实现」的那半程——落地一条可运行的离线全序列分割入口：起一个**独立进程**，对指定 `(task_id, step_id)` 从 FeatureStore 读完整 bbox 特征序列，经**可插拔策略**产 `SegmentFact` 幂等写 FactLedger。策略与真实模型解耦，先用 presence 型占位策略把链路跑通。
- **为什么改**：离线动作分割需要一条不抢占实时链路的消费端；这是离线链路开发的**第一步**，先把「进程隔离 + 策略接缝 + 预处理预留层 + 幂等落盘」这套骨架立住，后续的调度、可观测、可扩展都挂在它上面。
- **影响面**：新增 `inference/offline/` 框架层与 `offline/segmenters/` 实现层，扩 `feature/store.py`（多流读 + 幂等替换）与 `stage_factory.py`（离线策略工厂）。**在线链路零改动**；生产 `inference_config.yaml` 的 `offline` 保持 `{}` 不启用。

## 设计约束（本期硬需求）

| 约束 | 落地方式 |
|------|---------|
| 不抢占实时链路资源 | 独立 OS 进程（不进 uvicorn）；入口在任何 torch import 前置 `CUDA_VISIBLE_DEVICES=""` + `torch.set_num_threads`（默认 2）；文档建议 `nice -n 15` 起进程 |
| 输入不一定能直接喂模型 | `OfflineSegmenter.preprocess` 作为**独立预处理预留层**（默认恒等透传；重模型 override 做张量化/归一化/时间降采样/定长编码） |
| 可换不同离线推理策略 | 一策略 = 一 `OfflineSegmenter` 子类，YAML `offline.class` 选定，CLI `--strategy` 可覆盖对比 |
| 先用 JSON 存结果 | 结果走 `facts.jsonl`（`SegmentFact`），不碰 DB、不开 HTTP 路由 |

## 改动详情

### 1. `app/services/inference/feature/store.py` — 离线读写引擎

- **`FeatureStore.load_many(task_id, step_id, sources)`**：单次顺序扫 `features.jsonl` 读多个 source（clean 类 stage 订阅 `clean_large`+`clean_small` 需一致扫描），空检测帧保留、按 `ts` 升序、损坏行跳过；`load()` 委托其单 source 版本。
- **`FactLedger.replace_segments(task_id, step_id, producer, facts)`**：**幂等替换**某 producer 的分段——保留所有 EventFact 与其他 producer 的 SegmentFact，删同 producer 旧分段，追加新事实，临时文件 `os.replace()` 原子替换；写/序列化失败保留旧文件；全程持 `_JsonlBuffer._lock` 串行（与在线 append 的 `_write`/`open_fresh` 互斥）。开发期重复手动跑不累积重复分段。

### 2. `app/services/inference/offline/base.py` — 策略基类（两段接缝）

`OfflineSegmenter(ABC)`：`preprocess(streams) -> model_input`（**预处理预留层**，默认透传）+ `segment(model_input) -> list[SegmentFact]`（抽象）。约束：输入只读、不访问 Store/Ledger/ClientManager/DB（纯算法）。

### 3. `app/services/inference/offline/runner.py` — 编排层

`OfflineRunner.run(OfflineRunSpec)`：按 `step_id` 取 stage → 建策略（未启用 `skipped`）→ `load_many` 读订阅流（任一空 `skipped`，不覆盖旧事实）→ `preprocess` → `segment` → 全量校验（`source==name`、`start<=end`、有限数、`0<=conf<=1`，任一非法整批失败不部分写）→ 补 `meta.producer` → 按 `(start,end,label)` 排序 → `replace_segments`。自建绑定 `settings.storage_base_dir` 的 Store/Ledger，**不复用在线单例**（本就独立进程）。

### 4. `app/services/inference/offline/segmenters/` — 实现层（不外散）

框架（base/runner/cli）与实现分离：策略实现全收 `segmenters/`，一策略一自包含模块（预处理+模型+解码内聚）。本期 `clean_action.py` 的 `CleanActionSegmenter` 为 **presence 型占位策略**（任一订阅 source 在某 ts 有检测即「有动作」，相邻归并成段），仅为跑通链路；真实时序模型后续按同基类新增模块、`preprocess` 在其内 override。

### 5. `app/services/inference/stage_factory.py` — 离线策略工厂

`create_offline_segmenter(stage_name, override_class=None)`：校验 `stages.<step_id>.offline` schema（`{}`/`enabled:false` 不启用；`name`/`subscribes`/`class` 必填；`subscribes` 必须全命中同 stage detector；`params` 不得重复声明 `name`/`subscribes`；缺字段/未知 detector/类加载失败一律 fail-fast），复用 `_import_class` 实例化。

### 6. `app/services/inference/offline/cli.py` — 手动入口

`python -m app.services.inference.offline.cli --task-id N --step-id M [--strategy PATH] [--threads K]`。CPU 隔离在 runner/策略 import **之前**生效；同步跑一次即退；completed/skipped → 0，配置/输入/策略/写失败 → 非 0。不 import 任何在线模块（ClientManager/InferenceManager/FastAPI），与在线后端、mediamtx 网关无代码/进程耦合。

### 7. 保留项（刻意不动）

- 生产 `config/inference_config.yaml` 的 `offline` 保持 `{}`：离线不触碰在线 B2B 测试；具体策略随真实模型落地再开。
- 在线 L1→`_slide_window`→L3 1Hz 链路、HLS、告警行为完全不变。

## 数据通道

| 通道 | 填充 | 消费 | 本期影响 |
|------|------|------|---------|
| `{base}/{task}/{step}/features.jsonl` | 在线 `FeatureStore.append`（L2 落盘，常开） | 离线 `load_many` | 只加读，不改写口径 |
| `{base}/{task}/{step}/facts.jsonl` | 离线 `replace_segments`（本期新增写方） | 离线 `FactLedger.load` / 将来下游 | 在线仍不写 FactLedger（实时不落事实） |

## 后续计划（离线链路仍在建设，本记录随进展续写）

本期只立了「能跑一次」的骨架。要成为可托付的离线服务，还需三条主线，均挂在本期稳定的 runner/策略接口之外，**不回改本期接口**：

### A. 调度合理

- **排队 + 不丢不饿死**：持久化任务队列（先文件 spool：`pending/running/done/failed` 目录 + `os.rename` 原子转移，单消费者无竞争），FIFO 防饿死，毒任务有界重试后进死信，崩溃恢复靠 `replace_segments` 幂等重跑。
- **并发与资源门控**：从单进程单任务 → 进程池；核预算与在线错峰（在既有 CPU 限核基础上定「离线总核数」上限）。
- **手动 → 自动触发**：任务 terminate 时自动入队，前置依赖「在线输入封口 + FeatureStore flush 完成」的完成信号（CQ 生命周期提供），在此之前只对人工确认已停写的数据运行。

### B. 可观测

- **任务态与进度**：每 job 的 status/耗时/产出分段数/失败原因可查（先随 spool 落 JSON，后可入库）。
- **指标**：对齐现有 [`metrics.py`](../../app/utils/metrics.py)，导出离线运行数/时延/失败数/队列深度，供 Prometheus 抓取。
- **结构化日志**：runner 关键节点（skip 原因、校验失败、replace 结果）统一打点，便于排障。

### C. 可扩展

- **真实策略接入**：时序大模型策略按同基类新增模块，`preprocess` 预留层承接张量化/降采样/定长编码；多策略并存与 A/B 对比（`--strategy` 已支持）。
- **下游衔接**：`SegmentFact` → 离线 Judge → Alarm / 入库（本期只产分段，不判合规、不告警）。
- **存储演进**：结果 JSON → DB（查询/聚合）；若同 `(task_id, step_id)` 需多次运行，再引入 run 分区（本期存储键只有二元组）。

## 验证

| 项 | 结果 |
|----|------|
| 新增 [`tests/test_offline_pipeline.py`](../../tests/test_offline_pipeline.py)（引擎/工厂/Runner/占位策略/CLI） | 29 passed |
| 幂等重跑 facts 无重复 / preprocess 预留层被 runner 调用 / 策略异常不写 / 退出码 | 覆盖并通过 |
| 独立 `python -m ...offline.cli`（真实 config，offline 未启用） | 进程独立启动、CPU-only、正确报 `skipped`、退出 0 |
| 全量 `pytest tests/` | 322 passed（在线链路零回归） |
