# CleanSightBackend — 开发者快速指南

AI 视觉巡检后端系统，基于 FastAPI + YOLOv8，提供实时 RTSP 流推理、HLS 录制与告警上报。

---

## 快速启动

```bash
./start_backend.sh dev          # Linux（加载 .env.dev）
.\start_backend.ps1 dev         # Windows
python -m app.main              # 直接运行
docker-compose up               # Docker
```

入口：[app/main.py](app/main.py) → FastAPI lifespan 依次启动各 Service 单例。

---

## 目录结构速览

```
app/
├── main.py            # FastAPI 入口，lifespan 管理服务启停
├── settings.py        # 全局配置（Pydantic Settings，读 .env）
├── database.py        # SQLAlchemy 连接池（PostgreSQL）
├── models/            # Pydantic API 模型 + SQLAlchemy ORM 模型
├── routers/           # HTTP/WS 路由层
├── services/          # 核心业务服务（见下方详细说明）
└── utils/             # 框架工具（异常、重试、指标、上下文）
config/                # YAML 配置（各 Service 参数）
docs/                  # 架构文档
tests/                 # 单元 & 组件测试
integration_tests/     # 端到端集成测试
```

---

## 核心模块

### `app/routers/` — 路由层

| 文件 | 路由前缀 | 职责 |
|------|---------|------|
| `api.py` | `/api` | 统一入口：`POST /api/start`、`POST /api/terminate` |
| `ai.py` | `/ai` | 推理服务：任务加载、WebSocket 推流 `/ai/video` |
| `inspection.py` | `/inspection` | 流控制：启停 RTSP 流（旧接口，逐步废弃） |
| `task.py` | `/task` | 任务历史：溯源记录、告警查询 |
| `health.py` | `/health` | 健康检查：系统状态、Prometheus 指标 |

### `app/services/` — 三大核心服务

**1. `stream/` — 视频流服务**
- 职责：FFmpeg 解码 RTSP/RTMP，向推理队列分发帧
- 关键类：`FFmpegDecoder`（子进程包装）、`StreamHealthMonitor`（5s 心跳、最多重试 5 次）
- 背压策略：队列 > 90% 时主动丢帧

**2. `inference/` — 推理引擎**
- 职责：多阶段 AI 推理 → 时序分析 → 可视化标注
- 关键类：`InferenceManager`（总调度）、`StageAwareDispatcher`（轮询分帧）、`MultiModelWorkerPool`（线程池）
- 扩展点：`workflows/` 下新建 `InferenceWorkflow` 子类即可接入新检测任务（见 `/infer-workflow` skill）

**3. `persistence/` — 持久化服务**
- 职责：HLS 视频分段写入 + 告警数据上报
- 关键类：`PersistenceManager`、HLS Writer workers、Alarm Reporter

### `app/services/client/` — 客户端状态管理
- `ClientManager`：全局单例，管理所有在线客户端
- `ClientState`：每客户端状态（task_id、时序历史、告警列表）
- `ClientQueues`：4 条异步队列（`ca_ready` / `ca_raw` / `ca_processed` / `rt_processed`）

### `app/utils/` — 基础设施

| 文件 | 职责 |
|------|------|
| `exceptions.py` | 6 种自定义异常（retryable/fatal 标记） |
| `executor.py` | `GuardedExecutor` — 统一重试框架（边界层 2） |
| `decorators.py` | 日志装饰器 |
| `metrics.py` | Prometheus 指标导出 |
| `context.py` | 线程本地上下文（client_id、task_id） |

---

## 数据流

```
RTSP (30fps)
  ↓ [FFmpegDecoder]
ca_ready (推理)  ca_raw (完整录制)
  ↓ [AI推理 → 时序分析 → 可视化]
ca_processed → [HLS分段 + 告警上报]
rt_processed → [WebSocket 实时推送]
```

---

## 配置文件

| 文件 | 作用 |
|------|------|
| `config/inference_config.yaml` | 推理阶段、模型路径、置信度阈值、批大小 |
| `config/stream_config.yaml` | FFmpeg 参数、背压比例、重连策略 |
| `config/persistence_config.yaml` | HLS 分段时长、告警批次、存储路径 |
| `config/client_config.yaml` | 队列大小、时序历史窗口 |
| `config/health_monitor_config.yaml` | CPU/内存/GPU 阈值、检查间隔 |

---

## 异常处理分层（4 个边界层）

| 层 | 位置 | 策略 |
|----|------|------|
| L1 | Worker.run() | 捕获线程崩溃，记录并重启 |
| L2 | GuardedExecutor | 自动重试（retryable）/ 快速失败（fatal） |
| L3 | FastAPI exception handlers | 转换为 HTTP 状态码 |
| L4 | main() | 顶层 fail-fast，记录并退出 |

详见 [docs/BOUNDARY_LAYER_DESIGN.md](docs/BOUNDARY_LAYER_DESIGN.md)

---

## 数据库

- PostgreSQL，连接池：5 基础 / 15 最大
- 主表：`clean_task`（任务记录）、`clean_alarm`（告警记录）
- ORM 模型：[app/models/task.py](app/models/task.py)

---

## 常用开发操作

```bash
# 运行单元测试
pytest tests/

# 运行集成测试（需真实 RTSP 流）
python integration_tests/local_full_pipeline_rtsp.py

# 查看 API 文档
http://localhost:8000/docs
```

**新建检测 Workflow**：使用 `/infer-workflow` skill，自动按规范生成代码框架。

### Workflow 实现注意事项

- `class_name` 直接取自模型 `result.names`，不做归一化，匹配字符串必须与模型训练类别名严格一致
- `infer_batch` 覆写时，batch 路径与 fallback 单帧路径的业务字段赋值逻辑必须保持一致

**查看数据库 Schema**：使用 `/schema-inspect` skill，自动对比 ORM 与实际表结构。
