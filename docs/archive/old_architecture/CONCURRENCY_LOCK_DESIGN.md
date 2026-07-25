# 并发锁设计规范

本文总结在 CleanSightBackend 纯 `threading` 架构下沉淀的锁设计原则，可直接复用于其他模块。

---

## 基础方法论：自底向上构建线程安全性

**先保证每个基础组件的单个操作是线程安全的，上层再线程安全地组合这些操作。**

这是整套设计的前提。具体到本系统的三层结构：

```
ClientQueues（底层）
  每个方法（set_task / append_ca_raw / get_task_id / …）
  自身是线程安全的原子操作，调用方无需关心其内部竞争

InferenceManager / ClientManager（中层）
  可以放心调用底层方法，无需重复保护单个字段
  只需为"多个底层操作的组合序列"添加额外同步
  例：cq.set_task() 和 client_manager.bind_task() 各自安全，
      但两步之间的窗口期需要 _client_locks 保护

API 层（上层）
  用 asyncio.Lock 序列化跨多个服务的完整业务事务
  不关心底层字段细节，只关心事务边界
```

推论：**上层的锁只需覆盖"组合语义"，不需要重新保护底层已经安全的单个操作。** 如果上层还在为底层字段加锁，说明底层封装不够。

对应地，**底层组件的方法不应依赖调用方已持有某把外部锁**，否则线程安全性变成隐式合约，难以维护。

---

## 核心思路：按业务访问模式分锁

**不要按资源分锁，要按访问模式分锁。**

粗放的做法是"每个字段一把锁"或"一个类一把大锁"。正确的起点是问：**谁和谁总是被同一个业务动作一起访问？** 把同一个业务动作总是一起读写的字段归入同一把锁。

```
反例：
  _rendered_lock  → 只保护 _latest_rendered
  _processed_lock → 只保护 ca_processed
  两把锁，VizWorker 每帧要加锁两次

正例：
  _viz_lock → ca_processed + _latest_rendered
  VizWorker 对同一帧连续写两者，一次加锁，天然原子
```

---

## 原则一：识别 SPSC，消除不必要的锁

Single Producer Single Consumer（单写单读）模式下，CPython GIL 已保证 `deque.append` / `popleft` 的原子性，不需要显式锁。

**判断条件：**
- 只有一个线程写
- 只有一个线程读
- 两者角色固定、不会动态变化

**示例：** `ca_ready` deque — decoder 线程唯一写，dispatcher 线程唯一读，无锁。

能证明不需要锁的地方，就不加。锁的成本不仅是性能，还有认知负担。

---

## 原则二：快照模式，避免锁嵌套

热路径需要读多个受不同锁保护的字段时，在进入"重锁"之前先用"轻锁"快照依赖值到局部变量，之后只用局部变量。两把锁的生命周期完全不重叠，彻底消除嵌套死锁风险。

```python
# 反例：在 _raw_lock 内获取 _task_lock，形成嵌套
with self._raw_lock:
    if self.task is not None:           # 读 self.task（需要 _task_lock）
        task_id = self.task.task_id     # TOCTOU：check 与 use 之间 task 可能被置 None

# 正例：进入 _raw_lock 前先快照
_task = self.get_task()                 # 在 _task_lock 下快照，已释放
with self._raw_lock:
    if _task is not None:               # 用局部变量，无锁嵌套
        frames_to_persist = ...
```

此模式同时修复了 TOCTOU（Time Of Check To Time Of Use）竞争条件。

---

## 原则三：固定全清顺序，防死锁

需要同时持有多把锁（如 `clear()` 全量原子清除）时，死锁的充要条件是**不同路径以不同顺序获取同一组锁**。

解法：在类文档中声明唯一的全清顺序，所有路径严格遵守。

```python
# 在类 docstring 中声明：
# 全清顺序：_task_lock → _raw_lock → _viz_lock → _inference_lock
#           → _frontend_lock → _slide_window_lock → _alarm_lock

def clear(self) -> None:
    locks = [
        self._task_lock, self._raw_lock, self._viz_lock,
        self._inference_lock, self._frontend_lock,
        self._slide_window_lock, self._alarm_lock,
    ]
    with contextlib.ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        # 在全部锁保护下执行清除
        ...
```

`contextlib.ExitStack` 保证按顺序加锁、逆序释放，且异常时也正确释放。

---

## 原则四：同一临界区内读取关联值，防止窗口期数据丢失

若两个值之间存在不变式（invariant），必须在同一次加锁内同时读取。分两次加锁会在中间窗口破坏不变式。

**典型场景：游标 + 增量列表**

```python
# 反例：两次加锁，中间窗口新告警 seq=N+1 写入
alarms = self.get_alarm_increment(since_seq)  # 释放锁
max_seq = self.get_alarm_max_seq()            # 重新加锁
# 客户端以 since_seq=N+1 发下次请求，seq=N+1 的告警永久消失

# 正例：一次加锁，保证 max_seq >= max(a.seq for a in alarms)
with self._alarm_lock:
    alarms = [a for a in self._alarm_log if a.seq > since_seq]
    max_seq = self._alarm_seq
```

---

## 原则五：把"字段赋值"和"业务逻辑"分离到不同方法

一个 setter 方法应该只做赋值，不携带副作用（缓存清理、事件触发等）。原因：

1. 调用方可能需要在"赋值之前"完成某些旧状态相关的操作
2. 混入副作用的 setter 线程安全语义不明确
3. 调用顺序的正确性难以在阅读代码时直接看出

```python
# 反例：赋值 + 清缓存混在一起
def set_task(self, task):
    self.task = task
    self._alarm_log.clear()   # 副作用，调用方无法控制时机

# 正例：分离
def set_task(self, task):          # 纯字段赋值
    with self._task_lock:
        self.task = task

def clear_task_caches(self):       # 显式清缓存，合约写在文档里
    """调用方须确保旧 Actor 已停止。"""
    ...
```

合约（precondition）写在 `clear_task_caches()` 的 docstring 里，调用方明确知道自己的责任。

---

## 原则六：状态机转换的正确顺序——先退出旧状态，再进入新状态

涉及"停旧启新"的生命周期切换时，必须严格按顺序：

```
1. finalize_and_stop(old)   旧状态完整退出，写入其上下文相关的数据
2. set_task(new_task)        切换字段
3. clear_task_caches()       清空旧状态遗留数据（此时旧 Actor 已停，安全）
4. start(new)                启动新状态
```

反过来（先 `set_task` 再 `finalize_and_stop`）会导致旧 Actor 的结算数据写入已切换的新 task 上下文，数据归属错误。

---

## 原则七：按业务层级分锁，纵深防御

不同调用来源（HTTP 请求、后台监控线程）对同一资源的并发访问，不能靠单一层次的锁解决。

| 层 | 锁类型 | 保护范围 | 防御对象 |
|---|---|---|---|
| HTTP API 层 | `asyncio.Lock` per client | 序列化并发 HTTP 请求 | 前端重复点击、网络重试 |
| 服务层 | `threading.Lock` per client | 业务事务（Actor 状态机） | 后台线程（health monitor）与 API 线程的竞争 |
| 数据层 | 细粒度 `threading.Lock` | 各数据结构读写 | 推理/可视化/前端读取的并发 |

**关键点：** `asyncio.Lock` 只在 event loop 的协程之间有效，对独立的 `threading.Thread`（如 health monitor）没有任何约束力。服务层的 `threading.Lock` 是必要的纵深防线，不是冗余。

---

## 原则八：幂等语义精确化

幂等检查要精确到"完全相同"，不能只检查主键。

```python
# 反例：只检查 task_id，忽略 current_step 和 URL 变化
if old_task_id == req.task_id and stream_running:
    return idempotent_response   # current_step 变了也返回这里，stage 不更新

# 正例：三者完全相同才幂等，否则全量重建
if (old_task_id == req.task_id
        and old_task.current_step == str(db_task.current_step)
        and stream_url == req.rtsp_url):
    return idempotent_response
# 否则：全量清理 + 重建，不做部分更新
```

部分更新（只更新变化的字段）看似高效，实则引入"哪些状态需要同步跟进"的边界复杂度。对低频的生命周期操作，**全量重建的简单性远比节省资源重要**。

---

## 锁文档模板

每个有锁的类应在 docstring 里维护锁清单：

```
锁清单（Lock Inventory）：
  _task_lock        Lock   self.task + self.task_started_at（读多写少共享资源）
  ca_ready          无锁   SPSC deque：单生产者 / 单消费者，GIL 保证原子性
  _raw_lock         Lock   ca_raw + latest_raw_frame + latest_raw_timestamp
  ...

全清顺序（clear() 同时持锁时的固定顺序，防死锁）：
  _task_lock → _raw_lock → _viz_lock → ...

热路径读共享字段模式：进入 frame lock 前先快照，两把锁永不嵌套。
```

这份清单是可执行的规范，而不是注释：`grep` 锁名即可验证代码与文档是否一致。

---

## 快速检查清单

设计或 review 并发代码时逐项检查：

- [ ] 底层组件的每个公共方法是否自身线程安全，不依赖外部锁？
- [ ] 上层是否只为"多步组合"加锁，而非重复保护底层已安全的字段？
- [ ] 同一业务动作一起访问的字段是否归入同一把锁？
- [ ] 是否有 SPSC 模式被错误加锁？
- [ ] 热路径是否存在锁嵌套？能否用快照模式消除？
- [ ] 多锁场景是否声明并遵守固定获取顺序？
- [ ] 关联值（如游标+列表）是否在同一临界区内读取？
- [ ] setter 是否混入了副作用？
- [ ] 状态机转换是否先退出旧状态？
- [ ] 不同调用来源（async/thread）是否有各自对应的锁层？
- [ ] 幂等检查是否覆盖了所有会影响系统状态的字段？
