# 推理子进程收尸：队列 feeder 线程卡死导致后端进程退不出去

> **变更状态**：生效中（2026-09-02）——`_kill_child` 原先只 `q.close()`，无法逼停阻塞中的 QueueFeederThread。一次子进程 wedge 就会留下一具永不退出的尸体线程，挂死整个后端进程的退出，须按两次 Ctrl-C 才能杀掉。
> **知识库**：待沉淀 → `kb/SERVICE_INFERENCE.md`（进程边界收尸契约）

## 概述

- **现象**：2026-09-01 19:15 关停，uvicorn 已打完 `Application shutdown complete` / `Finished server process [12719]`，进程却不退。操作者再按一次 Ctrl-C，拿到这个：

  ```
  Exception ignored in atexit callback: <function _exit_function at ...>
  Traceback (most recent call last):
    File ".../multiprocessing/util.py", line 357, in _exit_function
      _run_finalizers()
    File ".../multiprocessing/queues.py", line 199, in _finalize_join
      thread.join()
  KeyboardInterrupt
  ```

  同日 10:25 有一条前因：`[RemoteInferProxy] 子进程失败 dead=False wedged=True not_ready=False，清理在途并重启`。
- **改了什么**：[infer_proxy.py](../../app/services/inference/detection/infer_proxy.py) 的 `_kill_child`，队列收尸从「只 `close()`」改为「`req_q._reader.close()` → `cancel_join_thread()` → `close()`」；`_spawn_child` 建 `req_q` 时预置 `_ignore_epipe = True`。共 ~10 行。
- **影响面**：只动关停/重启路径的队列收尸。在线推理链路、进程边界数据格式、背压语义均不变。

---

## 一、进程 / 线程的启动关系

### 1.1 谁起了谁

```
后端主进程（uvicorn，非 daemon 主线程）
│
├── DetectionService.start()
│   └── RemoteInferProxy.start()
│       ├── _spawn_child()
│       │   ├── req_q  = ctx.Queue()   ← 主进程 put，子进程 get
│       │   ├── resp_q = ctx.Queue()   ← 子进程 put，主进程 get
│       │   └── ctx.Process(target=run_stages, name="InferChild", daemon=True)
│       │         └─▶ 【子进程 InferChild】
│       │              ├── 主循环：req_q.get → _infer_models → resp_q.put
│       │              └── QueueFeederThread(resp_q)   ← 子进程侧，daemon
│       ├── Thread("InferCollector", daemon=True)   → _collect_loop：resp_q.get → write_back
│       └── Thread("InferSupervisor", daemon=True)  → _supervise_loop：1s tick 看门狗
│
└── QueueFeederThread(req_q)   ← 主进程侧，**首次 put 时由 Queue 自己惰性拉起**，daemon
```

关键在最后一条：**`QueueFeederThread` 不是我们起的**，是 `multiprocessing.Queue` 在第一次 `put()` 时自己在 `_start_thread()` 里拉起来的。我们的代码里没有任何一行提到它，但它是这次故障的主角。

### 1.2 为什么 put 需要一个后台线程

`Queue.put(obj)` **不写管道**，它只做两件事：`self._buffer.append(obj)` + `notempty.notify()`，立刻返回。真正的 pickle + `os.write` 由 feeder 线程在 `Queue._feed` 里做：

```python
# CPython multiprocessing/queues.py  （节选）
def _feed(buffer, notempty, send_bytes, writelock, reader_close,
          writer_close, ignore_epipe, onerror, queue_sem):
    while 1:
        ...
        while 1:
            obj = bpopleft()
            if obj is sentinel:
                reader_close()     # ← 关 fd 只发生在这里
                writer_close()
                return
            obj = _ForkingPickler.dumps(obj)
            send_bytes(obj)        # ← 卡点：管道满就在这里阻塞
```

这个设计对小对象无所谓，但我们**过进程边界的是原始帧**：`submit()` 往 `req_q` 塞 `List[np.ndarray]`，一批 4 帧 = 3.7MB（本仓库帧恒为 640×480 BGR ≈ 0.92MB/帧，ffmpeg 解码即 `scale=640:480` 定死，见 [SERVICE_STREAM.md](../kb/SERVICE_STREAM.md)），而管道内核缓冲只有 64KB。也就是说 feeder 每送一批要在 `send_bytes` 里被唤醒/阻塞近 60 次，**全靠子进程持续读走才推得动**。

对称地，子进程回结果走 `resp_q.put((req_id, merged))` → **子进程自己的** feeder → 管道 → 父进程 `InferCollector` 的 `resp_q.get()`。注意 `get` 侧**没有线程**：`_recv_bytes()` 和 `loads()` 都在调用线程里同步做完。也就是说 **feeder 只存在于每条队列的写端那侧** —— `req_q` 的在父进程，`resp_q` 的在子进程，父进程里不存在 `resp_q` 的 feeder。

**故障因此是单向的**：resp 方向同样可能卡住 feeder（collector 停了没人读），但子进程随后被杀，尸体线程连同整个地址空间一起消失；而**父进程活下来**，只有它这侧的尸体会一代代累积。量级也差着数量级：req 是 `List[np.ndarray]`（MB 级，必然阻塞），resp 是检测结果 dict（KB 级，一次写完）。

> **`maxsize` 不约束内存**：它由 `_sem` 按**条数**卡（`put` acquire / `get` release），与字节数、与管道那 64KB 都无关。`maxsize=32` 时 32 条各带 4 帧照样能全躺在内存 buffer 里 —— 真正的内存上限是我们自己的 `max_inflight=8`。§3.5 的「28/32 帧」正是这么来的：7 条仍是 ndarray，第 8 条已被 `dumps` 成等大的字节副本（`obj = dumps(obj)` 重绑定，原 ndarray 引用释放）。

### 1.3 两条 fd 的归属（后面判 EPIPE 要用）

`Queue.__init__` 里 `self._reader, self._writer = connection.Pipe(duplex=False)` —— 两端都在**主进程**创建，spawn 之后子进程各拿一份副本。于是 `req_q` 的实际持有情况是：

| | 主进程 | 子进程 |
|---|---|---|
| `_writer` | **在用**（feeder 写） | 持有但不用 |
| `_reader` | **持有但不用** ← 就是它 | 在用（主循环读） |

主进程自己攥着一个从不使用的读端，这个「无用副本」是下面所有麻烦的根。

---

## 二、退出卡在哪里

### 2.1 故障如何形成（时间线）

```
10:25:23  子进程 wedge：进程还活着，但不再从 req_q 读
   ↓      （dead=False，所以 proc.is_alive() 判据不响）
10:25:23  主进程 feeder 卡进 send_bytes()，永远等一个不会来的读者
10:25:38  _supervise_loop 判 wedged=True（在途>0 且 15s 无响应）→ _handle_child_failure
   ↓      _kill_child(): terminate → join → kill → join → q.close()
   ↓      _spawn_child(): self._req_q = 新 Queue（旧 Queue 对象失去引用）
10:25:38  ★ 旧 feeder 线程仍卡在 send_bytes，且已成为一具无主的尸体
   ...    （之后 8 小时正常跑，任何日志都看不见它）
19:15:0x  Ctrl-C → uvicorn 干净收工 → 解释器开始退出 → 撞上尸体，挂死
```

### 2.2 精确卡点：`Py_FinalizeEx` 的第 3 步

解释器退出按顺序做三件事，我们的线程在前两步全部合法通过，死在第三步：

| # | 阶段 | 对本进程的实际效果 |
|---|------|-----------------|
| 1 | `threading._shutdown()` join 非 daemon 线程 | Collector / Supervisor / feeder **全是 daemon，直接跳过** |
| 2 | atexit → `util._exit_function` → `_run_finalizers(0)`（优先级 ≥0） | 跑 `Queue._finalize_close`（`exitpriority=10`）：往内存 buffer 追加哨兵 |
| 2' | `_exit_function` 里 terminate + join 所有 daemon 子进程 | 子进程早被杀干净，秒过 |
| 3 | `_run_finalizers()`（剩余全部，含负优先级） | 跑 `Queue._finalize_join`（`exitpriority=-5`）→ **`thread.join()`，无 timeout** ← **挂死在此** |

生产堆栈里的 `_run_finalizers → _finalize_join → thread.join()` 就是第 3 步。而那个 `KeyboardInterrupt` 不是故障本身，是操作者按的第二次 Ctrl-C 打断了这个 join。

> **补充：空闲的 feeder 不会挂。** 第 2 步发的哨兵能把它唤醒自退，第 3 步的 join 秒过。会挂的**只有卡在 `send_bytes` 里的那种**——它压根走不到取哨兵那行。

---

## 三、为什么回收不掉

四件事叠起来才挂，缺任何一件都不会出问题：

### 3.1 `Queue.close()` 唤不醒它 —— 哨兵送不到

`close()` 触发的 `_finalize_close` 只是往**内存 buffer** 追加 `_sentinel`。而 feeder 阻塞在 `send_bytes` 里，从 buffer 取下一个对象的循环根本没跑到。**唤醒信号发到了一个它正好读不到的地方。**

### 3.2 `close()` 也不关 fd —— EPIPE 也不来

看 §1.2 的源码：`reader_close()` / `writer_close()` 只出现在**哨兵分支里**。也就是说「关 fd」这件事被 CPython 委托给了 feeder 自己，而 feeder 正卡着 —— **循环依赖**：要关 fd 得先让 feeder 醒，要让 feeder 醒（靠 EPIPE）得先关 fd。

### 3.3 杀死子进程也不产生 EPIPE —— 主进程自己是那个读者

直觉上「子进程都杀了，写管道该 EPIPE 了吧」。不对：EPIPE 的条件是**管道再无任何读端持有者**，而按 §1.3，主进程自己还攥着一份 `req_q._reader`。

这一条有直接实证（`tmp/repro_feeder_stack.py`）：

```
child alive after kill: False      ← 子进程已确认死亡
close() 已返回；3s 后 dump 全部线程栈
Thread 0x…  QueueFeederThread:
  File ".../connection.py", line 373 in _send      ← 写操作依然阻塞
```

### 3.4 join 终结器是**进程级**的，宿主还是线程不是队列

最后一根钉子。`_start_thread` 注册的是：

```python
self._jointhread = Finalize(self._thread, Queue._finalize_join,       # 宿主 = 线程
                            [weakref.ref(self._thread)], exitpriority=-5)
self._close     = Finalize(self, Queue._finalize_close,               # 宿主 = 队列
                           [self._buffer, self._notempty], exitpriority=10)
```

`_spawn_child` 用新队列覆盖 `self._req_q` 之后，旧 Queue 对象确实被 GC 了 —— 但被 GC 的是 `_close` 那个（宿主是队列）。`_jointhread` 的宿主是**线程**，线程还活着，于是它安然留在模块级强引用字典 `util._finalizer_registry` 里，**一直躺到解释器退出**。

> **所以「关停时没有活跃任务」完全不影响判断。** 挂死的是 8 小时前那次 wedge 留下的尸体，与关停时刻有没有流量无关。

### 3.5 附带代价：内存也回收不掉

`buffer` 是**按参数**传进 `Queue._feed` 的（见 §1.2 签名），被卡死线程的栈帧强引用。旧队列对象被 GC 带不走它 —— 泄的是**整个 deque**，不是正在写的那一帧。

实测（`tmp/repro_feeder_leak.py`，weakref 存活计数）。**注意脚本用 1080p 帧（6.2MB/帧）刻意放大信号**，生产帧是 640×480（0.92MB/帧），故绝对值按 1/6.7 折算：

| 收尸方式 | 滞留帧数 | 脚本口径（1080p） | 生产口径（640×480） |
|---------|---------|------------------|--------------------|
| 修复前 | **28/32 帧** | ≈166MB + 约 25MB 已 pickle 的字节副本 | ≈26MB + 约 3.7MB 字节副本 |
| 修复后 | **0/32 帧** | 0 | 0 |

泄漏**比例**（28/32 帧全滞留）与帧尺寸无关，是本节的结论；MB 数只是标度。

生产单次上限 = `max_inflight(8) × batch_size(4) × 0.92MB ≈ 29MB`。

> **观测陷阱**：`_handle_child_failure` 打的 `丢弃在途 N 帧` 清的只是 `_pending` 里的轻量元数据（`_Pending` 按设计不含 ndarray），像素还在 feeder 手里 —— **日志说「丢了」，内存里没丢**。
>
> **风险形状**：泄漏速率 = wedge 频率 × 当时在途量，而 `max_restarts=None`、退避封顶 30s。偶发时无害（本次事件在途仅 1 帧 ≈ 0.92MB，6 次累计 <6MB，所以长期没被发现）；一旦 wedge 转成连发（30s 退避 → 2 次/min × 29MB）就是 ~58MB/min 的单调增长，且 OOM 之前先表现为推理全线停摆，两个症状叠在一起极难归因到队列 feeder。**注意这是「不封顶的单调泄漏」而非「一次性占用」——量级不大不改变必须修的结论，只是把 OOM 时间从小时级推到天级。**

---

## 四、回收方案

### 4.1 `_kill_child` 三步收尸

```python
if self._req_q is not None:
    try:
        self._req_q._reader.close()      # ① 断读端 → 阻塞的写立刻 EPIPE
    except Exception:
        pass
for q in (self._req_q, self._resp_q):
    try:
        if q is not None:
            q.cancel_join_thread()       # ② 注销无超时的 thread.join 终结器
            q.close()                    # ③ 原有动作，保留
    except Exception:
        pass
```

对应上一节：

| 步骤 | 破掉的死结 | 换来的 |
|------|-----------|-------|
| ① `_reader.close()` | §3.2 + §3.3 —— 由**主进程主动**关掉自己那份无用读端，不再等 feeder 关 | feeder 拿到 `BrokenPipeError`，真正返回，**连带释放整个在途 buffer**（生产最坏 29MB，且每次 wedge 累加） |
| ② `cancel_join_thread()` | §3.4 —— `Finalize.cancel()` 把 `_jointhread` 从 `_finalizer_registry` 摘掉 | 即便 ① 失效，尸体也只是个被遗弃的 daemon 线程，不再拖住退出 |
| ③ `close()` | （原有）常规路径 | 空闲 feeder 走哨兵正常自退 |

**① 只对 `req_q` 做**：`resp_q` 的读端是本进程 collector 在用的，不能关。这一步等价于 CPython 自己的 `Queue._terminate_broken`（gh-94777 / gh-107219）。

**② 撤掉的是 flush 承诺，不是「回收线程的能力」**。`join_thread()` 的用途是**保证 buffer 里剩下的数据全部写进管道**再让 feeder 退出——没有它，进程可能带着没写出去的数据退出、静默丢数据。`_jointhread` 这个 atexit finalizer 就是把这条保证设成**默认**：你从不调 `join_thread()`，解释器退出时也替你做。**CPython 在此选了「不丢数据」优先于「保证能退出」**，代价正是那个无超时 join；`cancel_join_thread()` 是官方逃生口（文档明写：允许立即退出，代价是入队数据可能丢失）。

对我们这条路径，flush 保证的是**一批已经判死的帧**：`_kill_child` 跑到时，要消费它的子进程正在被杀，`_handle_child_failure` 早把这些帧计进了 `frame_drop_total{reason="infer_child_restart"}`（健康 `stop()` 路径同理，drain 后剩的按 `infer_child_down` 计）。把它们写进一根即将没有读者的管道——零价值，却要付一个无上限的阻塞。

**② 也是 ① 的兜底**：子进程真杀不死时（CUDA D 态；注意 `proc.join(timeout=2.0)` 超时**不抛异常**、代码会静默往下走），子进程那份读端仍开着，EPIPE 依然不来 —— 这时只有 ② 能救。**①②分工**：① 让 feeder 真的退出（成功时压根没有 join 可做）；② 让「它没退出」从**致命**降级为**可容忍**。

**为什么不能只用 ②**：只撤 join 能治退出挂死，但 feeder 仍卡在 `send_bytes` 上攥着整个在途 buffer，且随 wedge 次数累加。**必须①+②**：① 管内存，② 管退出。

**关于两个半私有口子**：`_reader` 无公开等价物（3.12 才有 `_terminate_broken`，且仍是私有）；`cancel_join_thread` 本就是公开 API。三处全裹 `try/except`，属性哪天没了也只是退回旧行为，不会引入新故障模式。

### 4.2 `_spawn_child` — `req_q._ignore_epipe = True`

断读端后 feeder 拿到 `BrokenPipeError`，默认走 `_on_queue_feeder_error` → `traceback.print_exc()`，每次收尸往 stderr 打一坨假故障。置位后 feeder 静默返回。

**必须在首次 `put` 之前置位** —— 它是在 `_start_thread` 里**按值**传给 `_feed` 的，线程起来之后再改无效。同 CPython 的 `ProcessPoolExecutor` 对 `_call_queue` 的做法。

### 4.3 为什么不干脆在 spawn 之后就把读端关掉（已评估，本次不做）

既然主进程那份 `req_q._reader` 从头到尾没用过（§1.3），为什么不在 `_spawn_child` 里 `proc.start()` 之后立刻关掉？那样 EPIPE 就变成子进程一死自动发生，`_kill_child` 的 ① 都不需要。

- **`start()` 之前不能关**：spawn 传 fd 是在 `proc.start()` 内部完成的（POSIX 走 `popen_spawn_posix.duplicate_for_child` 把 fd 塞进 `spawnv_passfds` 的传递表，Win32 走 `CreateProcess` 之后的 `DuplicateHandle`），那一刻 fd 必须是开的。
- **`start()` 之后可以关，CPython 只是不能替我们决定**：`Queue` 契约是对称的 —— 任何一端都可 `get`/`put`，同一队列还可能被之后才 spawn 的 worker 继承（进程池的常规用法）。父进程关读端会同时废掉这两种用法。我们是「单向 + 一队列一子进程」（`_spawn_child` 每代建新 Queue），所以我们能关。

实测可行（`tmp/repro_close_reader_at_spawn.py`）：`start()` 后立刻关读端，子进程 3 批全收到（不影响正常通信）；子进程被 `terminate` 后，**不做任何 `_kill_child` 特殊处理**，feeder 自己 EPIPE 退出。

| | 现方案（收尸时断读端） | spawn 后立刻断读端 |
|---|---|---|
| 生效时机 | 只在 `_kill_child` 跑到时 | 子进程任何死法（crash / OOM-kill / terminate）都自动 |
| 不变式性质 | 程序性：每条 teardown 路径都要记得断 | 结构性：fd 表强制「父进程在 req_q 上只写」 |
| `_reader` 这个半私有口子的爆炸半径 | 失败路径 | **spawn 路径（每次都跑）**，坏了是起不来而非收不干净 |
| 能否替掉 ② `cancel_join_thread()` | — | **不能**：子进程真杀不死（CUDA D 态）时仍攥着读端，EPIPE 依旧不来 |

**结论：本次不做。** 今天只有一条 teardown 路径，结构性不变式的边际收益小，而风险从失败路径挪到了必经路径；它也替不掉 ②，只是多一道保险。等出现第二条 teardown 路径（如 graceful restart）再上。

### 4.4 为什么不是教科书的 `close() + join_thread()`

回收 `QueueFeederThread` 的标准药方是 `q.close(); q.join_thread()`。这里**故意不用**：`join_thread()` 就是本次挂死我们的那个无超时 join（`_finalize_join`）。在 `_kill_child` 里调它，等于把同一个死锁从「解释器退出」挪到 `stop()` 里 —— 而且更糟：uvicorn 连 `Application shutdown complete` 都打不出来，现象从「按两次 Ctrl-C」退化成「服务卡在停止中」。

两条路径的正确形状本就不同，而 `_kill_child` 同时服务这两条（`stop()` 第 3 步也调它）：

| 路径 | feeder 状态 | 正确做法 |
|------|-----------|---------|
| 健康停机 | 空闲，能收到哨兵 | `close()` 即可自退；`join_thread()` 只是等它，可有可无 |
| wedge 收尸 | 卡在 `send_bytes`，哨兵送不到（§3.1） | **必须先断管**，等是等不到的 |

①断管 + ②撤 join 对两条路径同时成立，且比 `join_thread()` **保证更强**：强制它返回，而不是等它返回。

> `resp_q` 在父进程里**没有 feeder 线程** —— 父进程从不 `put` resp_q，`_start_thread` 没跑过，`_jointhread is None`。`cancel_join_thread()` 里的 `except AttributeError` 正是接这个。每代只有 `req_q` 一个 feeder。

---

## 五、验证

复现脚本 `tmp/repro_queue_exit_hang.py`（未入库）：spawn 一个只 `sleep` 不读队列的子进程模拟 wedge → put 3 批 12MB 帧灌满管道 → `terminate` → 按修复前/后两种方式收尸。（脚本用 1080p 帧灌管道，只为快速把 64KB 缓冲填满、缩短复现时间；生产帧是 640×480，触发条件相同——任何 >64KB 的 payload 都会让 feeder 阻塞在 `send_bytes`。）

| 模式 | 收尸方式 | 结果 |
|------|---------|------|
| 修复前 | 只 `q.close()` | 主逻辑打完收工日志后**永久挂死**，12s 看门狗 SIGKILL（rc=137） |
| 修复后 | `_reader.close()` + `cancel_join_thread()` + `close()` | **0.68s 干净退出**（rc=0），stderr 无 traceback |

`faulthandler` 打出的挂死现场与生产堆栈逐帧一致：feeder 停在 `connection.py:_send`，主线程停在 `_exit_function → _run_finalizers → _finalize_join → thread.join`。

内存见 §3.5 表。全量 `pytest tests/` 444 passed（含 `tests/test_infer_proxy.py`）。

> 测法备注：内存判据用 weakref 存活计数而非 RSS —— macOS 的内存压缩会把 `np.full` 那种常量页压没，RSS 在这题上不可信；且脚本里必须 `del q` 模拟 `_spawn_child` 覆盖旧队列，否则测的是脚本自己的局部变量。

## 遗留

- **wedge 本身没查**：2026-09-01 10:25:38 那次 `dead=False wedged=True` 的成因（CUDA 卡死 / 前向超 15s / 子进程被信号打断）本次未定位，只修了它的收尸后果。若复发需确认重启后是否真恢复（`子进程就绪 pid=` 是否跟上），以及被 wedge 的旧子进程有没有真死透、显存有没有回收。
- **杀不死的子进程完全静默**：`proc.join(timeout=2.0)` 超时后代码不抛不记，直接往下走。建议下次动这块时在 kill/join 之后补一条 `if proc.is_alive(): logger.critical(...)`，把「SIGKILL 后仍存活、显存不会释放」这种情况变可见。
- **没有验证 feeder 真的退了**（同上一条的同源缺口）：①断管、②撤 join 之后，我们从不回头确认那个线程死了。子进程杀不死时 EPIPE 不来，②只保证进程能退出，feeder 会带着整个在途 buffer 静默活到进程结束 —— 正是「unjoined QueueFeederThread」这个经典症状，而目前零日志。补一个**有界** join 即可变可见（1s 封顶，不会重现无超时 join 的死锁）：

  ```python
  feeder = getattr(self._req_q, "_thread", None)   # 队列从未 put 过时为 None
  if feeder is not None and feeder.is_alive():
      feeder.join(timeout=1.0)
      if feeder.is_alive():
          logger.critical("[RemoteInferProxy] req_q feeder 断管后仍存活，"
                          "子进程可能未真死（CUDA D 态），在途帧内存不会释放")
  ```
- `max_restarts=None`（无上限重启）本次未动 —— 对自愈服务是对的，真要限也该配套告警，属另一个决定。

## 订正（2026-09-03）

初版全文按 **1080p（6.2MB/帧）** 估算内存，来源是复现脚本 `tmp/repro_*.py` 里刻意放大的帧尺寸，误当成了生产参数。**本仓库不存在 1080p 帧**：ffmpeg 解码命令写死 `scale=640:480`（[decoder.py](../../app/services/stream/decoder.py) `_build_cmd`，取 `DecoderConfig.default_width/height`），任何源进系统第一步就被缩到 640×480 BGR ≈ **0.92MB/帧**，全链路（含离线 HLS 取帧）无第二种尺寸。

已改：§1.2 一批 4 帧 24MB→**3.7MB**、唤醒 400 次→**60 次**；§3.5 泄漏上限 190MB→**29MB**、连发速率 380MB/min→**58MB/min**，并把脚本口径与生产口径拆成两列；§4.1/§4.2 的 166MB 改为不写死数字。**所有结论与修复方案不变**——本题的判据是「payload > 64KB 管道缓冲」和「滞留比例 28/32」，两者都与帧尺寸无关。

> `config/client_config.yaml` 的 `resize_width/height` 是 dead config（`ClientQueues` 只存不用，全仓无消费者），别拿它当缩放依据。
