> 更新时间：2026-07-06
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
2. [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)
3. [DESIGN_EXTENDING_DETECTION.md](DESIGN_EXTENDING_DETECTION.md)

后端开发：

1. [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)
2. [SERVICE_STREAM.md](SERVICE_STREAM.md)
3. [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)
4. [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)
5. [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)
6. [SERVICE_PERSISTENCE.md](SERVICE_PERSISTENCE.md)
7. [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)

运维排障：

1. [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)
2. [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)
3. [SERVICE_CONFIG.md](SERVICE_CONFIG.md)
4. [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)

追溯/送标：

1. [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md)
2. [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)
3. [SERVICE_LAB.md](SERVICE_LAB.md)
4. [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)

## 维护规则

- [KB_MAINTENANCE.md](KB_MAINTENANCE.md)：规定知识库可信来源、更新时间、索引维护和旧文档核验规则。

## 业务场景

- [BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md)：解释 CleanSight 的业务目标、任务/步骤/告警/证据等核心概念。
- [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)：记录当前代码实际执行的 LEAK/CLEAN/MOCK 检测标准与告警规则。
- [BUSINESS_TASK_LIFECYCLE.md](BUSINESS_TASK_LIFECYCLE.md)：说明任务从启动、幂等判断、切换到终止清理的完整生命周期。
- [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md)：说明告警证据回溯、任务回放、时间轴和 Label Studio 送标业务流程。

## 整体架构

- [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)：概览 FastAPI 主进程、MediaMTX、FFmpeg、Postgres 和外部系统的组件关系。
- [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md)：追踪视频流从 RTSP 输入到推理、可视化、HLS、告警的端到端数据流。
- [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)：API 路由接线图——router 归属、注册与中间件顺序、生命周期挂载（端点请求/响应契约属对外 API 文档，不在本库）。
- [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)：说明数据模型分层（domain/ORM/DTO）、`clean_task`、`clean_alarm`、HLS 文件目录、playlist、features.jsonl 和 metadata。

## 逐服务说明

- [SERVICE_STREAM.md](SERVICE_STREAM.md)：说明 StreamService、FFmpegDecoder、URL 重写、背压和断流检测输入。
- [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)：说明 ClientManager（COW 注册表，int task_id 键）、ClientQueues（per-run 不可变身份 + ACTIVE/DRAINING/CLOSED 状态机）、队列、前端消息和告警 gate。
- [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)：说明 RunController 跨服务起停编排、per-task 锁、拆机顺序与对象身份 fence。
- [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)：说明 InferenceManager、StageFactory、Dispatcher、模型推理、时序分析和可视化三池设计。
- [SERVICE_PERSISTENCE.md](SERVICE_PERSISTENCE.md)：说明 HLS/告警持久化队列、worker、慢 IO 分离、fMP4 转码和告警上报。
- [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)：说明全局健康监控、断流重连、孤儿状态检测和统一 cleanup_client。
- [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)：说明 SegmentFinder、MediaToken、追溯接口、媒体 token 鉴权和 VOD playlist。
- [SERVICE_LAB.md](SERVICE_LAB.md)：说明 Lab 裁剪 raw 视频、Label Studio 上传、配置和失败隔离策略。
- [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)：说明 FastAPI Gateway、独立 MediaMTX Gateway、IP 白名单、限流和 RTSP TCP 代理。
- [SERVICE_CONFIG.md](SERVICE_CONFIG.md)：说明环境变量、YAML 配置、Gateway、Lab 和各服务之间的配置耦合点。

## 关键工程设计

- [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md)：线程安全性、异步解耦、防卡死与可维护性。
- [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)：说明异常边界层、GuardedExecutor、重试、健康监控和优雅关闭策略。
- [DESIGN_EXTENDING_DETECTION.md](DESIGN_EXTENDING_DETECTION.md)：说明如何新增 Detector（流源）、Operator（流算子，analyze+judge 合并）、YAML stage 配置和相关测试。
- [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)：说明 fMP4、EXTINF 真值、tfdt、在途段过滤和时间轴计算的关键约束。
- [TESTING_MAP.md](TESTING_MAP.md)：索引现有测试覆盖面，并给后续改动提供优先补测方向。
