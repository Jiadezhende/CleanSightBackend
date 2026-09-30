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

试纸比色：

1. [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)

## 维护规则

- [KB_MAINTENANCE.md](KB_MAINTENANCE.md)：规定知识库可信来源、更新时间、索引维护和旧文档核验规则。

## 业务场景

- [BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md)：解释 CleanSight 的业务目标、任务/步骤/告警/证据等核心概念。
- [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)：记录当前代码实际执行的 LEAK/CLEAN 检测标准、告警规则与未配置 step 的处理（另含试纸比色判据）。
- [BUSINESS_TASK_LIFECYCLE.md](BUSINESS_TASK_LIFECYCLE.md)：说明任务从启动、幂等判断、切换到终止清理的完整生命周期。
- [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md)：说明告警证据回溯、任务回放、时间轴和 Label Studio 送标业务流程。

## 整体架构

- [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)：概览 FastAPI 主进程、MediaMTX、FFmpeg、Postgres 和外部系统的组件关系。
- [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md)：追踪视频流从 RTSP 输入到推理、可视化、HLS、告警的端到端数据流。
- [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)：API 路由接线图——router 归属、注册与中间件顺序、生命周期挂载（端点请求/响应契约属对外 API 文档，不在本库）。
- [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)：说明数据模型分层（`app/types` / `app/db` ORM / DTO）、只读平台库（`clean_task`、`clean_alarm` 与 `query_*`）、按 run 分目录的落盘布局（`{task}/{step}/{run_id}/hls|inference/`）、run 分配与可见判据、hls / inference 产物的写者与读者、读侧 `query_*` 清单、TTL 回收与 `.trash` 回收区。
- [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)：`app/` 分层依赖图与导入纪律——routers / daemons / services / services/utils / storage·db / types，四个白名单包、routers 向下依赖 db / storage、L0-L3 重依赖分级与 L2 的三条合法通路、服务包骨架（`service.py` / `instance.py` / `*_worker.py`）、`__init__` 三形态、单例构造与引用面、导入写法、门禁测试映射。**动包结构或新增 `app/storage/` 域文件前必读**。

## 逐服务说明

- [SERVICE_STREAM.md](SERVICE_STREAM.md)：说明 StreamService、FFmpegDecoder、URL 重写、背压和断流检测输入。
- [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)：说明 ClientService（COW 注册表，int task_id 键）、ClientQueues（`cq.run` 不可变身份 + ACTIVE/DRAINING/CLOSED 状态机）、队列与落盘缓冲、前端消息和告警 gate。
- [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)：说明 RunControlService 跨服务起停编排、锁外 step 校验、锁内 `runs.allocate`、per-task 锁、拆机顺序与对象身份 fence。
- [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)：说明 inference 包 online / offline / 共享层结构、InferenceService、检测与时序契约、零 IO 写回口、Detector / Operator 框架、按 ts 重采样、时序 Actor，以及离线段（Runner、CLI、作业服务、CLEAN 离线策略）。
- [SERVICE_RECORDING.md](SERVICE_RECORDING.md)：说明录制落盘编排——sweeper 节拍器、HLS 与检测结果两条单消费队列、只写自己的 run 目录、零锁并发模型、断流残帧 flush、失败不重试的理由。**生产 HLS 与 `detections.jsonl` 的写侧就是这里。**
- [SERVICE_ALARM.md](SERVICE_ALARM.md)：说明告警服务——纯入队、AlarmWorker 单条上报与退避重试、AlarmReporter 成功判据（TTL 回收见 ARCHITECTURE_STORAGE_AND_SCHEMA.md）。
- [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)：说明全局健康监控（`app/daemons/health_monitor/`）、断流重连、孤儿状态检测和统一 cleanup_client。
- [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)：说明任务回放与时间轴接口、读侧 run 锁定（`app/routers/utils/runs.py`）、MediaToken 鉴权、VOD playlist 渲染，以及 `hls.query_span` / `query_timeline` 等取数路径。
- [SERVICE_LAB.md](SERVICE_LAB.md)：说明 Lab router 与 `services/lab/service.py` 的分工、裁剪 raw 视频送标、整段导出下载、逐帧类别概率读口、Label Studio 上传、配置和失败隔离策略。
- [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)：说明 FastAPI Gateway、独立 MediaMTX Gateway、IP 白名单、限流和 RTSP TCP 代理。
- [SERVICE_CONFIG.md](SERVICE_CONFIG.md)：说明环境变量、YAML 配置（含 stage 的 offline 块）、Gateway、Lab 和各服务之间的配置耦合点。
- [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)：说明试纸比色（colorstrip）无状态算法服务——调用链、包结构与零 `app.*` 依赖约束、`params.yaml` 配置、判定语义、Gateway 分档与验收工装。

## 设计原则与最佳实践

开发中总结出的设计原则 / 最佳实践，再遇到同类问题时参考（准入见 KB_MAINTENANCE.md「文件分类」）。

- [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)：线程安全性、异步解耦、防卡死与可维护性。
- [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md)：迟到写入与换代 / 回收冲突。「前提检查 + 写」之间不穿插同资源的其他写才算闭合，三种工具：单消费线程队列、锁（含锁内 CAS）、资源的原子操作原语（跨进程时用，对方的写也须原子，删除因此要改成 rename 到回收区再删、失败按「没删」处理）；代次令牌两种用法——写前比对 vs 多版本（MVCC：一代一个版本目录、版本号单调不复用、tmp→rename 原子提交、只有属主建版本、读者解析一次锁定句柄、SEALED 封口、只回收非最新版本、一文件一写者）；耗时任务执行与提交分离；每一代新建对象优于复用；门禁与点外自查最多缩窗；按场景选手段（写错可重跑、无下游副作用的可先不防）。动队列、清理、落盘或迟到结果处理前先读。
- [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)：`[PRESSURE]`/`[VIZ_THROUGHPUT]`/`[BACKPRESSURE]` 三条正交诊断日志——压力周期快照、reason 触发侧语义、拒收可见化、日志量上界。
- [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)：说明异常边界层、兜底只兜运行时推理失败、告警上报重试、健康监控和优雅关闭策略。
- [DESIGN_DETECTION_WORKFLOW.md](DESIGN_DETECTION_WORKFLOW.md)：检测链路架构总览图（整体流程、流源/流算子角色分工、两种告警模式、各检测点详细流程），配合 DESIGN_EXTENDING_DETECTION 使用。
- [DESIGN_EXTENDING_DETECTION.md](DESIGN_EXTENDING_DETECTION.md)：说明如何新增 Detector（流源）、Operator（流算子，analyze+judge 合并）、YAML stage 配置和相关测试。
- [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)：说明 fMP4、EXTINF 真值、timescale pin=90000、tfdt、在途段过滤、时间轴计算，以及逐帧 ts sidecar（`.idx`）与离线帧反查的关键约束。
- [DESIGN_SEGMENT_CONCAT.md](DESIGN_SEGMENT_CONCAT.md)：**选型参考**——把分段视频拼成一条 ffmpeg 能吃的流，由**两条正交的轴**决定（段能否独立 demux → `-f concat`；段能否字节拼接 → `concat:` 协议）。按轴给出落盘格式分类（段自包含 / fragment + 共用 init）与消费端矩阵，再落到「四条真候选 + 四条被排除的写法」的实测对照（`-f concat` 全家被轴 1 结构性判死，不在候选之列）。**另有与轴正交的一节**：三条**静默失败**全部能骗过 `returncode != 0` + `size > 0` 型判据——`-f concat` 清单含 init → exit 0 产零流空壳；LIVE 清单缺 `ENDLIST` → 无限挂死；**路径 ① 遇坏段 → exit 0、全日志级别无输出、`-xerror` 无效，产出合法但截短的 mp4（选对路径照样会中）**。另有 Windows 路径分隔符分歧；fMP4 段的时间轴只听 `tfdt`，清单 EXTINF 覆盖不了段时长。动 lab 导出 / 裁剪 / 离线解帧的取数方式前先读。
- [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)：数据层 `app/storage/` 的**准入判据**——什么进层什么不进（四问）、按域拆包与四道路径隔离机制、成员动词前缀封闭集合（含 `query_` / `allocate`，对外不出删除成员）、域容器 `types.py`、定位集中（L）、读写条文（R/W 三路线）、零锁、依赖与测试、门禁映射。**它是判据不是现状**，盘上现状见 ARCHITECTURE_STORAGE_AND_SCHEMA.md。
- [DESIGN_SEGMENT_CONCAT_VERIFY.md](DESIGN_SEGMENT_CONCAT_VERIFY.md)：**验收判据**——与上一篇的「怎么选路」正交。四个失败点里**三个会骗过 `returncode != 0` + `size > 0`**（`-f concat` 清单含 init 产零流空壳、LIVE 清单缺 `ENDLIST` 无限挂死、坏段产出合法但截短的 mp4），而 `step_exporter` / `clip_builder` 两处现役调用点用的正是那对判据。动 lab 导出/裁剪的成功判定前先读。
- [TESTING_MAP.md](TESTING_MAP.md)：索引现有测试覆盖面，并给后续改动提供优先补测方向。
