# 内存按字节兜底：ca_ready 独立浅上限、录像段队列按字节限额、启动内存自检

> **变更状态**：生效中（2026-10-13）
> **知识库**：待沉淀

## 概述

三处改动，都不改契约：

- **队列缓存**：`ca_maxlen_seconds` 从 30 降到 20。`ca_ready` 拆出独立上限 `ca_ready_seconds`（2s，按 `inference_fps` 换算成 30 帧），解码背压随之改看这个上限。
- **录像段队列**：新增在途字节预算 `queue_max_mb`（8192），超预算即丢段。
- **启动自检**：启动时按规划路数估算最坏内存，超过 `MemAvailable` 拒绝启动（可改为只告警）。

这是 15 路改造的第 ② 阶段，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **现状**：三条 ca 队列共用一个 `ca_maxlen`（900 帧）；录像段队列只按段数限长（100 段 × 每段约 285MB）。按 15 路估算，最坏约 37GB：转码一停滞，28.5GB 的段先堆在内存里，然后才开始丢段，结果是 OOM 先于丢段发生。
- **已定参数**（2026-10-13 与负责人确认）：按中等性能服务器规划（32GB 内存），`ca_maxlen_seconds` 定为 20。

## 方案详情

### 全景：三种停滞取最大，再加进程基线

```text
单帧 fb = 640×480×3 ≈ 0.92MB，规划路数 N = capacity_planned_streams
稳态            normal   = N × 2 × segment_len × fb          （raw / processed 各攒满一段，段边界对齐时的峰值）
转码停滞        normal + queue_max_mb                         （sweeper 照常拉段，堆在录像队列）
推理停滞        normal + N × ca_ready_maxlen × fb             （与 ca_raw 不再共享的那部分帧）
sweeper 停滞    N × 2 × ca_maxlen × fb + N × ca_ready_maxlen × fb
最坏 = max(三种停滞) + memory_baseline_mb                      （几种停滞不同时发生：sweeper 活着时 ca 队列持续被拉空，
                                                               sweeper 停了录像队列也就不再进段）
```

| 部件 | 落在哪 |
|------|--------|
| `ca_ready_seconds`、`ca_maxlen_seconds` 改为 20、容量规划三项配置 | [settings.py](../../app/settings.py) |
| `ClientConfig.ca_ready_maxlen` 进 `cq_kwargs`；`ClientQueues(ca_ready_maxlen=)` 独立上限、独立压力水位与容量 | [client/config.py](../../app/services/client/config.py)、[client/queues.py](../../app/services/client/queues.py) |
| 解码背压的容量改为 `ca_ready_maxlen` | [stream/decoder.py](../../app/services/stream/decoder.py) |
| 段队列在途字节记账：入队时计入，写完不论成败都归还，入队失败当场归还 | [recording/service.py](../../app/services/recording/service.py)、[recording/config.py](../../app/services/recording/config.py)、`config/recording_config.yaml` |
| 估算公式（纯函数）与读 `MemAvailable` | [utils/memory_budget.py](../../app/services/utils/memory_budget.py) |
| 启动自检，放在 lifespan 起服务之前 | [main.py](../../app/main.py) `_check_memory_budget` |
| 丢帧原因新增 `recording_budget`（按帧计） | [utils/metrics.py](../../app/services/utils/metrics.py) |

### 方案选型

| 决策点 | 采用 | 否掉的备选 |
|--------|------|-----------|
| ca_ready 上限 | 独立设为 2s：调度器取帧速度远高于输入（约 1000fps 对 225fps），正常时队列总是空的；积压下来的陈帧对实时督促没有用 | 沿用 `ca_maxlen`：推理停滞时会额外占 8–11GB |
| 录像队列限长 | 按在途字节，同时保留段数上限作双保险；超预算沿用现有的「丢段 + 告警」语义 | 只按段数：内存上限是 100 × 285MB |
| 自检失败怎么处理 | 默认拒绝启动；`CLEANSIGHT_MEMORY_CHECK_ENFORCE=false` 时只告警，供资源紧的测试机用 | 拦截 `/api/start` 按路数限流：改契约 |
| malloc 参数 | 不动 | `MALLOC_ARENA_MAX=2`：实测全部释放后仍有 2.74GB 不归还 |

### 配置项

| 配置 | 默认 | env |
|------|------|-----|
| `ca_maxlen_seconds` | 20（原 30） | `CLEANSIGHT_CA_MAXLEN_SECONDS` |
| `ca_ready_seconds` | 2.0 | `CLEANSIGHT_CA_READY_SECONDS` |
| `capacity_planned_streams` | 15 | `CLEANSIGHT_CAPACITY_PLANNED_STREAMS` |
| `memory_baseline_mb` | 6144 | `CLEANSIGHT_MEMORY_BASELINE_MB` |
| `memory_check_enforce` | true | `CLEANSIGHT_MEMORY_CHECK_ENFORCE` |
| `recording_config.yaml: queue_max_mb` | 8192 | — |

> **`ca_maxlen_seconds = 20`**：等于 600 帧，满足「≥ 1.2 倍段长（360 帧）」的校验，raw 轨给录像留 2 段余量；processed 按 15fps 进帧，同样 600 帧可容纳 40s。
> **`queue_max_mb` 必须 ≥ 单段字节（约 285MB）**，否则每一段都会被拒。

### 保留项（不改动）

- **对外契约**：`/api/start` 不新增拒绝条件。
- **队列语义**：ca 队列满时丢最旧帧，以及各类丢帧计数的语义。
- **录像队列只有一个消费线程**：tfdt 依赖执行顺序。

## 变更效果

| 场景（15 路） | 变更前 | 变更后 |
|------|--------|--------|
| 转码停滞 | 8.5GB + 28.5GB ≈ 37GB | 7.7GiB + 8GiB ≈ 15.7GiB |
| 推理停滞 | +8–11GB | +0.4GB |
| sweeper 停滞 | ≈ 37GB | ≈ 15.9GiB |
| 最坏 + 基线 6GiB | — | ≈ 21.8GiB < 32GB 机器的 MemAvailable（约 29GiB），启动自检通过 |

`ca_maxlen_seconds` 保持 30 时，最坏 + 基线约 29.5GiB，会被启动自检拒绝。

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_memory_budget.py`（新增：ca_ready 独立上限、估算公式、读 meminfo、自检的通过 / 拒绝 / 只告警 / 跳过） | 10 passed |
| `tests/test_recording_service.py::TestHlsByteBudget`（新增：超预算拒收、停滞后恢复、入队失败归还、写失败归还） | 4 passed |
| 全量 `pytest tests/` | 1002 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `memory_baseline_mb = 6144` 是估计值 | 估低了，自检会放过真实超限 | 测试环境 15 路压测时记录进程总 RSS，按实测回填 |
| 测试机内存小于 32GB | 默认配置下后端拒绝启动 | 部署时按日志里的分项调小 `CLEANSIGHT_CAPACITY_PLANNED_STREAMS`，或设 `CLEANSIGHT_MEMORY_CHECK_ENFORCE=false` |
| KB 的 [SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md) 仍写 `ca_maxlen_seconds = 30` | 文档滞后 | 走 KB 融合流程时更新 |
