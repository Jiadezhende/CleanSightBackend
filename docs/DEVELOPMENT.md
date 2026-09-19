# CleanSight Backend 开发规范

本文是 CleanSight Backend 的开发**约定**：分支提交流程、测试规范、模块内聚与解耦、日志规范、检测点契约、重构节奏、注释边界。
环境安装（Linux 生产 / Windows 开发）与物料分发见 [DEPLOYMENT.md](DEPLOYMENT.md)；架构、数据流、各服务内部等描述性内容以知识库 [kb/INDEX.md](kb/INDEX.md) 为准。

---

## 1. 分支与提交流程

- **分支**：从 `dev` 切特性分支，命名 `feature/<简述>`；Pull Request 的 base 一律是 `dev`，不直接推 `dev` / `main`。
- **提交前自检**：
  - 激活项目 `.venv`（别用裸 `python3` / `python3.x`）。
  - 遵循 PEP 8。
  - `pytest` 全绿。
  - **逐个 `git add`，别用 `git add .` / `-A`**：先 `git status` 过一遍，确认只有本次任务的改动。调试中间产物（`init.mp4`、`seg0.mp4`、`pl.m3u8` 这类）、样本数据与截图、模型权重（`*.pt`）、`tmp/` 等大体积文件一律不进版本库；反复出现的加进 `.gitignore`。仓库一旦收下大文件，git 历史里就永久留着，事后再删也清不掉。
- **commit message**：`type(scope): 简述`，`type` 用 `feat` / `fix` / `docs` / `refact` / `test` / `chore`，`scope` 可选（如 `docs(kb):`、`feat(inference):`）。
- **文档纪律**：
  - **一个开发任务一份 `docs/update/YYYYMMDD_主题.md`**，记录本次的改动与结论；同一任务后续提交追加进这份，不按提交次数新建。写法与**两条状态轴**（变更状态 / 知识库）照 [update/_TEMPLATE.md](update/_TEMPLATE.md)——「知识库」轴默认填 `待沉淀`，它是 KB 融合时的欠债清单，漏填等于这次改动不会被沉淀。
  - **只动文档的任务不落 update 记录**（写进 KB、重组文档、改文档规范、改 docstring 措辞）：用一份文档记录另一份文档的诞生是递归，没人会读。判据是「跑起来的东西有没有变」，条文见 [kb/KB_MAINTENANCE.md](kb/KB_MAINTENANCE.md)。顺带改了代码行为的按代码改动处理。
  - **`docs/kb/` 不随手改**：KB 是 update 的融合产物，只在**人主动发起融合**（`/kb-merge` skill）时才写入——日常开发把增量留在 `docs/update/` 即可，别自行同步。内容验收标准见 [kb/KB_MAINTENANCE.md](kb/KB_MAINTENANCE.md)。
  - 对外 API 端点契约改动同步 `docs/api/`（该目录是端点契约真源，不走 KB 融合流程）。

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
- **不追覆盖率数字**：按 [kb/TESTING_MAP.md](kb/TESTING_MAP.md) 的「建议补测」补关键路径。典型：新增检测点补 Detector/Operator 单测 + YAML 加载测试；改 HLS 写入补 playlist EXTINF、在途段过滤、timeline 测试；改清理流程补结算告警归属测试。

---

## 3. 模块内聚 + client 中台解耦

各 service（stream / inference / persistence / traceback / lab …）功能内聚，只做自己的事；**不建 service 对 service 的直接依赖**。跨服务协作靠两个中台：

- **共享状态走 client 中台层**：跨服务需要读写的运行态统一放 `ClientManager`（COW 注册表，`int task_id` 键）+ `ClientQueues`（一次 run 的不可变身份 + 队列/快照），各服务只与 client 层打交道。
  - client 层是**零跨服务依赖的 leaf**、哑存储：本身**不构造 CQ**，CQ 由 `RunController` 建好后 `set` 换槽。
  - client 层**只吐自有词汇的原始数据**（如按流名聚合的信号）；流名 → 展示 metric 的翻译/映射**上移到 router 装配层**，不下沉进 client。
- **跨服务起停编排走 `RunController`**：一次 run 的 start/stop/restart、per-task 锁、拆机顺序、对象身份 fence 都归 RunController，不下沉到 client 或各 service。

判断落点的经验法则：一段逻辑若需要"知道另一个服务"，八成放错了——要么它属于 client 层的共享状态，要么属于 RunController 的编排，要么该由 router 在装配层翻译。

依据 KB：[kb/SERVICE_CLIENT_STATE.md](kb/SERVICE_CLIENT_STATE.md)、[kb/SERVICE_RUN_CONTROL.md](kb/SERVICE_RUN_CONTROL.md)。

---

## 4. 日志规范

- **格式** `[ModuleName] message`：方括号内 **PascalCase**（`[ClientManager]`、`[InferenceService]`）；Worker 用 `[Name-N]`（`[HLSWorker-0]`）。禁止 `print()` 代替 `logger`。
- **参数惰性格式化**：用 `%` 占位符传参，**不用 f-string**（未启用的级别不会提前拼字符串）：
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
- **日志配置**（`logging_config.json`：colorlog 彩色 console + 分级 rotating 文件，经 `uvicorn --log-config` 加载）见 [kb/SERVICE_CONFIG.md](kb/SERVICE_CONFIG.md)。

**提交前自检**：全部日志有 `[Module]` 前缀 / `%` 格式化非 f-string / 级别恰当 / 异常带 `exc_info=True` / 无 `print()` / 热路径无 DEBUG。

---

## 5. 检测点 / Workflow 契约

新建检测任务、Detector、Analyzer、Judge 走 `/infer-workflow` skill（含完整模板与 checklist）。两条会**静默出错**的红线单列在此：

- **`class_name` 不做归一化**：直接取自模型 `result.names`，配置/代码里的匹配串须与训练类别名严格一致——写错不报错，只是永远匹配不上，表现为静默漏检。
- **统一检测契约是 `Detection`（单框）+ `FrameDetections`（整帧输出）**，见 [app/domain/detection.py](../app/domain/detection.py)。别为单个检测点往契约里塞领域字段（如 `xxx_detected` / `xxx_count`）：派生量放 `Detection.extra` 或 `FrameDetections.metadata`，时序统计交给 L3 Analyzer。

---

## 6. 重构规范

**较大的重构不做破坏性一次性切换**——新旧实现在迁移期并存，按下面四步走：

1. **新实现独立落地**：写在新模块 / 新函数里，旧实现和旧调用点保持原样可用。
2. **新实现先测绿**：补齐它自己的单测并全绿，再碰任何调用点。
3. **调用点分批迁移**：每批迁完都能单独 `pytest` 跑通再进下一批，不攒着一把切。
4. **最后单独删旧**：旧实现的清理是独立一步/独立提交，不和迁移混在一个改动里。

**为什么**：一次性把调用点全切过去，单测和上游链路会同时爆炸，单次工程量与 debug 面积都失控——出问题时无法二分定位是新实现本身的 bug 还是迁移漏改。分批切换的代价只是多一段并存期，换来的是每一步都有绿测作为回退锚点。

配套：整个重构仍是**一个开发任务**，共用一份 `docs/update/YYYYMMDD_主题.md`（第 1 节文档纪律），分批提交追加进去，不按批次新建。

---

## 7. 代码内注释 / docstring 的边界

**解释性内容的家在 `docs/kb/` 与 `docs/update/`，不在代码文件开头。** 代码里的文字只回答一件事：**用这段代码，当场必须知道什么**。判据是「读者是谁」——改这个模块的人才需要设计推导，调用它的人只需要怎么调、踩什么雷。

**留在代码里**（目标：模块 docstring ≤ 15 行）：

- 一句话说清这个模块是什么，加 3–5 行调用示例。
- **会静默出错的调用约束**：一句结论 + 一条去 KB / update 的链接（如「`-ss` 必须在 `-i` 之后，否则 exit 0 产出空壳，见 `docs/kb/DESIGN_SEGMENT_CONCAT.md` §5.3」）。写结论，不写实测过程。
- 非显然的不变量与前提：并发假设、调用顺序、谁负责持锁。

**搬去 KB / update**：设计推导与权衡、坐标系 / 时间轴论证、落盘结构与架构图、域内分工表、历史变更记录（「XX 已于 2026-09-19 删除」「旧说法已被推翻」）、被否决的方案。这些的读者是维护者和后来人，他们该从 KB 入口进来，而不是靠打开某个 `.py` 碰运气发现。

**操作要点**：

- **是搬家不是删除**——先确认内容已在 KB / update 里有落点（没有就先补），再把代码里那段换成一行链接。
- **docstring 超 30 行基本等于里面混了 KB 的内容**，当成信号去拆，别当成"文档写得好"。
- 别把 `docs/update/` 的记录正文抄进 docstring：同一段论证两处维护，代码那份必然先烂掉，而读者无从判断哪份是新的。
- 包 `__init__.py` 同理。它是 facade，写清楚导出什么、有什么硬约束即可；域的完整说明归 KB 的 `DESIGN_*.md`。
