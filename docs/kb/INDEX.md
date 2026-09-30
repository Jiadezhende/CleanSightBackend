> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# CleanSight 代码知识库索引

本目录是 CleanSight Backend 的可信知识库。内容优先来自当前代码、配置和测试；`docs/` 旧文档仅作为线索，不能替代代码事实。

## 推荐阅读路径

新人快速理解：

1. [BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md)
2. [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)
3. [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md)

业务/算法协作：

1. [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)
2. [DESIGN_DETECTION_WORKFLOW.md](DESIGN_DETECTION_WORKFLOW.md)
3. [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)
4. [DESIGN_EXTENDING_DETECTION.md](DESIGN_EXTENDING_DETECTION.md)

后端开发：

1. [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)
2. [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)（动包结构、加模块、改 import 前必读）
3. [SERVICE_STREAM.md](SERVICE_STREAM.md)
4. [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)
5. [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)
6. [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)
7. [SERVICE_RECORDING.md](SERVICE_RECORDING.md)
8. [SERVICE_ALARM.md](SERVICE_ALARM.md)
9. [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)
10. [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)（动 `app/storage/` 或落盘产物前必读）
11. [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md)（动队列、清理、迟到结果处理前必读）

运维排障：

1. [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)
2. [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)
3. [SERVICE_CONFIG.md](SERVICE_CONFIG.md)
4. [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)
5. [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)

追溯/送标：

1. [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md)
2. [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)
3. [SERVICE_LAB.md](SERVICE_LAB.md)
4. [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)
5. [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md)（动 lab 导出 / 裁剪 / 离线解帧前必读）

试纸比色：

1. [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)

## 维护规则

- [KB_MAINTENANCE.md](KB_MAINTENANCE.md)：可信来源顺序、文件分类与 `DESIGN_` 准入、元信息与更新时间规则。

## 业务场景

- [BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md)：业务对象（任务 / 步骤 / run / 告警 / 证据 / 点位）、对外能力清单与业务边界。
- [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)：LEAK / CLEAN 现行检测标准、阈值与可调位置、未配置 step 处理、告警去重、试纸判据。
- [BUSINESS_TASK_LIFECYCLE.md](BUSINESS_TASK_LIFECYCLE.md)：run 起停流程：启动校验、幂等与换代隔离、四条结束路径（含进程停机）。
- [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md)：按 run 回放与告警定位、两套坐标的使用规则、Lab 送标业务约束。

## 整体架构

- [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)：主进程组件与外部依赖一览；lifespan 嵌套顺序与停机拆 run 流程。
- [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md)：RTSP → 推理 → 可视化 → 落盘主链路；在线 / 离线经盘上产物衔接。
- [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)：router 归属与下层依赖、`routers/utils`、注册与中间件顺序（端点契约见 `docs/api/`）。
- [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)：types / ORM 分层、只读平台库、run 目录布局与可见判据、各产物写读方、`query_*` 清单、TTL 回收。
- [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)：分层依赖图与门禁映射、重依赖位置、服务包骨架、单例引用面。**动包结构前必读**。

## 逐服务说明

- [SERVICE_STREAM.md](SERVICE_STREAM.md)：StreamService、FFmpegDecoder 起停、准入背压、RTSP 读超时判死、URL 重写。
- [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)：COW 注册表与 per-task 锁、CQ 不可变身份与写门状态机、抽帧、容量校验。
- [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)：`start_run` / `stop_run` 编排顺序、身份 fence、失败回滚、停机时逐个 `stop_run`。
- [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)：在线 L1 子进程管线与写回、Actor 与告警过闸、离线 Runner / CLI / 作业服务与 CLEAN 策略。
- [SERVICE_RECORDING.md](SERVICE_RECORDING.md)：sweeper 拉取、HLS 与检测结果两条单消费队列、断流残帧 flush、失败不重试。生产 HLS 与 `detections.jsonl` 的写侧。
- [SERVICE_ALARM.md](SERVICE_ALARM.md)：告警纯入队与单条上报重试；⚠ 上报失败现因 `TypeError` 实际不重试。
- [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)：进程死活判断、重连成功 / 放弃判据、读超时预算、拆除委托 `stop_run`。
- [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)：回放 / 时间轴端点、读侧 run 锁定、MediaToken 与 `/media` 防穿越、VOD 清单生成。
- [SERVICE_LAB.md](SERVICE_LAB.md)：Lab router / service 分工、ClipBuilder 裁剪送标、StepExporter 整段导出、LS 客户端与运行时配置。
- [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)：后端 Gateway 三档路径及判据、RTSP 网关代理、MediaMTX 仅开本机 RTSP。
- [SERVICE_CONFIG.md](SERVICE_CONFIG.md)：fps 三层模型、各 yaml 与 offline 段、env 加载与必填项、Gateway / Lab / 日志配置。
- [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)：试纸比色无状态服务：调用链、零 `app.*` 依赖、`params.yaml`、判定语义与验收工装。

## 设计原则与最佳实践

可迁移的原则 / 判据 / 反例，再遇到同类问题时参考（准入见 KB_MAINTENANCE.md「文件分类」）。

- [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)：生命周期锁、分锁原则、单消费队列 + 目录隔离、队列解耦、关停原则。
- [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md)：迟到写与换代冲突：闭合判据、三种工具、令牌写前比对 vs 多版本、执行与提交分离、按场景选手段。**动队列、清理、落盘前必读**。
- [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)：`app/storage/` 准入判据：四问、分域与路径隔离、动词前缀、L / R / W / D / T 条文、零锁、门禁现状。判据不是现状，盘上现状见 ARCHITECTURE_STORAGE_AND_SCHEMA。
- [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)：四个异常边界层、重试 / 不重试判据、兜底只兜推理失败、停机交出原则。
- [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)：`[PRESSURE]` / `[VIZ_THROUGHPUT]` / `[BACKPRESSURE]` 语义、判定规则、日志量上界。
- [DESIGN_DETECTION_WORKFLOW.md](DESIGN_DETECTION_WORKFLOW.md)：检测链路总览与设计判据：流源 / 流算子划分、游标、告警模式、在线还是离线。
- [DESIGN_EXTENDING_DETECTION.md](DESIGN_EXTENDING_DETECTION.md)：新增检测点与离线策略的静默出错约束和测试清单；代码骨架见 `/infer-workflow` skill。
- [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)：墙钟 / 媒体两轴、EXTINF / tfdt / timescale 约束、MediaTimeline 换算与断流判据、`.idx` 帧反查。
- [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md)：fMP4 分段拼接选路（两条正交轴，禁 `-f concat`）；三种 exit 0 静默失败与按时长验收。
- [TESTING_MAP.md](TESTING_MAP.md)：`tests/` 各文件覆盖面、测试基建约定、导入门禁、集成脚本、补测缺口。
