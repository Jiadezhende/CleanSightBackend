# 落盘分代存储（MVCC）：一代一个版本目录，换代不删，读侧取最新

> **变更状态**：提案（2026-09-26）——**当前系统未做任何改动**。现有流量下这些冲突几乎不会触发；本篇记录目标形态与分期，择期落地。
> **知识库**：无需沉淀（提案，未落地）。设计原则已写进 [DESIGN_STALE_WRITES.md](../kb/DESIGN_STALE_WRITES.md) §3.1，落地时另起「生效中」记录。

## 概述

把 `{step}/hls/` 和 `{step}/inference/` 从「各代共用一个目录、换代时整域删除」改成「一代一个版本目录 `{domain}/{run_id}/`，读侧取最大的已提交版本」。离线按锁定的 `run_id` 读写，删除改为 rename 到回收区。共分 6 期，第 1 期（删除原子化）可以独立先做。

## 变更背景

- **现状**：同一 (task, step) 的每一次 run 共用 `{task}/{step}/{domain}/`。新一代首次写入时，recording 在属主队列里整域删除上一代（`_write` / `_write_detections` 的 ②）；离线子进程不做任何换代 / 回收判断（戳核对与 `create=False` 曾在本分支落地，2026-09-26 为保最简已删除），作业服务也不查 step 是否在 live；TTL 直接 `rmtree`。
- **根因**：各种冲突都出在两件事上——盘上路径跨代复用，以及删除不是原子操作。
- **触发来源**：离线作业服务上线后，`inference/` 域第一次有了跨进程写者；复核迟到写入防护时整理出下表。

冲突清单（2026-09-26 对照 `feat/offline-scheduler` 提交 `1e6336e` 之后的代码）：

| 资源 | 冲突的写 | 现有手段 | 状态 |
|------|---------|---------|------|
| run 注册表槽位 | `/api/start` 重启 vs HM 迟到清理 | `lock_for` 内 CAS | 已闭合 |
| CQ 内数据 | 拆除 vs 抽帧、结果写回、tick | 每个 run 新建 CQ | 已闭合 |
| `{step}/hls/`、`{step}/inference/` | 新一代首写整域删除 vs 旧一代的段和检测结果 | 属主队列 + `cq` 令牌 | Linux 已闭合。**Windows 未闭合**：目录里有文件被打开（回放、lab 导出、离线读取）时，`rmtree` 删到一半就失败；`hls.delete` / `inference.delete` 吞掉异常返回 False，recording 不看返回值、照样记下认领，新一代接着写进残留的旧 playlist / 旧 `detections.jsonl`（按 Windows 删除语义推断，未实测） |
| recording 代次表 | `forget_task` vs 本代写入 | 队列 FIFO | 已闭合 |
| `temporal.jsonl` | 同一 (task, step) 的两次离线写入 | 作业队列串行 + 提交去重 | 已闭合（手动并行跑 CLI 不在保证范围内） |
| `detections.jsonl` | 在线追加 vs 离线读取 | 无（约定只对已结束的 step 提交） | **未防**：对运行中的 step 提交，按当时已落盘的部分出结果 |
| `{step}/inference/` | 新一代首写整域删除 vs 离线结果写入 | 无 | **未防**：离线作业全程都是窗口，旧结果会写进新一代目录 |
| `{step}/` | TTL 回收 vs 离线结果写入 | 无 | **未防**：目录已删完时写入会重建出只有 `inference/` 的空壳 step；`rmtree` 进行中时留下删了一半的 step |
| `{step}/` | TTL 回收 vs recording 拆除后迟到的残段 | 无 | **未闭合**：会出现僵尸 step；靠 TTL 15 天 vs 任务超时 30 分钟的量级差，目前触发不到 |

目前可以接受的理由：离线只能从 admin 页手动提交、并发量极低；结果只供 admin 页可视化，不产生告警，也不写 DB，写错了重跑一次就会覆盖。一旦离线结果开始被业务读取，或者 Windows 成为生产平台，就需要落地本提案。

## 方案详情

### 全景

```text
① /api/start（lock_for 内）   run_id = runs.allocate(task, step)，挂到新建的 CQ 上
② 首写（域属主队列）           首份产物写进 {domain}/.{run_id}.tmp/ → rename 成 {domain}/{run_id}/     ← 提交即生效
③ 运行期写入（域属主队列）     只写 {domain}/{run_id}/，create=False
④ 拆除（stop_run）             残帧 flush → seal_run(cq) 排进两条属主队列 → 各自落 SEALED
⑤ 读                           runs.latest(task, step, domain) 只解析一次 → RunRef；之后所有读写都只收 RunRef
     回放 / 时间轴               媒体 token 带上 run_id
     离线作业                    提交时锁定 RunRef → 要求有 SEALED → 读写都在 {run_id}/，create=False
⑥ 回收（TTL 线程）             非最新、且后继版本提交超过宽限期 → rename 到 {root}/.trash/ → rmtree
                               整个 step 过 TTL：同样先 rename 再删；删除失败下一轮重试
```

步骤之间的硬约束：

- **① 必须在 `lock_for` 内**：同一 step 的分配串行执行，版本号才能保证单调。
- **② 必须先写后 rename**：目录一出现就对读者可见，空版本会遮住上一代的录像。
- **④ 必须排在本代所有写之后**：靠属主队列的 FIFO 保证，不能在控制面线程上当场写。
- **⑥ 只回收非最新的版本**：版本号只增，「非最新」一旦成立就不会翻转，所以回收和写侧不需要互斥。

落盘布局：

```text
{root}/{task}/{step}/
  hls/        {run_id}/        段 init playlist sidecar metadata SEALED
              .{run_id}.tmp/   首写进行中，读者跳过
  inference/  {run_id}/        detections.jsonl  temporal.jsonl  label_probs.npz  SEALED
  lab/        不分代（临时件，用完即删）
{root}/.trash/                 回收区，名字不是数字，TTL 扫描与 tasks 列举都跳过

run_id = {ts_ns}-{rand8}       ts_ns = max(time_ns(), 该 step 已有最大 ts + 1)；排序只看 ts，身份比较用整串
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 删除原子化（⑥ 的前置，可独立先做） | `app/storage/_root.py`、`hls/_write.py`、`inference/_layout.py`、`tasks.py`、`cleanup_worker.py`、`recording/service.py` | §1 |
| 版本层、`RunRef`、分配 / 解析 / 提交 / 封口 | `app/storage/_root.py`、新增 `app/storage/runs.py`、`app/storage/types.py`、hls / inference 读写口 | §2 |
| ① 分配 run_id；②③ 写侧提交；④ 封口 | `run_control.start_run`、`ClientQueues`、`recording/service.py` | §3 |
| ⑥ 版本回收 | `cleanup_worker.py` | §4 |
| ⑤ 在线读侧锁定 | `MediaToken`、`routers/media.py` / `traceback.py` / `task.py` / `lab.py` / `ai.py`、`services/utils/media_timeline.py`、`lab/clip_builder.py` / `step_exporter.py` | §5 |
| ⑤ 离线锁定 | `offline/service.py` / `cli.py` / `runner.py`、`storage/inference/_detection.py` | §6 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| 分代版本目录，读侧取最大（采用） | 存储层加版本层，读写口改收 `RunRef`；recording 首写改为提交 | 各代写入在盘上不相交；认领表整套删掉 |
| 分代 + `CURRENT` 指针文件 | 多一个必须和目录保持一致的文件 | 否。版本号只增时「最新」就是「当前」，指针是冗余的 |
| 小修：rename 删除 + 删除成功才认领 + 读侧比戳 | 3–4 个函数 | 否。能闭合，但路径仍跨代复用，令牌和戳的推理一直留着；其中的删除原子化被本方案第 1 期吸收 |
| 统一的 step 属主串行点（TTL、离线提交、两条 recording 队列合流） | 牺牲两条队列的吞吐隔离；offline 服务要依赖 recording 服务 | 否 |
| 版本层放在域之上（`{step}/{run_id}/{domain}/`） | hls、inference 两条队列要共同决定版本何时可见，破坏「一张表一条队列、零锁」 | 否。版本层放在域之内，各域各自提交 |
| run_id 直接用墙钟 | 时钟回拨会让新版本不可见，还会被当成旧版本回收 | 否。用 `max(now, 已有最大 + 1)` 加随机后缀 |

### 1. 删除原子化（第 1 期，可独立落地）

- `app/storage/_root.py` 新增原子删除：先 rename 到 `{root}/.trash/{uuid}`，再 rmtree。rename 失败时返回「没删」，盘上原样不动；rmtree 失败的留在回收区。
- 返回值要能区分「本来就不在」「已删」「删不掉」三种情况。现在的 `bool` 把「不在」和「失败」都表达成 False。
- `hls.delete`、`inference.delete`、`tasks.delete_step`、`cleanup_worker._scan_and_clean` 都改走它；TTL 每一轮先清空回收区。
- recording `_write` / `_write_detections` 的 ②：删不掉就不记认领、丢弃本批，下一批再试着认领。这一条到第 3 期随首写自清一起删掉，但在那之前它修的是 Windows 上正在发生的问题。
- 启动校验：`cleanup_days × 86400` 远大于 `task_max_duration`（例如至少 10 倍），否则拒绝启动。
- 实测：在 Windows 上确认「目录里有文件被打开时 rmtree 删一半、rename 整体失败」这两个行为。

### 2. 存储层分代能力（第 2 期：新实现写完、测好，不接调用点）

- `app/storage/types.py` 新增 `RunRef(task_id, step_id, run_id)`。
- 新增 `app/storage/runs.py`：
  - `allocate(task, step) -> str`：扫描该 step 下所有域的版本（含 `.tmp`），按全景里的规则生成新的 `run_id`；
  - `latest(task, step, domain) -> Optional[RunRef]`：跳过点号开头的目录，按 ts 前缀取最大；
  - `seal(ref, domain)` / `is_sealed(ref, domain)`。
- `_root.path` 增加版本这一级。只有提交步骤会建目录（建的是 `.tmp`），其余写入一律 `create=False`。
- hls、inference 的读写口新增收 `RunRef` 的版本。提交怎么做（`begin/commit` 两步，或首写参数），实现时再定。
- 旧的 (task, step) 签名保留：第 3 期起在内部解析最新版本作为过渡，第 5 期删除。

### 3. 写侧接线（第 3 期）

- `ClientQueues` 新增 `run_id` 字段，由 `run_control.start_run` 在 `lock_for` 内、构造 CQ 之前分配。
- recording `_write` / `_write_detections`：
  - ① 代次校验保留；
  - ② 首写自清改为：本代版本目录不存在且 `current is job.cq` → 在 `.tmp` 里写首份产物并提交；不存在且不是当前代 → 丢弃（迟到写者不建目录）；已经存在 → `create=False` 写入。
  - 删掉 `_claimed_hls` / `_claimed_detections`：是否已认领，看本代版本目录在不在。
- `forget_task(task_id)` 改为 `seal_run(cq)`：两条队列各排一个，各自落本域的 `SEALED`；`_pending_flush` 的回收并入 hls 队列上的那一个。
- `recording.start()` 在启动 sweeper 之前先补封：扫描存储根，给所有未封口的版本落 `SEALED`（进程刚启动，没有任何一代在写）。
- 读侧这一期不改调用点，旧签名在内部解析最新版本。
- **第 4 期必须和第 3 期同批上线，或紧随其后**，否则同一 step 每重启一次就多留一个版本，直到 TTL 才回收。

### 4. 版本回收（第 4 期）

- `cleanup_worker` 按 step、按域列出版本：凡是非最新、且后继版本目录的 mtime（约等于提交时间）早于宽限期的，走原子删除。
- 超过宽限期的 `.tmp` 目录（属主首写途中崩溃留下的）同样回收。
- 宽限期默认 1 小时，必须大于离线作业超时 `JOB_TIMEOUT_S`（30 分钟）。
- step 级 TTL 判据不变：版本目录建在 `{domain}/` 之下，不会刷新 `{step}/` 的 mtime。

### 5. 在线读侧锁定（第 5 期）

- `MediaToken` 的 payload 加上 `run_id`。渲染 playlist 时只解析一次版本，用它签发该清单里所有段和 init 的 token；`media.py` 按 `RunRef` 取路径。旧版本回收之后，再请求它就返回 404。
- traceback、task、lab、ai 这几个 router，以及 `media_timeline`、`clip_builder`、`step_exporter`：都在入口处调用 `runs.latest` 一次，往下只传 `RunRef`。
- 跨域配对（ai 的离线结果叠加在 hls 时间轴上）比较两边的 `run_id`；不相等时，返回「结果与录像不属于同一次运行」。
- 删掉旧的 (task, step) 读写签名，从此不再有「默认取最新」的接口。

### 6. 离线切到锁定版本（第 6 期）

- `submit`：
  - `runs.latest(task, step, "inference")` 为空 → 400「无检测结果」；
  - 版本未封口 → 409「未封口」；
  - 通过后把 `run_id` 记进 `OfflineJob`。
- CLI 新增 `--run-id`；不带时在入口解析一次，之后同样锁定。
- runner 通过 `RunRef` 读写：
  - 写入传 `create=False`（存储层届时新增），只写锁定的版本，不建目录；
  - 读取或写入时版本已经不在 → 新增状态 `superseded`，什么都不写。
- 作业服务不需要 live 检查：「输入是否完整」看 `SEALED`；新一代开跑后，旧版本上的作业结果依然有效，不必 kill。

### 7. 保留项（不改动）

- recording 两条队列与单消费线程；`_write*` 的 ① 代次校验。
- `lock_for` 内 CAS、每个 run 新建 CQ。
- 离线作业队列串行、提交去重、`JOB_TIMEOUT_S`。
- step 级 TTL 判据（`{step}/` 目录自身的 mtime）。
- `temporal.jsonl` 暂不按 producer 拆文件：目前只有离线这一个写者。

## 变更效果（预期）

| 冲突 | 现在 | 分代后 |
|------|------|--------|
| 新一代首写 vs 旧一代写入 | 属主队列 + `cq` 令牌；Windows 上会删一半 | 各写各的版本，写侧不再删除 |
| 对运行中的 step 提交离线 | 按部分输入出结果 | 未封口 → 409 |
| 新一代 vs 离线写入 | 无防护，旧结果写进新一代目录 | 离线只写锁定的版本，和新一代在盘上不相交 |
| TTL vs 离线写入 | 重建空壳 step 或留下删了一半的 step | 原子删除 + 离线不建目录 |
| TTL vs 迟到残段 | 僵尸 step | 原子删除 + 迟到写者不建目录 |
| recording 代次表回收 | 靠 `forget_task` 回收 | 表本身删掉 |

- **删掉的机制**：`_claimed_hls` / `_claimed_detections`、`forget_task`。
- **新增的机制**：`run_id` 分配、版本提交、`SEALED` 与启动补封、版本回收、回收区；离线侧的 `create=False` 写入、`superseded` 状态、提交时的封口校验。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 老的平铺数据（`{domain}/` 下直接放文件）在新布局下不可见 | 第 3 期上线后看不到上线前的回放和检测结果 | 第 3 期前决定：沿用「不做兼容、随 TTL 自然消失」，或写迁移脚本把平铺内容挪进一个版本并封口 |
| 重启过的 step 在宽限期内占两份盘 | 磁盘占用短时翻倍 | 宽限期做成可配置 |
| 启动补封要扫描整个存储根 | 启动耗时随 step 数线性增长 | 实测规模后再决定是否改成读取时惰性判断 |
| 看旧版本回放的过程中，旧版本被回收 | 后续段返回 404 | 接受：只影响在宽限期之后还在看旧版本的人 |
| Windows 的 rmtree / rename 行为是推断的 | 第 1 期的判断依据 | 第 1 期先实测 |
| 同一 (task, step) 手动并行跑 CLI | `temporal.jsonl` 的「读回 → 合并 → 写回」没有互斥 | 维持「不支持」；出现第二个 temporal 写者时按 producer 拆文件 |
