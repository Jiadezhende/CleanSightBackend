# CleanSight Backend 开发规范

本文是 CleanSight Backend 的开发**约定**：分支提交流程、测试规范、模块内聚与解耦原则。
环境安装（Linux 生产 / Windows 开发）与物料分发见 [DEPLOYMENT.md](DEPLOYMENT.md)；架构、数据流、各服务内部等描述性内容以知识库 [docs/kb/INDEX.md](docs/kb/INDEX.md) 为准。

---

## 1. 分支与提交流程

- **分支**：从 `dev` 切特性分支，命名 `feature/<简述>`；Pull Request 的 base 一律是 `dev`，不直接推 `dev` / `main`。
- **提交前自检**：
  - 激活项目 `.venv`（别用裸 `python3` / `python3.x`）。
  - 遵循 PEP 8。
  - `pytest` 全绿。
- **commit message**：`type(scope): 简述`，`type` 用 `feat` / `fix` / `docs` / `refact` / `test` / `chore`，`scope` 可选（如 `docs(kb):`、`feat(inference):`）。
- **文档纪律**：
  - 描述性内容（架构、数据流、服务内部、schema、API）改动**同步进 `docs/kb/`**，维护规则见 [docs/kb/KB_MAINTENANCE.md](docs/kb/KB_MAINTENANCE.md)。
  - 每次提交的文档增量先写 `docs/update/`，定期融合进 KB，避免双源漂移。

---

## 2. 测试规范

- **两层测试**：
  - 单元 & 组件测试在 `tests/`，`pytest` 运行（先激活 `.venv`）。
  - 端到端集成测试在 `integration_tests/`，需要真实 RTSP 流与可写数据库，不在 `pytest` 默认跑。
- **测试数据构造单一真源**：
  - `tests/factories.py` 是构造真源——纯函数、**无 pytest 依赖**，`tests/` 直接 `from factories import make_cq` 复用，`integration_tests/` 也可复用。
  - `tests/conftest.py` 把 factories 包成 factory-as-fixture（如 `make_cq`），需要注入式书写的用例用它，与 factories 同源、不产生第二份构造逻辑。
  - 契约一变（CQ 构造签名、`FrameInference` 加字段等）**只改 factories 一处**，不扫散点。
- **I/O 边界故意集成-only**：子进程 ffmpeg、CUDA、WebSocket、真实 RTSP 这类外部 I/O 不硬写单测——把纯逻辑抽成 seam 单独测（如 URL 改写、去抖、时间轴计算），I/O 编排留给集成测试。
- **不追覆盖率数字**：按 [docs/kb/TESTING_MAP.md](docs/kb/TESTING_MAP.md) 的「建议补测」补关键路径。典型：新增检测点补 Detector/Operator 单测 + YAML 加载测试；改 HLS 写入补 playlist EXTINF、在途段过滤、timeline 测试；改清理流程补结算告警归属测试。

---

## 3. 模块内聚 + client 中台解耦

各 service（stream / inference / persistence / traceback / lab …）功能内聚，只做自己的事；**不建 service 对 service 的直接依赖**。跨服务协作靠两个中台：

- **共享状态走 client 中台层**：跨服务需要读写的运行态统一放 `ClientManager`（COW 注册表，`int task_id` 键）+ `ClientQueues`（一次 run 的不可变身份 + 队列/快照），各服务只与 client 层打交道。
  - client 层是**零跨服务依赖的 leaf**、哑存储：本身**不构造 CQ**，CQ 由 `RunController` 建好后 `set` 换槽。
  - client 层**只吐自有词汇的原始数据**（如按流名聚合的信号）；流名 → 展示 metric 的翻译/映射**上移到 router 装配层**，不下沉进 client。
- **跨服务起停编排走 `RunController`**：一次 run 的 start/stop/restart、per-task 锁、拆机顺序、对象身份 fence 都归 RunController，不下沉到 client 或各 service。

判断落点的经验法则：一段逻辑若需要"知道另一个服务"，八成放错了——要么它属于 client 层的共享状态，要么属于 RunController 的编排，要么该由 router 在装配层翻译。

依据 KB：[docs/kb/SERVICE_CLIENT_STATE.md](docs/kb/SERVICE_CLIENT_STATE.md)、[docs/kb/SERVICE_RUN_CONTROL.md](docs/kb/SERVICE_RUN_CONTROL.md)。
