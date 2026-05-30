# CleanSight Backend 知识库总结

> 更新时间：2026-05-24
> 依据来源：代码分析和文档整理
> 可信级别：以当前仓库代码、配置、测试为准

## 概览

CleanSight Backend 是一个用于内镜人工清洗流程的 **实时视频 AI 视觉巡检后端**。系统通过接入 RTSP/RTMP 视频流，进行 AI 推理检测、告警生成、视频追溯和 Label Studio 送标。

---

## 核心架构

### 整体架构

CleanSight 采用 **FastAPI 主进程 + 外部组件** 的架构模式：

**主进程组件（app/ 目录）**：
- FastAPI 应用入口：`app/main.py`
- 流服务：`app/services/stream/` - 视频流接收与解码
- 客户端状态：`app/services/client/` - 客户端队列与状态管理
- 推理服务：`app/services/inference/` - AI 推理核心模块
- 持久化服务：`app/services/persistence/` - HLS 存储与告警上报
- 健康监控：`app/services/health_monitor/` - 全局健康监控
- 追溯与媒体：`app/services/traceback/`、`app/routers/traceback.py`、`app/routers/media.py`
- Lab 送标：`app/services/lab/`、`app/routers/lab.py`

**外部组件**：
- **MediaMTX**：接收或转发 RTSP/RTMP 流（暴露 1935、8004 等端口）
- **FFmpeg**：通过子进程读取流并解码 rawvideo
- **Postgres**：应用数据库，存储 `clean_task` 和 `clean_alarm` 表
- **外部告警接口**：`settings.alarm_report_url`
- **Label Studio**：Lab 模块通过 HTTP API 上传裁剪视频

### 端到端数据流

```
RTSP/RTMP
  -> StreamService
  -> FFmpegDecoder
  -> ClientQueues.ca_raw
  -> ClientQueues.ca_ready
  -> StageAwareDispatcher
  -> ModelWorkerService / MultiModelWorkerPool
  -> ClientQueues.slide_window + latest_inference
  -> ClientTemporalActor
  -> PersistenceManager.alarm_queue
  -> VisualizationWorker
  -> ClientQueues.ca_processed + latest_rendered
  -> HLSWorker / WebSocket
```

**关键流程**：
1. **输入与解码**：`StreamService` 为每个 `client_id` 创建 `FFmpegDecoder`，输出原始帧写入 `ClientQueues`
2. **推理与时序**：`StageAwareDispatcher` 轮询待推理帧，按 stage 分组推理，结果双写滑动窗口（时序分析）和原子快照（可视化）
3. **可视化与前端**：约 15 FPS 渲染最新推理结果和原始帧，通过 WebSocket 推送
4. **落盘与告警**：HLS 队列写视频段，Alarm 队列上报外部接口

### 存储架构

**数据库连接**：
- SQLAlchemy QueuePool，`pool_pre_ping=True`
- 常驻连接 5，最大溢出 10，连接回收 3600 秒

**核心数据表**：
- `clean_task`：任务表，包含 task_id、source_ip、current_step、status 等
- `clean_alarm`：告警表，包含 alarm_id、task_id、step_id、alarm_type、severity 等

**HLS 文件目录**：
```
{base_dir}/{task_id}/{step_id}/
  init.mp4
  raw_segment_{ts_us}.mp4
  processed_segment_{ts_us}.mp4
  raw_playlist.m3u8
  processed_playlist.m3u8
  keypoints_{ts_us}.json
  metadata.json
```

---

## 业务能力

### 当前已实现功能

| 功能 | 接口 | 状态 |
|------|------|------|
| 启动任务并拉流 | `POST /api/start` | ✅ |
| 实时推理视频 | `WebSocket /ai/video?client_id=...` | ✅ |
| 实时前端消息 | `GET /task/message/{task_id}` | ✅ |
| 历史告警查询 | `GET /task/{task_id}/alarms` | ✅ |
| 告警证据回溯 | `GET /traceback/alarm/{alarm_id}/evidence` | ✅ |
| 单步骤 VOD 回放 | `GET /traceback/task/{task_id}/playlist.m3u8?step_id=...` | ✅ |
| Lab 送标 | `POST /lab-f3m8/submit` | ✅ |

### 检测阶段现状

| 阶段 | 检测内容 | 状态 |
|------|----------|------|
| **LEAK** | 气泡检测 + 弯折动作检测 | ✅ 核心阶段 |
| **CLEAN** | Mock detector（纯透传） | ⚠️ 待开发 |
| **MOCK** | 兜底策略 | ✅ |

### 任务生命周期

**启动流程**：
1. API 层查询数据库 `clean_task`
2. 校验任务存在，获取 `source_ip` 作为 `client_id`
3. 获取 per-client 异步锁，串行化同一 client 的 start/terminate
4. 幂等判断：task、step、URL 完全相同则返回，否则清理旧任务
5. 启动解码和推理服务

**终止流程**：
1. 停止 StreamService 中的 decoder
2. 调用 InferenceManager 移除推理资源、触发结算告警、落盘残余 HLS 段
3. 从 ClientManager 移除客户端状态

**任务切换**：同一 client 任务变更时，先完整清理旧任务再启动新任务，确保 TemporalActor 结算告警归属正确。

---

## 测试覆盖

### 已覆盖领域

| 测试文件 | 测试内容 |
|----------|----------|
| `test_api_concurrency.py` | 并发 start、任务切换、terminate 锁 |
| `test_task_message_api.py` | 实时消息接口 |
| `test_alarm_increment.py` | 告警 gate、seq、自增和重置 |
| `test_inference_stage_routing.py` | current_step 到 stage 路由 |
| `test_temporal_debounce.py` | 时序去抖逻辑 |
| `test_boundary_layers.py` | 边界层行为 |
| `test_exception_handling.py` | 异常分类和处理 |
| `test_stream_rewrite.py` | RTSP URL 端口改写 |
| `test_reconnect_on_initial_failure.py` | 初始拉流失败后重连 |
| `test_gateway.py` | ASGI Gateway 行为 |
| `test_mediamtx_gateway.py` | MediaMTX Gateway 行为 |
| `test_traceback_router.py` | 追溯路由 |
| `test_traceback_segment_finder.py` | 段定位 |
| `test_traceback_media_token.py` | 媒体 token |
| `test_lab_clip_builder.py` | Lab 裁剪构建 |

### 待补测方向

- 新增检测任务的 Detector/Analyzer 单元测试
- HLS 写入逻辑的 playlist EXTINF、在途段过滤、timeline 测试
- 清理流程的结算告警归属和残余段 flush 测试
- Gateway 配置的 relaxed/bypass/normal 三类路径测试
- Lab 上传的单段失败不影响整请求的响应结构测试

---

## 文档索引

**快速入门路径**：
1. [BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md) - 业务总览
2. [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md) - 整体架构
3. [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md) - 数据流

**后端开发**：
1. [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md) - API 接口索引
2. [SERVICE_STREAM.md](SERVICE_STREAM.md) - 流服务详解
3. [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md) - 推理服务详解
4. [SERVICE_PERSISTENCE.md](SERVICE_PERSISTENCE.md) - 持久化服务详解
5. [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md) - 并发与队列设计

**运维排障**：
1. [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md) - 健康监控
2. [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md) - MediaMTX Gateway
3. [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md) - 容错设计

**追溯与送标**：
1. [BUSINESS_TRACEBACK_AND_LAB.md](BUSINESS_TRACEBACK_AND_LAB.md) - 追溯与送标业务流程
2. [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md) - 追溯媒体服务
3. [SERVICE_LAB.md](SERVICE_LAB.md) - Lab 送标服务
4. [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md) - HLS 时间轴设计

---

## 关键约束

1. **业务主键**：任务以 `task_id` 为主键，`source_ip` 仅作为运行时 `client_id` 来源
2. **追溯定位**：按 `task_id + step_id` 定位，不再依赖 `source_ip`
3. **Lab 送标**：只使用 raw 轨，不使用 processed 轨
4. **幂等策略**：同一 client 已运行时，只有 task、step、URL 完全相同才幂等返回
5. **清理顺序**：尽力而为策略，单步失败记录错误但继续后续步骤

---

## 相关文件

- 应用入口：`app/main.py`
- 统一 API：`app/routers/api.py`
- 推理服务：`app/services/inference/`
- 流服务：`app/services/stream/`
- 客户端状态：`app/services/client/`
- 持久化：`app/services/persistence/`
- 健康监控：`app/services/health_monitor/`
- 追溯与媒体：`app/routers/traceback.py`、`app/routers/media.py`、`app/services/traceback/`
- Lab：`app/routers/lab.py`、`app/services/lab/`
- Gateway：`app/utils/gateway.py`、`mediamtx_gateway/`
