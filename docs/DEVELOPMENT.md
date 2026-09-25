# CleanSight Backend 开发规范

本文是 CleanSight Backend 的开发**约定**。
环境安装、物料分发与 `.env` / 端口配置见 `/deploy` skill（[.claude/skills/deploy/SKILL.md](../.claude/skills/deploy/SKILL.md)）；架构、数据流、各服务内部等描述性内容以知识库 [kb/INDEX.md](kb/INDEX.md) 为准。

---

## 1. 分支与提交流程

- **分支**：从 `dev` 切特性分支，命名 `feature/<简述>`；Pull Request 的 base 一律是 `dev`，不直接推 `dev` / `main`。
- **提交前自检**：
  - 激活项目 `.venv`（别用裸 `python3` / `python3.x`）。
  - 遵循 PEP 8。
  - `pytest` 全绿。
  - **逐个 `git add`，别用 `git add .` / `-A`**：先 `git status` 过一遍，确认只有本次任务的改动。调试中间产物（`init.mp4`、`seg0.mp4`、`pl.m3u8` 这类）、样本数据与截图、模型权重（`*.pt`）、`tmp/` 等大体积文件一律不进版本库；反复出现的加进 `.gitignore`。
- **commit message**：`type(scope): 简述`，`type` 用 `feat` / `fix` / `docs` / `refact` / `test` / `chore`，`scope` 可选（如 `docs(kb):`、`feat(inference):`）。
- **文档纪律**：
  - **一批原子提交一份 `docs/update/YYYYMMDD_主题.md`**（一批 = 能独立落地、测绿的一步）：推进一步就新建一份，只有回头改同分支上已提交的内容才追加进那份。写法与**两条状态轴**（变更状态 / 知识库，后者默认 `待沉淀`）照 [update/_TEMPLATE.md](update/_TEMPLATE.md)。
  - **只动文档的任务不落 update 记录**，判据是「跑起来的东西有没有变」；顺带改了代码行为的按代码改动处理。条文见 [kb/KB_MAINTENANCE.md](kb/KB_MAINTENANCE.md)。
  - **`docs/kb/` 不随手改**：只在人主动发起融合（`/kb-merge` skill）时写入，日常增量留在 `docs/update/`。验收标准见 [kb/KB_MAINTENANCE.md](kb/KB_MAINTENANCE.md)。
  - 对外 API 端点契约改动同步 `docs/api/`（端点契约真源，不走 KB 融合）。

---

## 2. 测试规范

- **两层测试**：
  - 单元 & 组件测试在 `tests/`，`pytest` 运行（先激活 `.venv`）。
  - 端到端集成测试在 `integration_tests/`，需要真实 RTSP 流与可写数据库，不在 `pytest` 默认跑。
- **测试数据构造单一真源**：
  - `tests/factories.py` 是构造真源——纯函数、**无 pytest 依赖**，`tests/` 直接 `from factories import make_cq` 复用，`integration_tests/` 也可复用。
  - `tests/conftest.py` 把 factories 包成 factory-as-fixture（如 `make_cq`），供需要注入式书写的用例用。
  - 契约一变（CQ 构造签名、`FrameDetection` 加字段等）**只改 factories 一处**，不扫散点。
- **I/O 边界故意集成-only**：子进程 ffmpeg、CUDA、WebSocket、真实 RTSP 这类外部 I/O 不硬写单测——把纯逻辑抽成 seam 单独测（如 URL 改写、去抖、时间轴计算），I/O 编排留给集成测试。
- **不追覆盖率数字**：按 [kb/TESTING_MAP.md](kb/TESTING_MAP.md) 的「建议补测」补关键路径。典型：新增检测点补 Detector/Operator 单测 + YAML 加载测试；改 HLS 写入补 playlist EXTINF、在途段过滤、timeline 测试；改清理流程补结算告警归属测试。

---

## 3. 模块内聚 + client 中台解耦

各 service（stream / inference / persistence / traceback / lab …）功能内聚，只做自己的事；**不建 service 对 service 的直接依赖**。跨服务协作靠两个中台：

- **共享状态走 client 中台层**：跨服务需要读写的运行态统一放 `ClientManager`（COW 注册表，`int task_id` 键）+ `ClientQueues`（一次 run 的不可变身份 + 队列/快照），各服务只与 client 层打交道。
  - client 层是**零跨服务依赖的 leaf**、哑存储：本身**不构造 CQ**，CQ 由 `RunController` 建好后 `set` 换槽。
  - client 层**只吐自有词汇的原始数据**（如按流名聚合的信号）；流名 → 展示 metric 的翻译/映射**上移到 router 装配层**，不下沉进 client。
- **跨服务起停编排走 `RunController`**：一次 run 的 start/stop/restart、per-task 锁、拆机顺序、对象身份 fence 都归 RunController，不下沉到 client 或各 service。

判断落点的经验法则：一段逻辑若需要「知道另一个服务」，八成放错了。

依据 KB：[kb/SERVICE_CLIENT_STATE.md](kb/SERVICE_CLIENT_STATE.md)、[kb/SERVICE_RUN_CONTROL.md](kb/SERVICE_RUN_CONTROL.md)。

---

## 4. 日志规范

- **格式** `[ModuleName] message`：方括号内 **PascalCase**（`[ClientManager]`、`[InferenceService]`）；Worker 用 `[Name-N]`（`[HLSWorker-0]`）。禁止 `print()` 代替 `logger`。
- **参数惰性格式化**：用 `%` 占位符传参，**不用 f-string**：
  ```python
  logger.info("[StreamDecoder] Connected to %s | %dx%d", url, w, h)   # ✓
  logger.info(f"[StreamDecoder] Connected to {url}")                  # ✗ 提前计算
  ```
- **级别语义**：
  - `INFO` — 里程碑：服务启停、配置加载成功、模型加载、关键业务操作、资源池/健康汇总。
  - `DEBUG` — 内部细节：单 worker 启停、队列长度、逐帧/逐批处理、配置详情块。
  - `WARNING` — 可恢复：配置缺失走默认、背压、可重试的连接失败、降级（CUDA→CPU）。
  - `ERROR` — 需人工介入的失败：操作失败、连接断开、写库/落盘失败；**带 `exc_info=True`**。
  - `CRITICAL` — 致命、无法继续：必要组件启动失败、模型文件缺失。
- **热路径不打 DEBUG**：每秒数千次的循环（帧处理）用批量/采样日志；复杂计算的日志先守卫 `if logger.isEnabledFor(logging.DEBUG):`。
- **分隔**：多参数用 `|`，列表项用 `,`；配置详情块仅 DEBUG，用 `===` 包裹。
- **日志配置**（`config/logging.json`）见 [kb/SERVICE_CONFIG.md](kb/SERVICE_CONFIG.md)。

---

## 5. 检测点 / Workflow 契约

新建检测任务、Detector、Analyzer、Judge 走 `/infer-workflow` skill（含完整模板与 checklist）。两条会**静默出错**的红线单列在此：

- **`class_name` 不做归一化**：直接取自模型 `result.names`，配置/代码里的匹配串须与训练类别名严格一致——写错不报错，静默漏检。
- **统一检测契约是 `DetBox`（单框）→ `DetectorOutput`（单检测器单帧）→ `FrameDetection`（多流对齐的整帧）**，见 [app/domain/detection.py](../app/domain/detection.py)。别为单个检测点往契约里塞领域字段（如 `xxx_detected` / `xxx_count`）：派生量放 `DetBox.extra` 或 `DetectorOutput.metadata`，时序统计交给 L3 Analyzer。

---

## 6. 重构规范

**较大的重构不做破坏性一次性切换**——一把全切过去，单测和上游同时爆，二分不出是新实现的 bug 还是迁移漏改。新旧实现在迁移期并存，按下面四步走：

1. **新实现独立落地**：写在新模块 / 新函数里，旧实现和旧调用点保持原样可用。
2. **新实现先测绿**：补齐它自己的单测并全绿，再碰任何调用点。
3. **调用点分批迁移**：每批迁完都能单独 `pytest` 跑通，再进下一批。
4. **最后单独删旧**：旧实现的清理是独立提交，不和迁移混在一起。

配套：上面每一步各自一份 update 记录（第 1 节文档纪律），不共用一篇长文。

---

## 7. 代码内注释 / docstring 的边界

**解释性内容的家在 `docs/kb/` 与 `docs/update/`，不在代码文件开头。** 代码里只写「用这段代码，当场必须知道什么」。

**留在代码里**（模块 docstring ≤ 15 行，超 30 行当成混进了 KB 内容的信号去拆）：

- 一句话说清这个模块是什么，加 3–5 行调用示例。
- **会静默出错的调用约束**：一句结论 + 一条去 KB / update 的链接（如「`-ss` 必须在 `-i` 之后，否则 exit 0 产出空壳，见 `docs/kb/DESIGN_SEGMENT_CONCAT.md` §5.3」）。
- 非显然的不变量与前提：并发假设、调用顺序、谁负责持锁。

**搬去 KB / update**：设计推导与权衡、坐标系 / 时间轴论证、落盘结构与架构图、域内分工表、历史变更记录（「XX 已于 2026-09-19 删除」「旧说法已被推翻」）、被否决的方案。

**操作要点**：

- **是搬家不是删除**：先确认 KB / update 里已有落点（没有就先补），再把代码里那段换成一行链接。
- 一段论证只留一处，别把 `docs/update/` 的正文抄进 docstring。
- 包 `__init__.py` 是 facade，写清楚导出什么、有什么硬约束即可；域的完整说明归 KB 的 `DESIGN_*.md`。

---

## 8. 导入规范

四条硬规则，全部由 [tests/test_import_hygiene.py](../tests/test_import_hygiene.py) 门禁执行（`app/` 与 `mediamtx_gateway/` 同等适用）：

- **包内一律相对、跨包一律绝对**。判据是「目标是不是我这个包的后代」，不是目录深浅：

  ```python
  # app/services/inference/online/manager.py
  from .config import load_stage_config              # ✓ 同目录
  from .detection.service import DetectionService    # ✓ 本包子包
  from app.services.client.manager import client_manager   # ✓ 跨包（跨服务依赖一眼可见）
  from app.services.inference.online.naming import stream_name    # ✗ 包内却写了绝对
  ```

- **相对导入不上翻**：只许 `from .x import`，禁止 `from ..x` / `from ...x`。要引用兄弟包或父包，写绝对路径。
- **重依赖（`torch` / `ultralytics` / `cv2`）不写在模块顶层**，写进函数体内；例外只有 `impl/` 下经 `importlib` 按配置加载的实现模块。新增分层包模块要同步在门禁的 `BUDGET` 里登记一行。
- **`__init__.py` 不 re-export 单例 / manager / impl**：模块级只允许 import 轻量类型；指向 `instance` / `manager` 的 import 写在 `lifespan()` 等函数体内。

> 前两条不是风格偏好：单例引用面与分层白名单两条门禁都按模块名判定依赖，同一条依赖若有两种写法就有绕过的口子。

背景与推导见 [update/20260903_PACKAGE_LAYOUT_SPEC.md](update/20260903_PACKAGE_LAYOUT_SPEC.md)、[update/20260922_IMPORT_CONVENTION.md](update/20260922_IMPORT_CONVENTION.md)。
