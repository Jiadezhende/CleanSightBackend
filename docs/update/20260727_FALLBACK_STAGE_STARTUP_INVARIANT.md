# 兜底 stage 启动不变式：把「MOCK 恰好有 detector」的巧合提成显式契约

> **变更状态**：生效中（2026-07-27）。新增一条启动 fail-fast + `FALLBACK_STAGE` 常量收口；现网配置本就满足，行为无变化，357 passed（+2）。
> **知识库**：待沉淀 → `kb/SERVICE_INFERENCE.md`（stage 路由与启动校验段落）。
>
> 相关：兜底语义本身见 [20260722_FPS_TIME_VS_FRAME_DECOUPLE.md](20260722_FPS_TIME_VS_FRAME_DECOUPLE.md) 之外的 MOCK 兜底约定——MOCK 是**未知 step 的真兜底**，不是 demo 脚手架，勿删。

## 概述

- **改了什么**：(1) `_get_stage_configs()` 末尾加一条启动不变式——兜底 stage 必须 active（有 detector），否则 fail-fast；(2) `"MOCK"` 字面量在 inference 模块内收口成 `config.FALLBACK_STAGE` 常量。
- **为什么改**：这条不变式此前**没有任何强制**，纯靠现网 YAML 恰好给 MOCK 配了 detector 才幸免，而破坏它的操作既普通又不会被启动发现。
- **影响面**：`app/services/inference/{config,manager}.py` + 一个测试文件。现网配置满足不变式（已实测），无行为变化。

## 缺陷路径（此前无人拦截）

在线 stage 路由有两条硬事实：

1. `InferenceManager.resolve_stage` 把未知/未配 step_id **一律路由到 `"MOCK"`**（硬编码兜底目标），却从不检查 MOCK 是否 active。
2. `DetectionService` 只把**有 detector 的 stage** 放进 `active_stages`（`_get_stage_configs` 过滤），而 dispatcher 只提交 active stage 的帧。

两条之间没有连接。于是只要有人把 `inference_config.yaml` 里 MOCK 的 `MockDetector` 注掉（理由可能只是「MOCK 看着像 demo，清理掉」）：

- 启动**照常成功**，只在 INFO 打一行 `Skipped N stages (no detectors): ['MOCK']`——**不 fail、不 warn**；
- 此后每个未知 step_id 的 run，`cq.stage="MOCK"` 已非 active → 帧被 dispatcher 从 `ca_ready` 掏出、堆进无人 drain 的 stage deque → 攒满 `maxlen` 后静默淘汰；
- 结果是那一路**永久 0 推理且无任何告警**——不是「掉帧降级」，是彻底黑洞。

即：**掉一行 detector 配置就把「未知 step 的兜底」变成「未知 step 的黑洞」，且它躲过了启动检查。**

## 改动

### 1. 启动不变式（`manager._get_stage_configs`）

在确认 `stage_configs` 非空后追加：

```python
if FALLBACK_STAGE not in stage_configs:
    raise ValueError(f"兜底 stage '{FALLBACK_STAGE}' 无 detector（未 active）——…")
```

与既有「无任何有效 stage 就 raise」同姿态（同在 try 内，最终包成 `RuntimeError` 抛出）。错误信息带 `active=` / `skipped=` 两份清单，直接指向该改哪里。

> **为什么是这条而不是在 dispatcher 侧加过滤**：dispatcher 侧「非 active 就不 pop」只是把黑洞换成静默不取帧，症状仍不可见；而有了本不变式后 `cq.stage` 恒 active，那个过滤即成死代码。**在源头把假设变成契约，比在下游兜住症状更本质。**

### 2. `FALLBACK_STAGE` 常量收口（`config.py`）

`"MOCK"` 此前在 inference 模块内硬编码两处（`InferenceManager.resolve_stage` / `InferenceConfig.resolve_stage`），提成 `config.FALLBACK_STAGE` 单一真源。

两个 `resolve_stage` **仍然不合并**——它们故意不同方法：在线查 active 集合（有 detector），离线查 YAML 全集，查的集合不同是设计而非重复。但**兜底目标必须同源**，否则改一处会静默分叉。

`ClientQueues(stage="MOCK")` 的构造默认**不动**：那在 client 模块，跨服务直接依赖 inference 会违反解耦约定；它是裸建兜底，生产路径由 `RunController` 经 `resolve_stage` 定死。

### 3. 测试（`tests/test_inference_stage_routing.py`，+2）

在 config/factory 这层 seam 上注入假 stage 集合，不碰真权重加载（I/O 边界集成-only）：

| 用例 | 断言 |
|---|---|
| `test_fallback_stage_without_detector_fails_fast` | 兜底 stage 无 detector → `RuntimeError`（消息含 stage 名） |
| `test_fallback_stage_with_detector_passes` | 正常放行，且兜底目标落在 active 集合内、`resolve_stage` 未配 step 确实指向它 |

现网 `inference_config.yaml` 已实测满足（stages=`['1','2','MOCK']`，MOCK 有 1 个 detector）。
