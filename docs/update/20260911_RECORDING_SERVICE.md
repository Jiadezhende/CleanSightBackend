# 新建 recording 服务：代次校验当乐观锁、提交序当锁，零调用点改动

> **变更状态**：生效中（2026-09-11）　<!-- 新服务已落地并有 27 条单测覆盖；本期刻意不接调用点，`hls_strategy` 仍是生产写侧，运行时行为零变化 -->
> **知识库**：待沉淀
>
> <!-- storage 分层第 2 期的下半场：第 2 期把格式收进 app/storage/hls，本篇把编排收进 app/services/recording -->

## 概述

新建 `app/services/recording/`，把 HLS 落盘的**编排**收成一个服务：周期从活跃 CQ 拉整段 →
打包成落盘任务 → 经一条 `SerialTaskQueue` 按提交序串行执行 → 出队时校验代次 → 本代次首写
时清掉上一代 → 调 `app.storage.hls.insert_segment`。全系统零锁。顺带给 `storage.hls` 补上
域粒度删除 `delete()`。**不改任何调用点**：`run_control` / `main.py` / `persistence` / 读侧
全部不动，`hls_strategy.py` 仍是当前生产写侧，运行时行为零变化。

## 变更背景

### 现状 / 痛点

[hls 域落地](20260911_STORAGE_HLS_DOMAIN.md) 把「一段帧怎么变成盘上的字节」收进了
`app/storage/hls`，但那一层**没有生产消费方**——它只回答格式，不回答编排。缺的四问：

| 编号 | 问题 | 现状 / 风险 |
|------|------|------|
| #1 | 谁来拉段、谁来打包 | 在 `persistence` 里：`HLSSegmentSweeper` + `HLSPersistenceTask` + `HLSWorkerPool`，三件东西才凑出一次落盘 |
| #2 | 同一 step 的段怎么保证有序 | 靠 `hls_strategy._dir_locks` 按目录抢锁。而锁**只能让 rmtree 等一下**，等完写者继续写 —— 它挡不住一个没停的旧 run |
| #3 | 重启时怎么不让两代产物混在一起 | `PersistenceManager.start_run` 同步 `rmtree` 整个 step 目录，跨线程与三个域的在途写零互斥（[STEP_INIT_SUPERSEDE #1](20260911_STEP_INIT_SUPERSEDE.md)） |
| #4 | 段写失败怎么办 | `GuardedExecutor` 重试 3 次。而 `insert_segment` 把清单条目排在最后登记，重试若落在「条目已追加、统计写失败」之后会写出**重复 EXTINF**，毁掉整个 step 的回放 |

### 触发来源

[STEP_INIT_SUPERSEDE](20260911_STEP_INIT_SUPERSEDE.md) 的 §1（代次校验）+ §2（hls 域首写
自清）实施。该篇把设计定了下来但代码未动，且明确写着这两件事落在一个叫 `recording` 的
模块里。

### 承接

- **顺序**由 [`SerialTaskQueue`](../../app/utils/task_queue.py) 的提交序构造。
  `app/storage/_locks.py` 已随该方案删除，本篇确认不再需要任何锁。
- **代次**沿用 `features` 侧已有的 owner fence 形态（owner = cq 对象引用判等，见
  [store.py](../../app/services/inference/feature/store.py)），不引入第二种代次表达。
- **落盘**全部委托 [`app.storage.hls`](../../app/storage/hls/__init__.py) 的
  `insert_segment` / `delete`。

## 方案详情

### 全景：一段帧从 CQ 到盘上，中间只有三个判断

```text
  [推理线程] 结果帧 ──▶ ClientQueues 缓冲
                             │
  [sweeper 线程] 每 1s 扫一遍活跃 CQ，把攒满的整段拉走
                             │  submit_segment(cq, track, frames)
                             ▼
                    打包 _SegmentJob(task, step, track, frames, cq=cq)
                             │
                    ┌────────┴────────┐  一条 SerialTaskQueue（maxsize=100）
                    │  提交序 = 执行序  │  只有一个消费线程
                    └────────┬────────┘
                             ▼  [队列线程] _write(job)
       ① 代次校验   注册表里是同 task **同 step** 的另一个 CQ？ ── 是 ──▶ 丢弃，不重试
                             │否
       ② 首写自清   我还注册着，且这个 step 我还没写过？ ── 是 ──▶ hls.delete(task, step)
                             │
       ③ 落盘       hls.insert_segment(task, step, track, frames)
```

拆除时另有一条入口：`flush_residual(cq)` 把 CQ 里不足一段的残帧按 `ca_segment_len` 切完，
走同一个 `submit_segment` —— 于是残段与整段在队列上天然保序。

**两条机制各管一件事，缺一不可**：

```text
同代次内的顺序    由提交序保证      —— 但队列解决不了换代
跨代次的隔离      由代次判等保证    —— 但校验解决不了乱序
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 拉段（PULL） | `recording/_sweeper.py` | §3 |
| 打包 + 入队 + 拒收 | `RecordingService.submit_segment` | §2.1 |
| 代次校验 / 首写自清 / 落盘 | `RecordingService._write` | §2.2 §2.3 |
| 残段收尾 | `RecordingService.flush_residual` | §2.1 |
| 代次表回收 | `RecordingService.forget_task` | §2.4 |
| 域粒度删除 | `app/storage/hls/_write.py` 的 `delete` | §1 |
| 单写者前提下的 ffmpeg 超时 | `app/storage/hls/_fmp4.py` | §1 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **一条全局队列 + 出队校验代次（采用）** | 新服务 5 个文件约 430 行；调用点迁移推到下一轮 | 采用。顺序由**提交序构造**比抢锁更强：它顺带保证「旧残段先落盘 → 再整个删掉」，而那条现在只活在 `run_control.py` 的三行注释里 |
| 保留目录锁（原 §7.4 C1-C7） | 要自己写一套 per-(task, step) 读写锁 | 否。**互斥挡不住一个没停的写者**——锁只能让 rmtree 等一下，等完它继续写，僵尸 step 照样出现 |
| 每 task 一条队列 | 一个 task 的 ffmpeg 卡住不拖累别的 task | 否（暂）。队列随 task 建销，拆除时要么阻塞 `run_control` 等排空、要么留线程；`SerialTaskQueue` 还是一次性的，重启同一 task 得重建。真到吞吐吃紧再拆，那时有数字 |
| 给代次加单调递增 id | 「谁更新」可判定，能补掉 §「已知窄缺口」 | 否。判等就够回答「是不是当前这一代」，多一个 id 就多一处要同步的状态（STEP_INIT_SUPERSEDE §1 已定） |
| 段写保留 `GuardedExecutor` 重试 | 瞬时故障能救回一段 | 否，见 §2.5 |

### 1. `app/storage/hls`：补 `delete()`，收紧 ffmpeg 超时

**`_insert.py` 改名 `_write.py`**，`insert_segment` 与新增的 `delete` 同住——两个都是本域的
对外写侧动作，一个文件放得下；拆文件是给复杂工具函数用的（`_fmp4` / `_m3u8` / `_idx` 那种）。

```python
def delete(task_id: int, step_id: int) -> bool:
    """删掉本域在该 step 下的全部产物（整个 {step}/hls/ 目录），返回它此前是否存在。"""
```

三条契约写进 docstring：**只执行不判断**（「这是不是新一代的首写」是 run 生命周期语义，
归 recording）、**只删本域**（同 step 的 `features/` / `lab/` 一个字节不碰——这正是产物按域
隔离换来的东西）、**失败不抛**（它的调用场景是「新一代开写前清上一代」，抛出去只会把一次
录制整个葬掉）。

> **`tasks.purge_step` 的 docstring 顺带订正**：原文写「刻意不提供 `purge_domain`：目前零
> 需求」——supersede 现在就是域粒度，前半句不成立了。改成「本模块是跨域的，不提供也不该
> 知道域粒度的删除」，**不点名 `hls.delete`**：跨域模块不该持有任何一个域的领域知识。

> `_layout._domain_root` 随之改名 **`domain_dir`**（去掉前导下划线）：`_write.delete` 要
> 整个删掉这个目录，而它是同包的兄弟模块。样板注记已回填 `_root.py`——薄域单文件用
> `_domain_root`，重域子包用 `domain_dir`，两者都不出包。

**`_fmp4._TIMEOUT_S` 120 → 15 秒。** 调用侧是**单写者**，一个卡住的 ffmpeg 堵的不是自己
那一段，是所有 task 的录制。实测单段 ~260 ms、1080p 满段量级 1–3 s，15 s 已是 5–10× 余量；
再大只是把「卡住」变成「卡更久」。

### 2. `app/services/recording/`

对外与私有分清楚，这是本服务的边界声明：

| 文件 | 对外 / 私有 | 内容 |
|------|------------|------|
| `__init__.py` | 对外 | 门面型 docstring + `lifespan()`（零 re-export） |
| `instance.py` | 对外 | `recording_service` 单例 |
| `service.py` | 对外 5 个方法 | 其余（`_SegmentJob` / `_write` / `_claimed_by`）全私有 |
| `_sweeper.py` | **包内私有**（前导下划线） | `SegmentSweeper`，只由 `RecordingService.start()` 构造 |
| `config.py` | 对外 | `RecordingConfig` + `get_recording_config()` |

没有 `types.py`：`_SegmentJob` 是**打包形状不是对外契约**（外面交 `cq` + 帧，拿不到也用不着
它），四行 NamedTuple 放在用它的地方旁边。

#### 2.1 对外五个方法，四个以 `cq` 为入参

```python
start() / stop(timeout=10.0)
submit_segment(cq, track, frames) -> bool    # 打包 + 入队
flush_residual(cq)                           # 拆除期切完残帧
forget_task(task_id) -> bool                 # 代次表回收（也排进队列，见 §2.4）
```

**`task_id` / `step_id` / 代次身份都在 `cq` 上，签名里不再单列**（与既有的
`flush_residual_segments(cq)` / `start_run(cq)` 同口径）。拆开传只会多三个对不上的机会。
`forget_task` 是唯一不收 `cq` 的——它就是在 CQ 已经注销之后才调的。

`submit_segment` 三种拒收，都返回 False 且不入队：队列没起、`cq` 缺 task_id/step_id
（裸建/未绑定 step，定位不到落盘分区）、空 `frames`（空段在 storage 层是 `ValueError`，
在入口拦掉，别让它变成队列里一条需要人去看的 error log）。

**入队成功 ≠ 写成功**：真正的落盘在队列线程上异步发生，且可能被代次校验丢弃。要结果的
调用方说明它本就不该异步。

#### 2.2 ① 代次校验：**连 `step_id` 一起比**

```python
if current is not None and current is not job.cq and current.step_id == job.step_id:
    return          # 换代了，丢弃
```

注册表 `client_manager` 按 **task_id** 索引，但盘上是一个 `(task_id, step_id)` 一个目录。
同一个 task 从第 2 步切到第 3 步确实换了 CQ，可**新 CQ 写的是第 3 步的目录，跟手上这批
第 2 步的段在盘上根本不冲突，它们照常落盘**。不比 step_id 就会把切步时 `stop_run` 交出来
的上一步残段全当成"过期"丢掉——它们既不覆盖谁也不被谁覆盖。

三档展开：

```text
注册表里没有该 task（已拆除）          → 没人接管这块盘 → 照常落盘
注册表里是同 task、但 step_id 不同      → 新 CQ 写的是另一个目录 → 照常落盘
注册表里是同 task 同 step 的另一个 CQ   → 本目录已换代 → 丢弃
```

**与标准乐观锁的唯一区别：失败动作是丢弃，不是重读重试。** 旧 run 的段在新 run 里没有任何
意义，重试就是把它硬塞进去。这条写进了代码注释——否则后人看见「乐观锁」三个字会顺手补一个
重试循环。

代次判等**不要求 CQ 还活着**，只要求引用还在，`close()` 之后照样能判；也因此不需要递增 id。

#### 2.3 ② 首写自清：`current is job.cq` 这个前提是防炸的

```python
if current is job.cq and self._claimed_by.get(key) is not job.cq:
    hls.delete(job.task_id, job.step_id)
    self._claimed_by[key] = job.cq
```

少了 `current is job.cq`，规则退化成「表里不是我就清」，于是**拆除之后才执行的残段**（`current`
已是 None、代次表也被 `forget_task` 清了）会把这个 step **刚写完的整段录像删掉**再写。
加上之后，清目录只可能发生在这个 CQ 还注册着的时候；残段与迟到段一律只追加、永不删。
`test_straggler_after_forget_task_never_deletes` 专盯这条。

**懒惰比 eager 好，不只是"也可以"**：新 run 若一段都没写出来，旧产物原样保留，用户还能
回放上一次的录像；eager 删除在这种情况下只留一个空 step。

**代次表里不可能登记着比自己更新的一代**：队列是 FIFO 且只有一个消费线程，新一代的第一次
提交必然晚于旧一代的所有提交。**这就是队列不能加 worker 的原因**——加了不报错，只是
tfdt 开始碰撞、旧段串进新 run。`config/recording_config.yaml` 里因此没有 `workers` 这个旋钮。

#### 2.4 `forget_task` 也走队列 —— 于是**一把锁都不需要**

不回收则 `_claimed_by` 随 `(task_id, step_id)` 单调增长（论证同原 `release_dir_locks`）。

回收动作**排进队列、不当场清**，两个收益：

1. **`_claimed_by` 从此只被队列那一个线程碰**（`_write` 读写它、`_forget` 清它，都在队列
   上）。中间版本曾给它配一把 `threading.Lock`——那把锁护的不是盘、是这张 dict 被控制面
   线程碰，而它需要三行注释解释"我不是你以为的那把锁"，本身就是信号。把唯一的跨线程调用
   挪上队列之后，**本服务与 `app/storage` 加起来零锁**。
2. **顺序对**：FIFO 保证它排在这一代所有段之后，记录活到最后一段写完才消失，而不是在
   残段还没落盘时就被抹掉。

排不进去（队列满或已停机）只是漏一条记录：payload 已释放的小壳，且下一代在这个 step
首写时会照常认领覆盖它。故用默认 timeout、**不降级同步执行**——这跟 `SerialTaskQueue`
文档里「purge 这类不许丢的任务要传大 timeout 并检查返回值」正好相反，因为它丢得起。

清空之后再执行的旧段会走 §2.3 那条「只追加不删」的路，真正的新一代会自己重新认领。

#### 2.5 失败不重试

异常由 `SerialTaskQueue._execute` 统一记 error 后吞掉，**recording 刻意不包
`GuardedExecutor`**：

| | 重试 | 不重试（采用） |
|---|---|---|
| 瞬时故障 | 可能救回一段 | 丢一段 ≈ 丢 10 秒录像 |
| 「条目已 append、统计写失败」之后重试 | 往 playlist 写出**重复 EXTINF** → 毁掉整个 step 的回放 | 不会发生 |
| 现在会抛的失败 | ffmpeg 缺失 / 换代、盘满，**基本都是非瞬时的**，重试也修不好 | 同 |

#### 2.6 `_sweeper.py`：PULL，且代次在取帧那一刻捕获

从 `persistence/workers/segment_sweeper.py` 复制（原文件不动，随接线那轮删），三处改动：
文件名加前导下划线（只由服务构造）；`snapshot_fn` / `persist_fn` 两个窄回调换成直接收
`clients` + `service`（少两层命名）；**`cq` 整个交给 `submit_segment`**，不拆成
task_id/step_id 再传——代次身份就是这个对象引用，晚一步去注册表取，取到的可能已经是新一代。

`step_id is None` 的 CQ **不取帧**就跳过：取了只能丢，留在缓冲里等它绑上 step 才对。

### 3. 保留项（刻意不改）

- `app/services/persistence/**` 原样保留，`hls_strategy` 仍是生产写侧。两份并存是自觉的
  临时状态。`persistence_config.yaml` 的 `hls:` 段照旧生效。
- `run_control.py` / `main.py` 一个字没改：`recording.lifespan()` 写好了但**没有人嵌它**，
  `start_run` 的 rmtree 还在。
- 读侧全部不动（`segment_finder.py`、6 个 router、`clip_builder`、`step_exporter`、
  `frame_tracker`）。
- TTL（`cleanup_worker` 的 `glob("*/*/metadata.json")`）不碰——它与 `metadata.json` 的落位
  必须同一次改动，而落位切换发生在接线那一刻。

## 变更效果

| 维度 | 变更前（persistence 的 HLS 路径） | 变更后（recording，接线后才生效） |
|------|--------------------------------|--------------------------------|
| 一次落盘要几个部件 | sweeper + `HLSPersistenceTask` + WorkerPool + strategy | `submit_segment(cq, track, frames)` 一句 |
| 并发控制 | `_dir_locks` 按 target_dir 抢锁 + `release_dir_locks` | **零锁**（`recording` + `app/storage` 加起来一把都没有）：一条队列一个消费线程，段写 / 删目录 / 改代次表全在它上面，互斥是"根本没有第二个线程" |
| 段写并行度 | 2 个 worker（可配 1–4，配大了静默错） | 恒为 1，且配置里没有这个旋钮 |
| 重启 supersede | 编排层 `start_run` 同步 rmtree 整个 step（含 features/lab） | 写者在本代次首写时只清自己那个域，且只在自己还注册着时清 |
| 旧 run 的迟到段 | 照写（可能串进新 run 的目录） | 同 step 换代 → 丢弃；换 step / 已拆除 → 照常落盘 |
| 切 step 时的上一步残段 | 照写 | 照写（**代次校验连 step_id 一起比**，没有回归） |
| 段写失败 | `GuardedExecutor` 重试 3 次（可能写出重复 EXTINF） | 记 error 丢这一段 |
| ffmpeg 卡住的影响面 | 120 s 超时，占住 1 个 worker | 15 s 超时，占住唯一的队列 |
| 新 run 未产出时的旧录像 | 已被 eager 删掉 | 保留 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_recording_service.py`（新增） | **31 passed**，1.2 s —— 代次校验四档（同分区换代丢弃 / 切 step 照写 / 已拆除照写 / 别的 task 无关）、懒惰 supersede 五条（首写清一次 / 第二段不再清 / **forget_task 后的残段绝不清** / 重启完整剧本 / 每个 step 独立认领）、`forget_task` 六条（走队列 / 排在段之后 / 只碰该 task / 队列未起 / 队列满漏一条不抛）、四种拒收、残段切段四条（含代次随 cq 透传）、sweeper 两条、真队列起停两条 |
| 端到端（真队列 + 真 `storage.hls`，缺 cv2/ffmpeg 时 skip） | 1 passed —— 连提交两段，断言 `{step}/hls/` 下三个 mp4 齐备、playlist 两条 EXTINF |
| `tests/test_storage_hls.py` | 74 passed（原 69 + `delete` 新增 5：清空整域 / 不碰 features / 不碰别的 step / 目录不存在返 False / 删完下次 insert 自动重建） |
| `tests/test_import_hygiene.py` | 23 passed —— 新增 `app.services.recording{,.service}` 两条预算（cv2 从 `_encode` 函数体挪到模块级会先红）+ `recording_service` 进单例引用面名单 |
| 全量 `pytest tests/` | **640 passed**，零 failed（`--ignore=tests/test_storage_locks.py`，理由见下）。既有用例只改了 `test_import_hygiene.py` 的登记行 |
| 运行时行为 | 未验证也无需验证——本期不改任何生产调用路径，`recording` 没有消费方 |

## 已知窄缺口

一个 run **从头到尾没写出过整段**、只有残段，且它结束后 task 切到了别的 step：这批残段
执行时 `cq` 已不是自己（新 CQ 在另一个 step）或已是 None，按 §2.3 不清目录，于是追加在该
step 上一代的产物后面。表现是这个 step 的 playlist 里旧录像后面接一小段新的——**内容陈旧，
但时间轴自洽、不是坏数据**（tfdt 仍严格接在累计 EXTINF 后面）。

用「可能删掉整段录像」的风险换它不划算，故保留。真要补，正解是给代次加单调 id，那是另一次
立项。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **本期无生产消费方** | `recording` 是死代码，只有单测在跑 | 分期的自觉代价。接线是下一轮的价值兑现点，不宜长期搁置 |
| **接线要一次做完四件事** | 删 `start_run` 的 rmtree、`main.py` 嵌 `recording.lifespan()`（**必须在 inference 外层**，否则队列先停、残段真丢）、`run_control` 改调 `flush_residual` / `forget_task`、删 persistence 的 HLS 四件套 | 单独一轮，届时同步删 `persistence_config.yaml` 的 `hls:` 段 |
| **落盘位置切换是 breaking、无迁移路径** | 接线那一刻起产物落 `{step}/hls/`，读侧还在看 `{step}/` —— 现有 `database/` 全部读不到 | 需人拍板清空 `database/`。读侧最小桥接只需 `SegmentFinder.task_dir()` 加一级 `hls/` + cleanup glob 跟改 + 约 8 个测试 helper 各加一段（全部读侧消费方都经 `task_dir()`，已核对） |
| **TTL 的 glob 会在 `metadata.json` 迁位时静默失效** | `glob("*/*/metadata.json")` 匹配为空，回收整个停摆且无任何日志 | 必须与接线**同一次改动**完成，不能分两次（STEP_INIT_SUPERSEDE §5 已点名） |
| **全局一条队列的容量上界** | 单段转码 ~260 ms、每 task 每 10 s 出 2 段 → 约 19 个并发 task 打满一个消费线程；再多就排队、队满丢段 | 当前部署规模远低于此。真到吞吐吃紧再考虑 per-task 队列（选型表已列代价） |
| **「队列不能加 worker」靠 review 守** | 加了不报错，表现是 tfdt 碰撞、旧段串进新 run | 配置里不给这个旋钮 + `SerialTaskQueue` 的类名本身 + 三处注释。门禁抓不到 |
| **代次校验依赖 `client_manager` 是唯一真源** | 若将来有第二处能注册/替换 CQ，乐观锁的判据就分裂了 | 现状只有 `run_control` 一处 set/remove（门禁守着引用面） |
| ~~`tests/test_storage_locks.py` 已失效~~ | `app/storage/_locks.py` 随队列方案删除，该测试文件仍在，`pytest tests/` 直接 collection error | **已解决（2026-09-11）**：经人确认后删除，详见 [STORAGE_HLS_DOMAIN](20260911_STORAGE_HLS_DOMAIN.md) 的遗留风险表 |
| **dev 端到端启停未做** | 本期不改运行时路径，理论上无影响 | 留到接线那轮一次性做。**会连真实 DB，跑前先与人确认** |

---

## 追加（2026-09-11）：`_owners` → `_claimed_by`，`job.owner` → `job.cq`

纯重命名，无行为变更。`tests/test_recording_service.py` 31 passed 不变。

**为什么 `owner` 是坏名字**：这张表的前身是 `hls_strategy._dir_locks`（按 target_dir 抢锁），
`owner` 在并发语境里默认读作「锁持有者」——而本模块的卖点恰恰是零锁，名字把刚拆掉的心智
模型又装了回去。更糟的是同一个 `if` 里的两个 `owner`（表里的、job 上的）不是一回事。

**为什么不叫 `last_submit`**：它会把一个例外说成常态。表只在「我还注册着 **且** 这个 step
我还没认领过」时写入一次，此后同代次的段照写不更新，拆除后迟到的残段也照写不更新——所以
它记的是「最后一次以现任身份认领」，不是「最后一次提交」。t1 首写登记 A、t2 `forget_task`
清表、t3 A 的残段落盘：此刻 last submit 是 A 而表是空的，名字与内容直接矛盾。

**落地的三个名字**（`_write` 里两个 cq 差别只在时间，故谁都不叫 `cq`）：

| 旧 | 新 | 语义 |
|----|----|------|
| `self._owners` | `self._claimed_by` | `(task, step)` → 认领过这个目录的那一代，一次性闩锁 |
| `job.owner` | `job.cq` | 提交那一刻的 cq |
| `_write` 局部 `cq` | `current` | 执行那一刻注册表里的 cq，可能是 None |

`20260911_STEP_INIT_SUPERSEDE.md` §1 的设计期伪代码仍写 `job.owner` / `登记本任务 owner`，
未回改——那是设计记录，`owner` 在那里读作泛指的「归属」，不是本模块的符号名。
