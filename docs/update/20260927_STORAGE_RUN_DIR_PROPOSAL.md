# 落盘按运行分目录：一次运行一个目录，只有分配者建目录，读侧解析一次

> **变更状态**：提案（2026-09-27）——当前系统未做任何改动，待确认后按 §8 分期实现。
> **知识库**：无需沉淀（提案，未落地；各期落地时另起「生效中」记录）

## 概述

落盘布局从 `{step}/{domain}/` 改为 `{step}/{run_id}/{domain}/`。一次运行（run）一个目录，路径不跨代复用。run 目录只由 `/api/start` 分配时建出来，所有写者都不建；读侧解析一次 run 后，整次读都锁定这个 run；删除统一改为「rename 到回收区再删」。recording、离线、TTL、回放共用这一套规则，各处自带的代次逻辑与重复的落盘原语一并收编。run 的身份收成一个只含 (task, step, run_id) 的不可变 `RunIdentity`，路径由存储层按它解析。

## 变更背景

- **现状**：同一 (task, step) 的每一代 run 共用 `{task}/{step}/hls/` 与 `{task}/{step}/inference/`。换代隔离靠调用方各自处理：
  - recording：属主队列里用 `cq` 对象身份做代次校验，再做首写自清（[`_write` / `_write_detections`](../../app/services/recording/service.py)），认领状态记在 `_claimed_hls` / `_claimed_detections` 两张表里，由 `forget_task` 回收；
  - 离线：不设防，靠「调用方只对已停写的 step 提交」这条约定（[offline/service.py](../../app/services/inference/offline/service.py)、[runner.py](../../app/services/inference/offline/runner.py)）；
  - TTL：直接 `rmtree`（[cleanup_worker.py](../../app/services/persistence/workers/cleanup_worker.py)）；
  - 读侧：每次调用都按 (task, step) 读「当前」内容，媒体 token 也只带 (task, step)。
- **根因**：一是盘上路径跨代复用，二是删除不是原子操作。
- **触发来源**：离线作业服务上线后，`inference/` 第一次有了跨进程写者；复核迟到写入防护时整理出下表。

未闭合的冲突（对照 `dev` 291bc94）：

| # | 冲突 | 现状 | 后果 |
|---|------|------|------|
| 1 | 新一代首写 vs 离线结果写入 | 无防护 | 旧结果写进新一代的 `inference/` |
| 2 | 离线作业 vs 运行中的 step | 无防护 | 按提交时已落盘的部分检测结果出结论 |
| 3 | TTL 回收 vs 离线写入 / 迟到残段 | 写侧 `create=True`；[`_jsonl.write_atomic`](../../app/storage/inference/_jsonl.py) 自带 `mkdir(parents=True)` | 重建出空壳 step，或留下删了一半的 step |
| 4 | 读侧跨代混读 | 回放的 playlist / init 文件名跨代同名；[ai.py](../../app/routers/ai.py) 的时序叠加对 hls 时间轴和 `temporal.jsonl` 各自解析 | 换代瞬间回放、lab 导出读到半新半旧的内容；叠加可能把新录像配上旧结果 |
| 5 | 首写自清 `rmtree` 失败 | [`hls.delete`](../../app/storage/hls/_write.py) / [`inference.delete`](../../app/storage/inference/_layout.py) 吞掉异常返回 False，recording 不看返回值、照样记认领 | 新旧数据混在同一目录。生产是 Linux，目录里有打开的文件也能删，所以只在 Windows 开发机上触发 |

同时，盘上原语存在多份重复实现：

| 能力 | 现有副本 |
|------|---------|
| 整体替换（tmp + `os.replace`） | [`_idx.write`](../../app/storage/hls/_idx.py)、[`_meta.record_segment`](../../app/storage/hls/_meta.py)、[`_jsonl.write_atomic`](../../app/storage/inference/_jsonl.py)、[`_temporal._write_probs_atomic`](../../app/storage/inference/_temporal.py)，共 4 份 |
| 删除目录 | `hls.delete`、`inference.delete`、`cleanup_worker._scan_and_clean`，共 3 份 |
| step 目录遍历 | `tasks._step_id_dirs`、`cleanup_worker._iter_step_dirs`，共 2 份 |
| 时间口径 | TTL 用 `{step}` 自身的 mtime 近似创建时间；`list_task_ids(order="mtime")` 下钻取最大 mtime 表示最近活动，共 2 套 |
| 代次令牌 | recording 用 `cq` 身份（两处）+ 两张认领表 + 三个 `forget_*`；离线、媒体 token 没有令牌 |

## 方案详情

### 全景：分配 → 写 → 读 → 离线锁定 → 回收

```text
盘上布局
  {root}/{task}/{step}/
    {run_id}/                  run_id = 分配时刻的微秒时间戳（十进制串），同一 step 内严格递增
      hls/                     段 init playlist sidecar metadata.json  .stage_*（写入暂存）
      inference/               detections.jsonl  temporal.jsonl  label_probs.npz
    hls/ inference/ 平铺文件    旧布局残留：读侧不可见，随所在 step 由 ⑤ 删除
  {root}/.trash/               回收区

① 分配   /api/start 在 lock_for 内：runs.allocate(task, step) → mkdir {step}/{run_id}/ → RunIdentity，挂到新建的 CQ 上
② 写     recording 两条队列、离线子进程：只写 RunIdentity 指向的目录；建域子目录不带 parents
         run 目录不在 → OSError → 这次写丢弃
③ 读     入口处 runs.query(task, step, run_id) 查询一次 → RunIdentity，往下只传它；媒体 token 带 run_id
         run_id 给了 → 该 run；为空 → 最新可见 run（私有实现，不对外）；查不到 → None → 404
         storage 读写口只收字段完整的 RunIdentity
④ 离线   admin router 同样经 runs.query 拿到 RunIdentity 交给 submit；该 run 正在运行 → 409；作业全程只读写这个 run
⑤ 回收   cleanup_worker 每轮：清空 .trash → 整个删掉过 TTL 的 step → rmdir 空的 task 目录
         不区分 run；所有删除都走 _fs.remove
```

步骤之间的硬约束：

- **① 必须在 `lock_for` 内完成**：同一 step 的分配串行执行，`run_id` 才能保证递增。**也必须早于 `client_manager.set`**：CQ 构造时就带上 RunIdentity，此后不可变。
- **② 写者不建 run 目录**：产物目录只有 `runs.allocate` 用 `mkdir(parents=True)`。run 被回收后，迟到的写入会在文件系统层原子失败，不会重建出僵尸目录。run 路径不跨代复用，所以「目录在不在」就等于「这个 run 还在不在」，没有 ABA。
- **⑤ 按 step 整体回收**：被取代的 run 随 step 一起保留到 TTL，读侧、离线锁定的 run 在此之前一直可用；回收不看 run、不看可见判据，与写侧、读侧都不需要互斥。

| 步骤 / 部件 | 落在哪 | 期 | 详见 |
|------------|--------|----|------|
| 盘上原语（②③⑤ 共用） | 新增 `app/storage/_fs.py`；`hls/_idx`、`hls/_meta`、`inference/_jsonl`、`inference/_temporal`、`hls/_write`、`inference/_layout`、`tasks`、`cleanup_worker` | 1 | §1 |
| ① 分配 | 新增 `app/domain/run.py`（`RunIdentity`）、`app/storage/runs.py`；`run_control.start_run`；`ClientQueues` 及其身份字段的读者 | 2、3、4 | §2 |
| ② 写 | `hls/_write`、`inference/_detection`、`inference/_temporal`；`recording/service.py` | 2、3 | §3 |
| ③ 读 | hls / inference 读口；`routers/{media,traceback,task,lab,ai}.py`、`services/utils/media_timeline.py`、`lab/{clip_builder,step_exporter}.py`、`traceback/media_token.py`、`tasks.list_task_ids` | 2、3、4 | §4 |
| ④ 离线锁定 | `offline/{service,cli,runner}.py`、`routers/admin.py` | 4 | §5 |
| ⑤ 回收 | `cleanup_worker.py` | 1 | §6 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| 版本层放在 step 下，分配者建目录，读侧按「有产物」判断可见（采用） | `/api/start` 在锁内多一次 mkdir；读侧需要可见判据 | 写者没有代次逻辑，recording、离线规则相同；同一 run 的 hls 与 inference 同属一个父目录，跨域配对不用再比对；回收一代只要一次 rename |
| 版本层放在域内 `{domain}/{run_id}/`，首写时 `.tmp` 再 rename 提交 | 每个域的首写都要实现提交；只有当前代能提交，recording 仍需「`current is job.cq`」判断；跨域配对要比较两边的 `run_id` | 否。它不把版本层放在域之上，理由是两条队列要共同决定可见时机，这个理由只在「首写提交」的前提下成立 |
| 小修：原子删除 + 删成功才认领 + 离线读前读后核对文件戳 | 3–4 个函数 | 否。路径仍然跨代复用，离线核对只能缩小窗口；各调用方的代次逻辑都保留 |
| 离线用 SEALED 文件判断输入完整，并在启动时补封 | 两条队列各排一个封口任务；启动时扫描整个存储根；多一种盘上文件 | 否。离线服务就在主进程里，比较 `run_id` 与当前 CQ 就能判断；进程重启后所有 run 都没有写者，天然成立 |
| `run_id` 带随机后缀 | — | 否。分配者只有一个，而且在 `lock_for` 内，`max(now_us, 已有最大 + 1)` 已经保证唯一且递增 |
| `run_id` 取 `time_ns()` | — | 否。约 1.8e18，超过 JS 安全整数 2^53；`OfflineJob` 对外带 `run_id`，前端会拿到舍入后的值。取微秒（约 1.8e15）在安全范围内，与 hls 的 `ts_us` 同口径 |
| 一个 `RunIdentity(task_id, step_id, run_id)`，只含身份；CQ、离线、读侧都用它，路径由存储层按它解析（采用） | `cq.task_id` / `cq.step_id` 的读法要迁到 `cq.run.*`，另加测试 | 进程内和盘上共用一个身份类型；「是不是同一个 run」按值比较；存储层读写口只收这一种键 |
| CQ 上在 `task_id` / `step_id` 之外再加一个 `run_id` 字段 | 改动最小 | 否。身份仍是散字段，存储层调用处每次都要现拼键；「没绑定 run」要靠 `task_id is None or step_id is None` 在各处重复判断 |
| `RunIdentity` 连 `stage`、`source_ip`、`started_at` 一起收进去，另设一个只含键的类型给存储层用 | 两个类型 | 否。stage、source_ip 是 run 的属性，不是身份；从盘上解析只能得到 (task, step, run_id)，多出的字段填不出来 |

### 1. `app/storage/_fs.py` — 盘上原语收编成一份（底座）

| 原语 | 语义 | 收编 |
|------|------|------|
| `replace(path, write_fn)` | 写同目录下的 `.{name}.tmp`，再 `os.replace`；失败就删掉 tmp，原异常上抛；**不建父目录** | 4 份整体替换。`_jsonl.write_atomic` 里的 `mkdir(parents=True)` 随之删除 |
| `remove(path) -> Removed` | 先 rename 到 `{root}/.trash/{uuid}`，再 `rmtree`。返回三态：`ABSENT`（本来就不在）/ `REMOVED` / `FAILED`（rename 失败，盘上原样不动）。rmtree 失败的留在回收区，下一轮再清 | `hls.delete`、`inference.delete` 这一期先改为调用它，第 3 期随调用方消失一起删；`cleanup_worker` 的 `rmtree` 改为调用它，长期保留 |
| `ensure_dir(path)` | `mkdir(exist_ok=True)`，**不带 parents** | 写者在 run 目录下建域子目录时用 |

`.trash/` 在存储根下，与所有产物同卷，所以 rename 是原子的。它的名字不是数字，现有的 task / step 枚举都会跳过它。`remove` 每次先 `mkdir(.trash, exist_ok=True)` 再 rename，不假设它存在。

### 2. `runs.allocate` + `RunIdentity` — 分配 run，作为 CQ 的唯一身份（对应全景 ①）

`RunIdentity(task_id, step_id, run_id)` 放在 `app/domain/run.py`（frozen dataclass、stdlib only），**只含身份，不含路径、也不含派生量**。放在 domain 层，是因为存储层与服务层都要 import 它，而 domain 不能反过来 import storage。

- **来源有两个**：`runs.allocate` 分配（活着的 run），`runs.query` 从盘上查询（读侧、离线）。两种来源得到的是同一个类型，`run_id` 一定有值；调用方不自己构造 `RunIdentity`。
- **路径只在存储层解析**：hls / inference 的读写口收 `RunIdentity`，自己拼出 `{root}/{task}/{step}/{run_id}/{domain}/`；调用方拿不到、也不拼路径。
- `app/storage/runs.py` 提供 `allocate(task, step) -> RunIdentity`：`run_id = max(time_ns() // 1000, 该 step 已有最大 run_id + 1)`，然后 `mkdir(parents=True)`。run 目录名是纯数字，用 `_root.dir_name_to_int` 解析，和旧布局的 `hls/`、`inference/` 天然区分开。
- `ClientQueues.__init__` 的 `task_id` / `step_id` 两个关键字参数合并成 `run: Optional[RunIdentity]`，裸建时为 None。`stage`、`source_ip`、`task_started_at` 是 run 的属性、不是身份，仍留在 CQ 上。
  - 「未绑定 run」只剩一个判据：`cq.run is None`。recording 里 6 处「缺 task_id / step_id」判断（`submit_segment`、`submit_detections`、`flush_residual`、`request_residual_flush`、`_take_pending_flush`、`collect_from`）随之收成这一条。
- `run_control.start_run` 在 `lock_for` 内、构造 CQ 之前调用 `allocate`。
- mkdir 失败时 start 返回失败。现在的 start 不碰盘，这是行为变化。
- 没写出任何产物就结束的 run 会留下空目录，随 step 由 ⑤ 回收。
- **迁移**：
  - 第 3 期 CQ 改收 `run`，`task_id` / `step_id` 暂时保留为只读属性，转发到 `run`；
  - 第 4 期把读者迁到 `cq.run.task_id` / `cq.run.step_id`，涉及 recording、inference、persistence、routers、stream，以及 `tests/factories.py` 与相关测试；
  - 第 5 期删掉这两个转发属性。

### 3. 写侧只写自己的 run（对应全景 ②）

- 存储层写口改为收 RunIdentity：`hls.insert_segment(run, track, frames)`、`inference.append_detections(run, frames)`、`write_temporal(run, facts)`、`write_label_probs(run, probs)`。`.stage_*` 放在 `{run}/hls/` 下。
- recording：`_SegmentJob` / `_DetectionJob` 不再携带 `cq` 与 `task_id` / `step_id`，只带 `run: RunIdentity`（原来的 `cq` 身份比较改为按值比较）；`_write` / `_write_detections` 各自只剩一次调用 `…(job.run, …)`。
  - **删除** ① 代次校验和 ② 首写自清。
  - 旧一代迟到的段写进它自己的 run 目录，新一代读不到。现在这些尾段会被丢弃，改后会保留下来，补全旧录像的结尾。
  - 如果 run 目录已被回收，写入抛 OSError，由 `SerialTaskQueue._execute` 记 error 后吞掉，和现有的失败处理一致。
- **随之消失**：
  - `_claimed_hls` / `_claimed_detections`、`forget_task` / `_forget_hls` / `_forget_detections`；
  - `run_control.stop_run` 的第 4 步；
  - 模块不变式 4（一表一队列）。
- 不变式 1（队列不能加 worker）的理由缩为一条：同一 run 内 tfdt 按执行顺序累计。
- `_pending_flush`（断流时挂起的残帧 flush 请求）的回收挪到 `flush_residual(cq)` 的拆除路径（`until_ts=None`）：按身份 pop 掉本 cq 的请求。`stop_run` 持有 `lock_for`，期间不会有新一代登记同一个键。

### 4. 读侧解析一次、全程锁定（对应全景 ③）

- storage 对外只有一个查询口 `runs.query(task_id, step_id, run_id=None) -> Optional[RunIdentity]`，router、离线提交、离线 CLI 都只调它：
  - `run_id` 给了 → 该 run 目录存在就返回，不存在（写错或已回收）返回 None。不做可见判断：调用方点名要的就是这个 run。
  - `run_id` 为空 → 最新可见的 run：run 目录按 `run_id` 降序，返回第一个可见的。可见 = run 下任一域有主产物（`hls/metadata.json` 或 `inference/detections.jsonl` 存在）。`hls/metadata.json` 由 `insert_segment` 在提交的最后一步写入，它出现时首段已经可播。「取最新」是 `runs` 的私有实现，不对外。
  - 返回 None 时 router 一律 404。
- 存储层读口改为收 RunIdentity：`hls.list_segments` / `list_segments_in_range` / `segment_path` / `init_path` / `sidecar_path` / `playlist_path` / `read_segment` / `iter_frames`，以及 `inference.read_*`。
- 调用方在入口处解析一次，往下只传 RunIdentity：traceback、task、lab、ai 四个 router，`media_timeline`、`clip_builder`、`step_exporter`。ai 叠加的 hls 时间轴与 `temporal.jsonl` 取自同一个 RunIdentity。
- **对外契约只增字段与可选参数**，老前端不用改；行为上只有 timeline 告警少了区间外的那部分：
  - 返回 `run_id`：`/history` 的 `steps[]`、活跃任务列表、lab 的 step 列表各加一个 `run_id` 字段（整数，微秒，在 JS 安全范围内）。
  - 接收可选 `run_id`：playlist、`/timeline`、`/ai/temporal`、lab 读口、离线提交与查询。统一经 `runs.query` 查询，不传时按最新 run。
  - `/timeline` 的 `events` 只含本 run 存续期内的告警：`[该 run 的 run_id 时刻, 同 step 下一个 run 的 run_id 时刻)`，最新 run 没有上界。DB 告警没有 run 维度，区间两端取自盘上的 `run_id`。
    - 不能用段的时间跨度：结算告警在 `stop_run` 拆除时生成，时间戳是停止时刻，晚于最后一段；重启是先 `stop_run` 再分配新 run，所以它一定落在本 run 的区间内，由 `media_ms_at` 贴到进度条末尾。
    - 这是对现状的修正：现在同 step 所有 run 的告警都返回，区间外的被 `media_ms_at` 堆到进度条两端。
  - 前端从列表拿到 `run_id` 后，同一页面的各个请求都带上它，跨请求也锁定同一个 run。
- `MediaTokenPayload` 增加 `run_id`：签发清单时解析一次，用它签发清单里所有段和 init 的 token；`media.py` 经 `runs.query` 拿到 RunIdentity 再取路径。run 被回收后再请求返回 404。`run_id` 在 token 里是可选字段：上线前签发的 token（最长 `media_token_ttl` = 300 s）没有它，按最新 run 解析，不因缺字段校验失败。
- `tasks.list_task_ids(order="mtime")` 改为按各 step 下最大的 `run_id` 排序，也就是最近一次 run 的开始时刻；`_latest_step_mtime` 删除。
- **第 3 期过渡**：只有读口保留旧的 (task, step) 签名，内部先 `runs.query(task, step)` 再转发（查不到按空读）；写口只收 RunIdentity。第 4 期迁完读侧调用点后删除转发。

### 5. 离线锁定 run（对应全景 ④）

- `OfflineJobService.submit(run: RunIdentity)`：服务不查询 run，由 admin router 经 `runs.query` 查好传入（查不到时 router 已 404）；
  - 解析出的 RunIdentity 等于 `client_manager` 当前 CQ 的 `run` → 409「该 step 正在运行」；
  - 通过后把 `run_id` 记进 `OfflineJob` 并传给子进程。
  - 服务按 recording 的写法注入 `clients`。
- CLI 新增可选参数 `--run-id`，入口经 `runs.query` 查询一次，之后同样锁定。服务起子进程时总是传 `--run-id`。
- runner 全程用 RunIdentity 读写。run 目录不在（所在 step 已过 TTL 被回收）时新增状态 `reclaimed`，什么都不写。
- `_jobs` 的键从 (task, step) 改为 `RunIdentity`：同一个 run 在途时返回在途作业，不同 run 各跑各的。`get` / `cancel` 同样接收可选 `run_id`，由 admin router 经 `runs.query` 解析。
  - 仍按 (task, step) 去重会出错：同 step 换代后再提交，拿到的是旧 run 的作业，`get` 显示 completed 而叠加里看不到分段；改成「run 不同就取消在途」也不行，显式指定旧 run 提交会把最新 run 的作业取消掉。
- `_RUNNER_STATUSES` 加入 `reclaimed`，否则 CLI 返回的这个状态会被解析成 failed。

### 6. 回收按 step 整体进行（对应全景 ⑤）

- 保留 `StorageCleanupWorker` 现有的判据与粒度：`{step}` 目录自身 mtime 早于 `now − cleanup_days` 就整个删掉，新旧布局一视同仁。只做三处改动：
  - 删除从 `rmtree` 改为 `_fs.remove`；
  - 每轮先清空 `.trash/`；
  - 清空 task 目录那一步只认数字目录名，不再 rmdir `.trash/`、`.lab_exports/` 这类非数字目录。
- 新布局下 `{step}` 的直接子项只有 run 目录，所以它的 mtime 就是最近一次 `runs.allocate` 的时刻：TTL 从该 step 最后一次开跑算起。
- 被取代的 run 不单独回收，随 step 一起留到 TTL：同 step 不会频繁重启，多占的盘可以忽略；回放、离线锁定的 run 在此之前一直可用，不需要宽限期。

### 7. 保留项（不改动）

- recording 的两条队列与单消费线程：保证同一 run 内的写顺序，也隔离两个域的吞吐。
- `lock_for` 内的 CAS、`remove_if`、每个 run 新建 CQ：它们管的是注册表槽位，不是盘上数据。
- `_pending_flush` 的对象身份核对。
- 离线作业串行、`JOB_TIMEOUT_S`（去重的改动见 §5）。
- `temporal.jsonl` 在 runner 里的「读回 → 合并 → 写回」：目前只有离线一个写者。
- `.lab_exports/` 与 `step_exporter` 自带的 30 分钟孤儿扫描：lab 临时件的归属随 `storage/lab.py` 立项再定。

### 8. 落地分期

按「新实现先测绿、调用点分批迁、最后单独删旧」推进，每期一份 update 记录。

| 期 | 内容 | 上线后行为 | 验证 |
|----|------|-----------|------|
| 1 | §1 `_fs` 收编 4 份整体替换，删除（含 `cleanup_worker`）改走 `_fs.remove`；§6 的 `.trash/` 清理与数字目录过滤 | 不变（删除变成原子操作） | 原有单测；新增 `_fs` 单测（三态返回、rename 失败盘上不动、replace 不建父目录）；`cleanup_worker` 不删非数字目录 |
| 2 | `app/domain/run.py`（`RunIdentity`）；§2–§4 的存储层能力：`runs`（allocate / query）+ 收 RunIdentity 的读写口，与旧签名并存，不接调用点 | 不变 | 新增单测：`run_id` 递增、可见判据、`query` 三种结果（指定存在 / 指定不存在 / 缺省取最新）、`create` 边界 |
| 3 | 写侧切换：`start_run` 分配 `RunIdentity`，CQ 改收 `run`（`task_id` / `step_id` 保留为转发属性），recording / 离线写入走 RunIdentity，写口只收 RunIdentity；读侧旧签名内部改为经 `runs.query(task, step)` 转发；随写侧切换一并删除已无调用方的旧实现：`LegacyStep`（第 2 期指向旧布局的位置键）、recording 的 ①② 与认领表、`forget_*`（`_pending_flush` 的回收挪进 `flush_residual(cq)` 的拆除路径）、`hls.delete` / `inference.delete`；改写 `test_recording_service.py` 里断言认领表的用例（①② 删除后即失效）；[detection/service.py](../../app/services/inference/online/detection/service.py) `_write_back_results` 注释里的代次隔离依据改为 RunIdentity；`docs/api` 补 `/api/start` 的 mkdir 失败情形 | 新数据落 `{step}/{run_id}/`；旧布局数据读侧不可见 | 全量 pytest + dev 端到端：启停、同 step 重启、回放、离线提交 |
| 4 | 调用点迁移：读侧与离线迁到 RunIdentity（各 router、`media_timeline`、lab、媒体 token 带 `run_id`；离线锁定 run、409、`reclaimed`）；§4 的对外契约增量（列表返回 `run_id`、读口接收可选 `run_id`、timeline 告警按 run 存续期过滤、媒体 token 的 `run_id` 可选）并同步 `docs/api`；`cq.task_id` / `cq.step_id` 的读者迁到 `cq.run.*` | 回放与离线整次锁定同一个 run；不带 `run_id` 的老请求行为不变 | 全量 pytest（含不带 `run_id` 的回归用例）+ dev 端到端：换代期间持续回放、离线期间同 step 重启 |
| 5 | 单独一次提交删旧：读口的 (task, step) 转发、`_root.path(create=)`、CQ 的身份转发属性 | 不变 | 全量 pytest |

## 变更效果（预期）

| 冲突 | 现在 | 改后 |
|------|------|------|
| #1 新一代 vs 离线写入 | 旧结果写进新一代目录 | 离线只写自己锁定的 run，与新一代在盘上不相交 |
| #2 离线 vs 运行中的 step | 按部分输入出结果 | 提交时 409 |
| #3 TTL vs 离线 / 迟到写入 | 僵尸 step 或半删的 step | 删除是原子的，写者不建 run 目录，迟到写入直接失败 |
| #4 读侧跨代混读 | 半新半旧；新录像配旧结果 | 单个请求内锁定一个 run，跨域配对取自同一个 run；前端带上 `run_id` 后跨请求也锁定 |
| #5 首写自清失败 | Windows 上新旧数据混写 | 写侧不再删除 |

| 复用 | 现在 | 改后 |
|------|------|------|
| 整体替换 | 4 份 | `_fs.replace` 1 份 |
| 删除目录 | 4 份 | `_fs.remove` 1 份 |
| step 遍历 | 2 份 | 不变（`cleanup_worker` 扫注入的根，刻意不复用） |
| 时间口径 | step mtime、下钻 mtime 共 2 套 | TTL 仍用 step mtime（新布局下即最近一次分配时刻）；最近活动改取最大 `run_id`，`_latest_step_mtime` 删除 |
| 代次令牌 | `cq` 身份 + 两张表 + 三个 `forget_*`；离线、媒体没有令牌 | `RunIdentity`：进程内按值比较，盘上是目录名，URL 里带着 `run_id` |
| run 身份 | CQ 上 `task_id` / `step_id` 两个散字段，盘上与读侧没有 run 的身份；「未绑定」靠 `task_id is None or step_id is None` 在各处重复判断 | 1 个 `RunIdentity`，CQ、离线、读侧、存储层共用；「未绑定」即 `cq.run is None` |
| recording 写入编排 | `_write` / `_write_detections` 各带 ①② 两步 | 各一次调用 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 第 3 期上线后，旧布局数据读侧不可见 | 上线前 15 天内的回放和离线结果看不到；盘上残留由 ⑤ 按 TTL 清掉 | 本次不做迁移。以后需要时写一次性脚本，把 `{step}/hls|inference/` 与平铺产物 rename 进 `{step}/{legacy_run_id}/`；未提交的 `scripts/migrate_hls_layout.py` 按新布局改写 |
| 前端没带上 `run_id` 之前仍会跨请求混读 | 回放页的 playlist、`/timeline`、`/ai/temporal` 是独立请求，不带 `run_id` 时各自解析最新 run，两次请求之间换代，页面会半新半旧 | 后端第 4 期提供能力；前端按需迁移，不阻塞后端上线 |
| 被取代的 run 随 step 保留到 TTL | 同 step 每重启一次多占一份盘，且 step 的 TTL 从最后一次开跑重新计时 | 接受：不会有频繁重启的任务 |
| hls 的可见判据依赖 `metadata.json` | 以后废掉 `metadata.json` 时，可见判据要换成其他原子出现的标记 | 废弃时同步修改 `runs` 的可见判据 |
| 同 step 重启后，新 run 先因检测结果可见、首段 hls 还没出来 | 新 run 约 1 s 就因 `detections.jsonl` 可见，首段 hls 要 10 s 以上；这期间不带 `run_id` 的回放返回 404，不再显示上一次录像 | 接受：窗口约一个段长；要看上一次录像可带其 `run_id` |
| cleanup_worker 判定过期与删除之间恰好在该 step 分配了新 run | 新 run 随 step 一起被删，整次写入失败 | 接受：前提是 15 天没动的 step 恰在扫描那一刻重启，概率可忽略 |
| 离线在 stop 之后立刻提交 | 拆除时的残余检测结果还在 detections 队列里（毫秒级），离线会缺最后一批 | 接受：离线子进程启动是秒级，实际碰不到 |
| 知识库与新布局不一致 | [DESIGN_STALE_WRITES](../kb/DESIGN_STALE_WRITES.md) §3.1 的规则 3、4 与 §6；[DESIGN_STORAGE_LAYER](../kb/DESIGN_STORAGE_LAYER.md) §6；[ARCHITECTURE_STORAGE_AND_SCHEMA](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) 的布局 | 落地后在 KB 维护时改写 |
