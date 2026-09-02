# CleanSightBackend — 开发手册

AI 视觉巡检后端系统，对实时 RTSP 流提供推理、HLS 录制、告警上报和回放分析服务。
应用服务入口 [app/main.py](app/main.py) → FastAPI lifespan 依次启动各 Service 单例。
视频推流入口 [mediamtx_gateway/main.py](mediamtx_gateway/main.py)

本文件只做**导航与硬约束**：分诊到正确的文档、定位代码、列出容易默认做错的红线。描述性内容一律去下列文档，不在此重复。

---

## 文档路由

| 我要… | 去哪 |
|------|------|
| 懂架构 / 数据流 / 各服务内部 / schema（描述性） | 先读知识库 [docs/kb/INDEX.md](docs/kb/INDEX.md)，**再扫 [docs/update/](docs/update/) 里晚于 KB 更新时间的增量**（KB 定期融合、可能滞后，见下方注） |
| 对外 HTTP / WS 端点契约（请求响应 schema、字段语义、错误码） | [docs/api/](docs/api/)（按 router 分文件，README 是索引 + 全局约定）；路由怎么接线属架构，去 KB |
| 开发规范：分支提交、测试、模块内聚与解耦、日志、检测点契约 | [DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| 部署：Linux 生产 + Windows 开发安装、物料分发 | [DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| 定位代码：目录结构 → [README.md](README.md) 项目结构；某个服务内部 → KB 对应 `SERVICE_*.md` | — |

> **KB 不是最新的**（增量先落 `docs/update/`、定期才融合进 KB）：INDEX 顶部有「更新时间」，`docs/update/` 里文件名日期晚于它的都是尚未融合的增量——查某主题最新状态时，KB 结论要用 update 增量校正后再采信。写文档的细则见 [KB_MAINTENANCE.md](docs/kb/KB_MAINTENANCE.md)。

---

## 快速启动

```bash
./start_backend.sh dev          # Linux（加载 .env.dev，脚本内已后台拉起网关）
.\start_backend.ps1 dev         # Windows

# 不走脚本时两个进程都要起（缺网关 = 无 MediaMTX = 后端无流可拉）
python -m mediamtx_gateway.main # RTSP 网关：TCP 代理 + 拉起并守护 MediaMTX
python -m app.main              # 后端

pytest tests/                                        # 单元 & 组件测试
python integration_tests/test_single_client.py --scenario 1 --task_id 1  # 集成测试（需真实 RTSP 流）
```

---

## 行为规范

> 每轮都可能踩的红线；其余约定（测试、解耦、日志、检测点契约）正文在 [DEVELOPMENT.md](docs/DEVELOPMENT.md)，动到对应主题时去读。

**动手前**

- **开发只跑 dev / test 环境，不碰 prod**：prod 会写真实 DB、发真实告警。这不是默认值问题——没有「顺手用 prod」的场景；即便在 dev/test，跑会写库或发告警的端到端测试前也先跟人确认（环境与端口见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)）。
- **跑任何 python/pytest 前先激活项目 `.venv`**，别用裸 `python3`。
- **先评估现有能力再设计，别重复造轮子**：动手前查 KB + 代码里已有什么（数据模型、服务、client 中台、工具函数），能复用或扩展就不新起一套；确实要新建，先说清现有的哪里不够。

**动手后**

- **改动留档**：**一个开发任务一份** `docs/update/YYYYMMDD_主题.md`，同一任务的后续改动追加进这份，别每改一次新建。
- **不主动写 `docs/kb/`**：KB 是融合产物，只在人发起维护流程时才更新——日常改动留在 update 里，别顺手同步进 KB。

**汇报时**

- **只上报 P0/P1**：阻塞、正确性错误、数据/安全风险要讲；无需决策的实现细节、已按惯例处理掉的小事不提——开发者的处理带宽有限，别拿噪声填满。
- **要人决策就给决策依据**：列出选项、各自代价与影响面、你的推荐和理由，让人做选择题；别把一个裸问题甩过去让人从头想。
