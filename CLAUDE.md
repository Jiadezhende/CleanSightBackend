# CleanSightBackend — 开发手册

AI 视觉巡检后端系统，对实时 RTSP 流提供推理、HLS 录制、告警上报和回放分析服务。
应用服务入口 [app/main.py](app/main.py) → FastAPI lifespan 依次启动各 Service 单例。
视频推流入口 [mediamtx_gateway/main.py](mediamtx_gateway/main.py)

本文件只做**导航与硬约束**，描述性内容一律去下列文档。

---

## 文档路由

| 我要… | 去哪 |
|------|------|
| 懂架构 / 数据流 / 各服务内部 / schema（描述性） | 先读知识库 [docs/kb/INDEX.md](docs/kb/INDEX.md)，**再扫 [docs/update/](docs/update/) 里晚于 KB 更新时间的增量**（见下方注） |
| 对外 HTTP / WS 端点契约（请求响应 schema、字段语义、错误码） | [docs/api/](docs/api/)（按 router 分文件，README 是索引 + 全局约定）；路由怎么接线属架构，去 KB |
| 开发规范：分支提交、测试、模块内聚与解耦、日志、检测点契约 | [DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| 部署：装环境、物料分发、`.env` / 端口、启动验证（Linux / Windows / PPU） | `/deploy` skill（[.claude/skills/deploy/SKILL.md](.claude/skills/deploy/SKILL.md)，先定平台与角色再读对应 references） |
| 定位代码：目录结构 → [README.md](README.md) 项目结构；某个服务内部 → KB 对应 `SERVICE_*.md` | — |

> **KB 不是最新的**：INDEX 顶部有「更新时间」，`docs/update/` 里文件名日期晚于它的都是尚未融合的增量——KB 结论要用这些增量校正后再采信。写文档的细则见 [KB_MAINTENANCE.md](docs/kb/KB_MAINTENANCE.md)。

---

## 快速启动

```bash
./start_backend.sh dev          # Linux（加载 .env.dev，脚本内已后台拉起网关）
.\start_backend.ps1 dev         # Windows

# 不走脚本时两个进程都要起
python -m mediamtx_gateway.main # RTSP 网关：TCP 代理 + 拉起并守护 MediaMTX
python -m app.main              # 后端

pytest tests/                                        # 单元 & 组件测试
python integration_tests/test_single_client.py --scenario 1 --task_id 1  # 集成测试（需真实 RTSP 流）
```

---

## 行为规范

> 每轮都可能踩的红线；其余约定正文在 [DEVELOPMENT.md](docs/DEVELOPMENT.md)，动到对应主题时去读。

**动手前**

- **开发只跑 dev / test 环境，不碰 prod**（prod 写真实 DB、发真实告警）；即便在 dev/test，跑会写库或发告警的端到端测试前也先跟人确认（环境与端口见 `/deploy` skill 的 [runtime-config.md](.claude/skills/deploy/references/runtime-config.md)）。
- **跑任何 python/pytest 前先激活项目 `.venv`**，别用裸 `python3`。
- **先评估现有能力再设计，别重复造轮子**：动手前查 KB + 代码里已有什么（数据模型、服务、client 中台、工具函数），能复用或扩展就不新起一套；确实要新建，先说清现有的哪里不够。

**动手后**

- **改动留档**：**一批原子提交一份** `docs/update/YYYYMMDD_主题.md`（一批 = 能独立落地、测绿的一步）——推进一步就新建，只有回头改同分支上已提交的内容才追加进那份。**只动文档的任务不落记录**（判据：跑起来的东西有没有变）。
- **不主动写 `docs/kb/`**：只在人发起维护流程时才更新，日常改动留在 update 里。

**汇报时**

- **只上报 P0/P1**：阻塞、正确性错误、数据/安全风险要讲；无需决策的实现细节、已按惯例处理掉的小事不提。
- **要人决策就给决策依据**：列出选项、各自代价与影响面、你的推荐和理由，让人做选择题。
- **结论先行，标题写结论**：开头 ≤3 行给结论和行动项，论证在后；小标题要能单独扫读成句（「attn_mask 不省内存」而不是「一个实现坑」），扫完标题≈读完摘要。
- **推翻自己只说一次**：上一轮的结论错了要显式讲——改什么、影响什么——但一句话说完就继续，不复盘、不反复肯定对方直觉。
- **格式守两条**：需要对齐的内容（ASCII 图、表、代码）必须进代码块；同一层级的列表项粒度保持一致，一句话和一整屏别混在一层。
- **指代就地可解**：默认读者只看这一条消息、不回翻上文，每个指代都要当场能确定指的是什么——自造名词首次出现带一句定义，隔了几轮的事重提时补一句它是什么，序号/同名/简写换成带内容的全称（不是「第一点提过」，而是「方案 B 的按域隔离」）。
