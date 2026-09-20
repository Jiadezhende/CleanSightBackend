# 进重连时切出残帧段：让断流空洞落到段边界，消灭 3× 慢放

> **变更状态**：已实现（2026-09-20）　**验收**：真实断流未跑，判据见文末
> **选型依据**：[20260919_VIDEO_TIMEBASE_SELECTION.md](20260919_VIDEO_TIMEBASE_SELECTION.md) §3.2
> **知识库**：待沉淀

## 修的是什么

同一 run 内 RTSP 断流重连**不拆除 CQ、不清队列**。`SegmentSweeper` 只拉攒满 300 帧的整段，
残帧只在拆除时收尾。于是断流那刻攒在 CA 队列里的半批帧留在原地、被重连后的帧补满，拼成一个
横跨 gap 的段——而 `eff_fps` 由首末帧跨度反推，跨度里混进了整段 gap：

```text
断流前已攒   eff_fps    EXTINF     真实内容    慢放     墙钟跨度
    1 帧      9.99      30.03s     10.00s    3.00x    29.93s
  299 帧      9.99      30.03s     10.00s    3.00x    29.93s
  无断流      30.00     10.00s     10.00s    1.00x     9.97s
```

10 秒画面被写成 30 秒，回放 / 导出 / 送标三条链路一起中招；`eff_fps=9.99` 仍落在合理带
`[1,60]` 内不触发退化兜底，**全程无一条报警**。每次重连必现。

### 5.1 进重连时切出残帧段（修缺陷 #4：跨 gap 段 3× 慢放）

| 文件 | 改了什么 |
|---|---|
| [`health_monitor/manager.py`](../../app/services/health_monitor/manager.py) | `_enter_reconnect_mode` 在写入 `_reconnecting_clients` **之后**调 `recording_service.request_residual_flush(cq, fence_ts=last_frame_time)`；新增 `recording_service` 协作者（第 5 个，函数体内 import） |
| [`recording/service.py`](../../app/services/recording/service.py) | 新增 `request_residual_flush` / `take_pending_flush` 与 `_pending_flush` 表；`flush_residual(cq, until_ts=None)` 加参数；`_forget` 顺带回收挂起请求 |
| [`recording/_sweeper.py`](../../app/services/recording/_sweeper.py) | 收敛成纯节拍器：`_sweep` 只对每个活跃 CQ 调一次 `service.collect_from`（见下） |
| [`client/queues.py`](../../app/services/client/queues.py) | `drain_ca_raw` / `drain_ca_processed` 加 `until_ts`，只弹队首满足栅栏的连续前缀 |

#### 为什么运行期取帧是 PULL

分段判定与落盘触发是**录制的职责**，`ClientQueues` 该退回纯缓冲容器。历史上
`append_ca_*` 里直接调落盘，等于让缓冲区知道存储的事——换成 PULL 之后，CQ 只管存，
什么时候取、取多少、按什么顺序取全在 recording。

配套的边界：`_sweeper` 只是**节拍器**（每 interval 对每个活跃 CQ 调一次
`collect_from`），取什么、按什么顺序取在 `RecordingService`。这条边界的理由见下方
「接口收敛」。

**三条与初稿不同的决定**（初稿那版会静默出错）：

1. **flush 由 sweeper 线程执行，不在 health_monitor 线程就地做。** 重连期 CQ 仍在注册表里，
   sweeper 还在扫它 → 两个 drain 者。两次 drain 各自被 CQ 锁保护、帧不重不漏，但
   `submit_segment` 发生在锁外，**入队序一乱 tfdt 就乱**（每段 tfdt = 执行时读到的累计
   EXTINF），清单 ts 不再升序、读侧 bisect 失效。**初稿写的"走提交队列就安全"不成立**
   ——队列只保证执行序 = 入队序。
2. **用时间戳栅栏而不是帧数上限。** 断流判据是 **decoder 子进程死活**
   （[`manager.py:221`](../../app/services/health_monitor/manager.py#L221)，`check_interval`
   1.0s），不是初稿以为的 `heartbeat_timeout` 5s——检测很快，respawn 也可能在 1s 内恢复推帧，
   到 sweeper 下一 tick 时队列里已混进新帧。`fence_ts = last_frame_time` 与调用时机、与此前
   拉走了多少整段都无关。
3. **先拉整段、后拉残段**，顺序反了清单 ts 就逆序。

**接口收敛（2026-09-20，评审后）**：这套取帧逻辑一度写在 `_sweeper._sweep` 里，于是定时器
要调 `submit_segment` / `take_pending_flush` / `flush_residual` 三个方法，连带把上面第 3 条
顺序不变式也变成了定时器里的一句注释——而它成立的理由（`tfdt` = 执行时读到的累计 EXTINF）
整个是 recording 的事。现已塌成 `RecordingService.collect_from(cq)` 一个入口：

```text
sweeper 知道的东西     3 个方法 + 顺序协议 + step_id 判据   →   1 个方法
take_pending_flush     公开（只有 sweeper 调）              →   私有 _take_pending_flush
service 对外方法数     7                                    →   6
顺序不变式的用例       挂在 sweeper 测试类                  →   回到 service 测试类
```

定时器只剩「每隔 1 秒，对每个活跃 CQ 调一次」。`recording` 包外零调用点受影响
（`submit_segment` / `take_pending_flush` 本就没有外部调用者，`flush_residual` 仍由
`run_control` 在拆除期直接调）。


---

## 验收发现（三路并行 review）

**`_forget` 无差别清 `_pending_flush`，会吞掉新一代的挂起请求。** `forget_task` 是异步的
（只往队列里排任务，前面可能堆着几秒的编码/转码），这期间同一 task 完全可能已经起了新一代
并登记了同键的请求。`_claimed_by` 能无差别清是因为它自愈（下一代首写会重新认领），挂起请求
是一次性的、不自愈——两者不能套同一个论证，而初版就是套了。后果：那次断流的残帧不被切出，
**长回本改动要消灭的慢放段，且静默**。已加对象身份核对。

同一段还有第二处：对 dict 的列表推导**不是原子的**，health_monitor 此刻可能正在
`__setitem__` → `RuntimeError: dictionary changed size during iteration`，异常被队列吞成
一条 error 日志，后面的代次回收被跳过。已改成先 `list()` 快照。

**并发论证本身也是错的。** 字段注释原写"SPSC，health_monitor 写、sweeper 读"，实际有
**三个**线程碰它（第三个是队列线程的 `_forget`），而漏数的那个恰好就是上面缺陷的所在。
免锁的结论侥幸成立，论证不成立——已改成按「`dict` 单次操作原子、逐元素迭代不原子」陈述。

**测试缺口**：顺序不变式原先只在 raw 轨被钉住，把残帧那步挪到两个 `while` 之间照样绿，
而那样 processed 清单就逆序了。已补两轨断言。

---

## 已知残留

| 项 | 说明 |
|---|---|
| **短于 decoder 进程退出阈值的停顿仍被吞进段内** | 网络抖动 / 解码卡顿造成的亚秒～数秒停顿不触发重连、不 flush，该段轻微慢放。量级从 20s 降到数秒，没有消失 |
| **processed 轨的 flush 只在"残帧不足一整段"时完整** | processed 由 viz worker 按 tick 渲染，ts 落后 raw 一个推理管线延迟。延迟内积压超过 `ca_segment_len` 时，sweeper 会先把跨 gap 的那批当整段拉走，第二次 flush 捞不回来。已加"重连成功时用同一栅栏再登记一次"覆盖常见情形；积压超一整段的不修——processed 是参考轨，送标走 raw |
| **拆除期的直接 flush 与 sweeper 的 pending flush 可并发** | `stop_run` 在控制面线程直接调 `flush_residual`，此时 CQ 还在注册表里。窗口 = 断流登记与 `/stop` 落在同一个 sweeper tick 内（~1s），且断流→cleanup 隔着 `cleanup_timeout=20s`。**既有状态，本次未加重** |
| 流抖动时连出若干短段，单帧段撞退化兜底（`eff_fps=15.0`、`EXTINF=0.067s`） | 该段自洽（tfdt/mdhd/EXTINF 一致、播放不出洞），失真被限制在段内部 |

---

## 验收判据（**欠着，必须由人跑**）

本改动的时机只做了代码路径推导 + 单测（"给了栅栏就只切栅栏之前的帧"），**没跑过真实断流**：

> dev 环境接真实 RTSP 跑一个任务，拔流或 kill decoder 子进程制造一次断流，看
> `[storage.hls] 段已落盘` 日志——重连前后两段的 `fps=` 都应回到 ~raw_fps，中间多出一个
> `frames<300` 的短段。若某一段的 `fps` 仍被拉低，说明栅栏没拦住重连后的帧。

端到端会写库、发告警，**跑之前先跟人确认**。
