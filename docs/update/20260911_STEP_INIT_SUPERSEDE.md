# step 初始化改为域内懒惰 supersede：写者首写自清，编排层零 purge 调用点

> **变更状态**：提案（2026-09-11）　<!-- 设计已收敛；§1/§2 的实现已落地但未接线，`start_run` 当前仍是整 step rmtree -->
> **知识库**：待沉淀
>
> **追加（2026-09-11）**：§1（代次校验）与 §2（hls 域首写自清）**的实现已落地**在新建的
> `app/services/recording/`，`storage.hls` 也补上了 §2 要的域内清理入口（`hls.delete`），
> 见 [20260911_RECORDING_SERVICE](20260911_RECORDING_SERVICE.md)。**但接线未做**——
> `start_run` 的 rmtree 还在、`main.py` 没有嵌 `recording.lifespan()`、生产写侧仍是
> `hls_strategy`，故本篇状态维持「提案」，实施顺序里的第 2 步只完成了前两项。
>
> **一处订正（§1）**：「owner is 当前 cq」**只比 task_id 不够**。注册表按 task_id 索引，
> 而盘上是一个 `(task_id, step_id)` 一个目录——同一个 task 从第 2 步切到第 3 步也换代，
> 但新 CQ 写的是另一个目录，**跟第 2 步那批段在盘上不冲突，它们照常落盘**。只比 task_id
> 会把切步时 `stop_run` 交出来的上一步残段全当成过期丢掉。落地的判据是
> 「`cq is not job.owner` **且** `cq.step_id == job.step_id`」才算换代。
>
> <!-- 与 20260909_STORAGE_LAYER_BASE 的关系见「触发来源」：本篇替换其 §7.4 的并发结论 -->

## 概述

把「重启时由编排层删整个 step 目录」换成「各域写者在**本代次首次写入**时清自己那个域」。
旧代次的在途任务由 owner 校验（乐观锁）在出队时丢弃，同代次内的顺序由 `SerialTaskQueue`
的提交序保证。`run_control` 的 start/stop 路径里此后**不出现任何 purge 调用**。影响
`run_control`、`persistence`、`inference/feature`，storage 侧需新增一个域内清理入口。

## 变更背景

### 现状 / 痛点

[`PersistenceManager.start_run`](../../app/services/persistence/manager.py) 在每次 run 起始
同步 `rmtree` 整个 `{task_id}/{step_id}/` —— 含 `hls/`、`features/`、`lab/` 三个域。三个问题：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | **跨线程的删-写零互斥**：rmtree 在 `run_control` 线程，`FeatureStore.append` 在推理线程，lab 导出在 FastAPI 请求线程 | 某个域的目录删到一半，或写侧 `create=True` 在 rmtree 之后把目录重建出来、留一个已被记账删除的僵尸 step。**两种都不报错** |
| #2 | **eager 删除**：新 run 一起来就删掉上一次的产物 | 新 run 若立刻失败，用户连旧录像也看不了——删得比需要的早 |
| #3 | **没有域粒度**：supersede 是「删整个 step」这一个动作 | 将来新增第四个域时，如果改成逐域调用，编排层要记得补一行；漏了就是旧产物静默残留进新 run |

#1 的历史处置是「让 supersede 与写者持同一把锁」，见 [20260909 分层记录](20260909_STORAGE_LAYER_BASE.md)
的 §7.4（C1–C7）。**那套方案已作废**：锁保证的是「不重叠」，而这里需要的是「旧 run 已终止」
——一个还在写的 run，锁只能让 rmtree 等一下，等完了它继续写，僵尸 step 照样出现。互斥挡不住
一个没停的写者。

### 触发来源

[hls 域落地](20260911_STORAGE_HLS_DOMAIN.md)（分层第 2 期）删除了 `app/storage/_locks.py`，
顺序改由 [`SerialTaskQueue`](../../app/utils/task_queue.py) 的提交序构造。锁没了之后
「谁保证 supersede 与在途写不打架」需要重新回答——本篇就是那个答案。

### 承接

- **顺序**由 `SerialTaskQueue` 保证：一条队列一个消费线程，提交序 == 执行序。
- **代次**沿用 `features` 侧已有的 owner fence 形态（`owner = cq` 对象引用判等，
  见 [`store.py`](../../app/services/inference/feature/store.py)），不引入第二种代次表达。
- **域隔离**来自分层第 1 期的 `_root.DOMAINS` 封闭白名单。

## 方案详情

### 全景：重启时没有人去"删"，是各域在"写"的时候顺手清掉上一代

```text
一次重启（同 task_id, step_id）

  stop_run  ─┬─ 残段入队（任务上携带旧 cq 作为代次标记）
             ├─ inference 停 workflow
             └─ CQ 出 registry
                        │
  start_run ─┬─ 建新 CQ  ＝ 新代次诞生
             └─ ⟨不再调用任何 purge⟩
                        │
  此后各域自己完成 supersede：
       hls      lane 线程执行本代次首个段写时，发现域内登记的 owner ≠ 本任务 owner
                → 先清空 {step}/hls/，再写                              §2
       features start_workflow 里的 open_fresh 认领 owner + 截断         §3（已有）
       lab      临时件用完即删，无 supersede 语义                        §4

  排在队列里的旧代次任务：出队时 owner 校验不符 → 丢弃，不写            §1
```

| 步骤 | 落在哪个模块 | 详见 |
|------|------------|------|
| 代次校验（乐观锁） | `recording` 的 lane 任务体 | §1 |
| hls 域首写自清 | `recording` 写者 + `storage.hls` 新增域内清理入口 | §2 |
| features supersede | `inference/feature/store.py`（已有，不动） | §3 |
| lab | 无（豁免） | §4 |
| TTL 回收（唯一保留的跨域删除） | `retention` | §5 |
| 防遗漏门禁 | `tests/` | §6 |

### 1. 代次校验：乐观锁，失败即丢弃、**不重试**

队列任务携带提交时的 owner（= 当时的 `cq` 对象引用）。消费线程执行前先比对该 `task_id`
当前注册的 CQ 是否仍是同一个对象，不是就直接丢弃并记 debug。

```text
出队 → owner is 当前 cq ? → 是：执行
                          → 否：丢弃（换代了，这批帧属于上一次 run）
```

**与标准乐观锁的唯一区别：失败动作是丢弃，不是重读重试。** 旧 run 的段在新 run 里没有任何
意义，重试就是把它硬塞进去。这一条要写进代码注释——否则后人看见「乐观锁」三个字会顺手补一个
重试循环。

代次判等**不要求 CQ 还活着**，只要求引用还在，所以 `cq.close()` 之后照样能判。也因此不需要
递增 id：这里只需要回答「是不是当前这一代」，判等就够，多一个 id 就多一处要同步的状态。

### 2. hls：本代次首写时清空本域

写者（lane 上的 `recording` 组件）持有 `{(task_id, step_id): owner}` 一张小表：

```text
执行段写任务：
    if 表中登记的 owner is not 本任务 owner:
        hls 域内清理（清空 {step}/hls/）
        登记本任务 owner
    insert_segment(...)
```

owner 表在 **service 侧**，不进 storage —— 分层规范 D4 明确把 owner fence 列为不可持有项。
storage 侧只需新增一个**域内清理入口**（`{step}/hls/` 的清空），放在 `hls` 域自己的模块里：
往某个域里删东西是那个域自己的事，与「定位归域文件」同理。

> ⚠ **这推翻了 [`tasks.py`](../../app/storage/tasks.py) 的一句话**：那里写着「本模块刻意不提供
> `purge_domain`：目前零需求，而『重启 supersede』与『TTL 到期』两个真实场景都是整 step
> 粒度」。前半句不再成立——**supersede 是域粒度，只有 TTL 是整 step 粒度**。`tasks.py` 仍然
> 不提供 `purge_domain`（跨域模块不该有域粒度能力），入口放在 `hls` 域内。

**为什么懒惰比 eager 好**（不只是"也可以"）：新 run 若一段都没写出来，旧产物原样保留，用户
还能回放上一次的录像。eager 删除会在这种情况下留下一个空 step。

### 3. features：不动

`open_fresh` 已经是「写者自己在 run 起始认领 owner + 截断」的形态，且跑在正确的线程（推理线程）
上。它与 §2 同构，只是时机更早（eager）——两者的共同点是**由写者在自己的线程里完成**，这才是
关键，eager/lazy 只是时机差异。

`facts.jsonl` 同域同理，随 `store.py` 一起不动。

### 4. lab：豁免

`{step}/lab/` 只放送标 clip 与整段导出的临时件，用完即删，没有「上一代产物」的概念。残留随
step TTL 回收。它在 §6 的门禁里进显式豁免清单，而不是被遗忘。

### 5. TTL 回收：全系统唯一保留的跨域删除

保持整 step 粒度（`tasks.purge_step`），由 `retention` 的定时线程执行，**不走 lane**——lane
承诺的是「同一个 step 内的顺序」，而 TTL 扫的是全部 task 全部 step，塞进去会让一次全盘扫描堵住
段写，换来一个本来就不需要的互斥。

两处必须一起改：

- **判据从 `metadata.json` 换成目录 mtime**（[`tasks.ids(order="mtime")`](../../app/storage/tasks.py)）。
  三个理由：① metadata.json 迁进 `{step}/hls/` 后现有的 `glob("*/*/metadata.json")` 一条都
  匹配不到——**不报错，只是再也不回收**；② 只有 `features.jsonl`、没有 HLS 段的 step 现在
  永远看不见，那正是当前泄漏的一类；③ TTL 是跨域策略，不该依赖任何单一域的产物。
- **并发对手要写进 docstring 而不是加机制**：TTL 确实可能撞上 lab 导出或回放读 15 天前的
  step，但那些是**读者不是写者**（lab 只写自己的临时件），最坏是一次 HTTP 500，不产生半写
  产物。换成 mtime 判据后还白捡一个缓解：lab 导出会写临时件、自动刷新目录 mtime，正在被反复
  导出的 step 天然续命。

### 6. 防遗漏门禁

`_root.DOMAINS` 已是封闭白名单。加一条测试：每个域要么提供域内清理入口，要么在显式豁免清单里。

```python
def test_every_domain_can_supersede_itself():
    """新增域时忘了处理 supersede → 这里失败，而不是等到某次重启后旧产物串进新 run。"""
```

与 `test_layer_package_imports_only_whitelisted_app_modules` 同款思路：**把静默错误换成测试
失败**。这是 #3 的正面回答——不是"记得去编排层补一行"，而是"新增域时测试会提醒你补自己的清理"。

## 变更效果

| | 改造前 | 改造后 |
|---|---|---|
| 编排层的 purge 调用点 | `start_run` 一处（整 step rmtree） | **零** |
| 跨线程删-写冲突 | hls / features / lab 三条都在（#1） | 消失——各域在自己的线程里清自己 |
| 全系统锁数量 | 规划中一把 per-`(task, step)` | **零**（`_locks.py` 已删，本篇确认不再需要） |
| 新 run 未产出时的旧录像 | 已被删 | 保留 |
| 新增域的遗漏风险 | 编排层漏一行 → 静默残留 | 测试失败 |
| 跨域删除 | supersede + TTL 两处 | 只剩 TTL 一处 |

顺序与隔离的分工最终落成两句话：**同代次内的顺序由提交序保证，跨代次的隔离由 owner 判等保证。**
队列解决不了换代，校验解决不了乱序，两条都要。

## 遗留风险 / 后续任务

| 项 | 说明 | 处置 |
|---|---|---|
| **本篇是提案，代码未动** | `start_run` 当前仍是整 step rmtree，#1/#2/#3 三个问题都还在 | 实施顺序见下 |
| `storage.hls` 缺域内清理入口 | §2 依赖它 | 随实施一并加，同时订正 `tasks.py` 那段「刻意不提供 purge_domain」的理由 |
| **TTL 的 glob 会在 metadata.json 迁移时静默失效** | 迁进 `{step}/hls/` 后 `glob("*/*/metadata.json")` 匹配为空，回收整个停摆且无任何日志 | 判据换 mtime 必须与 metadata.json 迁移在**同一次改动**里完成，不能分两次 |
| yaml 注释与代码不符 | [persistence_config.yaml](../../config/persistence_config.yaml) 写「status=completed 且超过此天数才删」，但 `_scan_and_clean` 只比对 `updated_at`，没有任何 status 检查 | 行为本身合理（卡死的任务也该回收），改注释 |
| 正在执行的旧代次任务 | 校验通过后、写完之前发生换代，这一段会先写完再被 §2 的首写清理删掉 | 正确，只是白写一次；不额外加机制 |
| owner 表的生命周期 | `{(task_id, step_id): owner}` 需随任务拆除回收，否则长跑慢泄漏 | 挂在 `recording` 的 task 拆除钩子上，论证同原 `release_dir_locks` |

**实施顺序**（与 [persistence 解体计划](20260909_STORAGE_LAYER_BASE.md) 的期次对齐）：

1. `hls.workers: 1` + ffmpeg `timeout` 15 s —— 单写者先就位，是本篇「提交序即执行序」的前提
2. 代次校验（§1）+ hls 首写自清（§2）+ 删掉 `start_run` 的 rmtree
3. TTL 判据换 mtime（§5）—— 与 metadata.json 迁移同批
4. 门禁测试（§6）
