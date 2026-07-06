# CleanSightBackend — 开发者快速指南

AI 视觉巡检后端系统，基于 FastAPI + YOLOv8：实时 RTSP 流推理、HLS 录制与告警上报。
入口 [app/main.py](app/main.py) → FastAPI lifespan 依次启动各 Service 单例。

---

## 快速启动

```bash
./start_backend.sh dev          # Linux（加载 .env.dev）
.\start_backend.ps1 dev         # Windows
python -m app.main              # 直接运行

pytest tests/                                        # 单元 & 组件测试
python integration_tests/local_full_pipeline_rtsp.py # 集成测试（需真实 RTSP 流）
python -m mediamtx_gateway.main                       # RTSP TCP 代理网关（可选，对外部署）
```

---

## 硬规矩（不遵守会默认做错）

- 跑任何 python/pytest 前先激活项目 `.venv`，别用裸 `python3`。
- 部署默认 **dev**，别擅自当 prod；跑会写库/发告警的端到端测试前先确认。
- `/docs`、`/redoc`、`/openapi.json` 已永久关闭，别尝试打开。
- **Workflow**：`class_name` 直接取自模型 `result.names`，不做归一化，匹配串须与训练类别名严格一致。
- **Workflow**：`DetectionOutput` 是统一检测契约，别为单个检测点塞领域字段（如 `xxx_detected/xxx_count`）；派生量放 `Detection.extra` 或 `metadata`，时序统计交给 L3 Analyzer。

---

## Skill 路由（不可发现，必须显式用）

| 场景 | Skill |
|------|-------|
| 新建检测任务 / Detector / Analyzer / Judge | `/infer-workflow` |
| 查表结构、对比 ORM 与实际表 | `/schema-inspect` |
| 本地/远程 Linux GPU 主机部署 | `/deploy-linux` |

---

## 架构与设计以 KB 为准

目录结构、三大服务内部、数据流、异常分层、配置、schema、API 清单等**描述性**内容一律以单仓知识库 `docs/kb/` 为准，入口 [docs/kb/INDEX.md](docs/kb/INDEX.md)。CLAUDE.md 不重复描述，避免与 KB 双源漂移。

- 每次提交的增量写入 `docs/update/`，定期融合进 KB。
- KB 维护规则见 [docs/kb/KB_MAINTENANCE.md](docs/kb/KB_MAINTENANCE.md)。
