# CleanSightBackend — 开发者导航

AI 视觉巡检后端系统，基于 FastAPI + YOLOv8：实时 RTSP 流推理、HLS 录制与告警上报。
入口 [app/main.py](app/main.py) → FastAPI lifespan 依次启动各 Service 单例。

本文件只做**导航与硬约束**：分诊到正确的文档、定位代码、列出容易默认做错的红线。描述性内容一律去下列文档，不在此重复。

---

## 文档分诊（先读文档，别猜）

| 我要… | 去哪 |
|------|------|
| 懂架构 / 数据流 / 各服务内部 / schema / API（描述性） | 先读知识库 [docs/kb/INDEX.md](docs/kb/INDEX.md)，**再扫 [docs/update/](docs/update/) 里晚于 KB 更新时间的增量**（KB 定期融合、可能滞后，见下方注） |
| 开发规范：分支提交、测试、模块内聚与解耦 | [DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| 部署：Linux 生产 + Windows 开发安装、物料分发 | [DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| 定位代码：目录结构 → [README.md](README.md) 项目结构；某个服务内部 → KB 对应 `SERVICE_*.md` | — |

> 描述性文档改动同步进 `docs/kb/`；每次提交的增量先写 `docs/update/`，定期融合进 KB（细则见 [docs/kb/KB_MAINTENANCE.md](docs/kb/KB_MAINTENANCE.md) 与 [DEVELOPMENT.md](docs/DEVELOPMENT.md)）。
> **因此 KB 不是最新的：** INDEX 顶部有「更新时间」，`docs/update/` 里文件名日期晚于它的都是尚未融合的增量——查某主题最新状态时，KB 结论要用 update 增量校正后再采信。

---

## Skill 路由（不可发现，必须显式用）

| 场景 | Skill |
|------|-------|
| 新建检测任务 / Detector / Analyzer / Judge | `/infer-workflow` |
| 查表结构、对比 ORM 与实际表 | `/schema-inspect` |
| 本地/远程 Linux GPU 主机部署 | `/deploy-linux` |

---

## 快速启动

```bash
./start_backend.sh dev          # Linux（加载 .env.dev）
.\start_backend.ps1 dev         # Windows
python -m app.main              # 直接运行

pytest tests/                                        # 单元 & 组件测试
python integration_tests/test_single_client.py --scenario 1 --task_id 1  # 集成测试（需真实 RTSP 流）
python -m mediamtx_gateway.main                       # RTSP TCP 代理网关（可选，对外部署）
```

---

## 硬规矩（不遵守会默认做错）

> 以下是最容易默认踩坑的红线；开发规范的完整正文与其余约定以 [DEVELOPMENT.md](docs/DEVELOPMENT.md) 为准。

- **部署默认 dev，别擅自当 prod**；跑会写库/发告警的端到端测试前先确认（会写真实 DB / 发真实告警，部署细节见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)）。
- **跑任何 python/pytest 前先激活项目 `.venv`**，别用裸 `python3`。
- **写测试走 `tests/factories.py` 单一真源**构造 CQ/Detection 等，别在用例里另起炉灶或复制构造逻辑；契约一变只改 factories 一处。I/O 边界（ffmpeg/CUDA/WS）集成-only，别硬 mock（见 [DEVELOPMENT.md](docs/DEVELOPMENT.md)）。
- **跨服务别建直接依赖**：共享运行态走 client 中台（`ClientManager`/`ClientQueues`），起停编排走 `RunController`；client 只吐自有词汇原始数据，展示翻译上移 router（见 [DEVELOPMENT.md](docs/DEVELOPMENT.md)）。
- **Workflow / 新检测点**：`class_name` 直接取自模型 `result.names`，不做归一化，匹配串须与训练类别名严格一致——写错会静默漏检。
- **Workflow / 新检测点**：统一检测契约是 `Detection`（单框）+ `FrameDetections`（整帧输出，见 [app/domain/detection.py](app/domain/detection.py)）；别为单个检测点塞领域字段（如 `xxx_detected/xxx_count`），派生量放 `Detection.extra` 或 `FrameDetections.metadata`，时序统计交给 L3 Analyzer。
- `/docs`、`/redoc`、`/openapi.json` 已永久关闭，别尝试打开。
