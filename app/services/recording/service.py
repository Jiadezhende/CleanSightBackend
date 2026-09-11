"""录制服务 —— 把 CQ 里攒好的帧变成盘上一个可播的 HLS 段。

对外只有 5 个方法，**四个以 `cq` 为入参**（task_id / step_id / 代次身份都在它身上）：

    start() / stop()                          生命周期
    submit_segment(cq, track, frames) -> bool 打包 + 入队
    flush_residual(cq)                        拆除期把 CQ 里不足一段的残帧切完落盘
    forget_task(task_id) -> bool              代次表回收

格式怎么落盘不在这里——那全在 `app.storage.hls`（`insert_segment` 一次调用吞下编码、转码、
tfdt 修补、sidecar、init、清单、统计）。本模块只回答三个**编排**问题：

    什么时候拉       `_sweeper` 周期扫活跃 CQ，把攒满的整段拉走
    按什么顺序写     一条 SerialTaskQueue，提交序即执行序
    哪一代的产物     出队时比对代次（乐观锁），并在本代次首写时清掉上一代

## 顺序与隔离是两件事，两条机制各管一件

    同代次内的顺序   由提交序保证        —— 但队列解决不了换代
    跨代次的隔离     由代次判等保证      —— 但校验解决不了乱序

## 零锁：**本模块一把锁都没有**（`app/storage/_locks.py` 也已删）

互斥是"根本没有第二个线程"，不是抢出来的：段写、删目录、改代次表**全部跑在队列那一个
消费线程上**。对外的方法只做两件事——从 `cq` 上取值、往队列里排任务——都不碰共享状态。

这条不变式的护栏就一句：**队列不能加 worker**。加了不报错，只是 tfdt 开始碰撞、代次表
被更新的一代抢先占用、旧段串进新 run，全是静默的。配置里因此没有 `workers` 这个旋钮。

## 依赖

`app.storage.hls`（落盘）+ `app.utils.task_queue`（顺序）+ `client_manager`（代次真源，
中台 leaf，谁都可以向下依赖）。**不依赖任何别的 service。**
"""

from __future__ import annotations

import logging
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from app.domain.frame import Frame
from app.storage import hls
from app.utils.task_queue import SerialTaskQueue

from ._sweeper import SegmentSweeper
from .config import RecordingConfig, get_recording_config

logger = logging.getLogger(__name__)

# 队列名（出现在线程名与所有队列日志里）。
_QUEUE_NAME = "recording"


class _SegmentJob(NamedTuple):
    """一个待落盘的段。**打包形状，不是对外契约**——外面交 `cq` + 帧，拿不到也用不着它。

    `cq` 是提交那一刻的对象引用，只做两件事：与**当时**注册的 CQ 判等、以及被记进
    `_claimed_by`。不调用它的任何方法，故不要求它还活着（`close()` 之后照样能判等）。
    """

    task_id: int
    step_id: int
    track: str
    frames: List[Frame]
    cq: object

    @property
    def label(self) -> str:
        return f"seg:{self.task_id}/{self.step_id}/{self.track}@{hls.ts_to_us(self.frames[0].timestamp)}"


class RecordingService:
    """HLS 录制的编排者：拉段 → 打包 → 按序落盘。"""

    def __init__(
        self,
        config: Optional[RecordingConfig] = None,
        *,
        clients=None,
    ) -> None:
        """
        Args:
            config: 不传则用全局单例配置。
            clients: 代次真源 + CQ 快照来源，不传则用 `client_manager`。**只注入这一个
                协作者**：包一层 `snapshot_fn` / `current_owner_fn` 之类的窄回调只是
                多两个要记的名字，单测直接塞一个假的 client_manager 更短。
        """
        self.config = config if config is not None else get_recording_config()
        if clients is None:
            # 函数体内 import：写在模块级会把 client → numpy 那条链变成每个
            # `import app.services.recording.*` 的过路费（同 PersistenceManager 的写法）。
            from app.services.client.manager import client_manager

            clients = client_manager
        self._clients = clients

        # 落盘队列。**在 start() 里建**：SerialTaskQueue 是一次性的（stop() 之后不能再
        # start()），放构造函数会让单例在 start/stop 两轮之后炸。
        self._queue: Optional[SerialTaskQueue] = None
        self._sweeper: Optional[SegmentSweeper] = None

        # (task_id, step_id) → 已经认领过这个目录的那个 cq。
        # 它回答的是「本代次在这个 step 上写过第一段了吗」，由此决定要不要先清掉上一代。
        #
        # **认领是一次性的，落盘是连续的**：只在「我还注册着 + 这个 step 我还没认领过」
        # 时写入一次（见 `_write` 的 ②），此后同代次的段照写不更新；拆除后迟到的残段
        # 也照写不更新。故它不是「最后写这个目录的是谁」。
        #
        # **无锁，因为只有队列那一个线程碰它**：`_write` 读写它，`_forget` 清它，两者都
        # 跑在队列上。控制面调的是 `forget_task`，那只是往队列里排一个任务。
        self._claimed_by: Dict[Tuple[int, int], object] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """起队列与 sweeper。"""
        self._queue = SerialTaskQueue(_QUEUE_NAME, maxsize=self.config.queue_size)
        self._queue.start()
        self._sweeper = SegmentSweeper(
            clients=self._clients,
            service=self,
            interval_seconds=self.config.sweep_interval_seconds,
        )
        self._sweeper.start()
        logger.info("[recording] 已启动")

    def stop(self, timeout: float = 10.0) -> None:
        """停 sweeper 与队列。

        **顺序不能反**：先停 sweeper 不再拉新段，再停队列让它把已入队的排空。反过来
        会在队列停机后继续拉段，那些段提交被拒、帧已经从 CQ 弹出去了 —— 真丢。
        """
        if self._sweeper is not None:
            self._sweeper.stop(timeout=5.0)
            self._sweeper = None
        if self._queue is not None:
            self._queue.stop(timeout=timeout)
            self._queue = None
        logger.info("[recording] 已停止")

    # ── 对外 ────────────────────────────────────────────────────────────────────

    def submit_segment(self, cq, track: str, frames: Sequence[Frame]) -> bool:
        """把一段帧打包成落盘任务交给队列。

        Args:
            cq: 这段帧属于哪一次 run。`task_id` / `step_id` 与代次身份全取自它——签名里
                不再单列，那三个值本来就在同一个对象上，拆开传只会多三个对不上的机会。
            track: `"raw"` 或 `"processed"`。
            frames: 该段的帧，按时间升序。

        Returns:
            是否入队成功。**False 意味着这段不会被写**（队列满、队列还没起、或入参不合法）。

        入队成功不等于写成功：真正的落盘在队列线程上异步发生，且可能被代次校验丢弃
        （见 `_write`）。要结果的调用方说明它本就不该异步。
        """
        if self._queue is None:
            logger.warning("[recording] 队列未启动，丢弃段: track=%s", track)
            return False

        task_id = cq.task_id
        step_id = cq.step_id
        if task_id is None or step_id is None:
            # 裸建 / 未绑定 step 的 CQ 定位不到落盘分区。不抛：它是运行期的常态之一。
            logger.warning(
                "[recording] cq 缺 task_id/step_id，丢弃段: task_id=%s step_id=%s track=%s",
                task_id, step_id, track,
            )
            return False
        if not frames:
            # 空段在 storage 层是 ValueError（调用方算错了批次）。在这里拦掉，别让它变成
            # 队列里一条需要人去看的 error log。
            return False

        job = _SegmentJob(
            task_id=task_id,
            step_id=step_id,
            track=track,
            frames=list(frames),
            cq=cq,
        )
        return self._queue.submit(lambda: self._write(job), label=job.label)

    def flush_residual(self, cq) -> None:
        """拆除期落盘 CQ 残余帧：drain raw/processed → 按 `ca_segment_len` 切段 → 逐段入队。

        须在 `cq.close()` 释放帧之前调（RunController 保证）。切段口径与 sweeper 一致，
        差别只是它拉的是"攒满的整段"、这里拉的是"最后不足一段的那点"。

        代次身份随 `cq` 一起走，所以这批段与它们所属的那次 run 永远对得上——哪怕 CQ 已经
        出了注册表（见 `_write` 的分档）。
        """
        if cq.task_id is None or cq.step_id is None:
            logger.warning(
                "[recording] flush_residual: cq 缺 task_id/step_id，跳过 (task_id=%s)", cq.task_id
            )
            return

        seg_len = cq.ca_segment_len
        for track, frames in (
            ("raw", cq.drain_ca_raw()),
            ("processed", cq.drain_ca_processed()),
        ):
            for i in range(0, len(frames), seg_len):
                self.submit_segment(cq, track, frames[i : i + seg_len])

    def forget_task(self, task_id: int) -> bool:
        """回收该 task 名下所有 step 的代次记录（任务拆除后调用），返回是否排进队列。

        不回收则 `_claimed_by` 随 `(task_id, step_id)` 单调增长——长跑内存慢泄漏（论证同原
        `hls_strategy.release_dir_locks`）。

        **走队列而不是当场清**，两个收益：

        1. **`_claimed_by` 从此只被队列那一个线程碰**，于是不需要任何锁——这是本服务
           「全系统零锁」能成立的最后一块（见模块 docstring）。
        2. **顺序对**：FIFO 保证它排在这一代所有段之后，记录活到最后一段写完才消失，
           而不是在残段还没落盘时就被抹掉。

        排不进去（队列满或已停机）只是漏一条记录：它是个 payload 已释放的小壳，且下一代
        在这个 step 首写时会照常认领覆盖它。故用默认 timeout、不降级同步执行——这跟
        `SerialTaskQueue` 文档里「purge 这类不许丢的任务要传大 timeout」正好相反。
        """
        if self._queue is None:
            return False
        submitted = self._queue.submit(
            lambda: self._forget(task_id), label=f"forget:{task_id}"
        )
        if not submitted:
            logger.warning("[recording] 代次记录未能排进队列，漏一条: task_id=%s", task_id)
        return submitted

    def _forget(self, task_id: int) -> None:
        """在队列线程上清掉该 task 的代次记录。

        **清掉在途任务的记录是安全的**：之后再执行的旧段会走「这个 CQ 已不在注册表里」
        那一档，只追加、不删目录（见 `_write` 的 ②）。真正的新一代会自己重新认领。
        """
        stale = [key for key in self._claimed_by if key[0] == task_id]
        for key in stale:
            del self._claimed_by[key]
        if stale:
            logger.debug("[recording] 代次记录已回收: task_id=%s 条数=%d", task_id, len(stale))

    # ── 队列任务体 ─────────────────────────────────────────────────────────────

    def _write(self, job: _SegmentJob) -> None:
        """在队列线程上落一段盘：代次校验 → 本代次首写自清 → 写入。

        **失败不重试。** 异常由 `SerialTaskQueue._execute` 统一记 error 后吞掉，本模块
        刻意不包 `GuardedExecutor`：`insert_segment` 把清单条目排在最后登记，重试若落在
        「条目已追加、统计写失败」之后，会往 playlist 里写出**重复条目**，毁掉整个 step
        的回放；而现在会抛的失败（ffmpeg 缺失/换代、盘满）基本都是非瞬时的，重试也修不好。
        丢一段 ≈ 丢 10 秒录像，比毁一整段回放便宜。
        """
        # `current` = 该 task **此刻**注册的 CQ（没有则 None），`job.cq` = 提交那一刻的。
        # 两个名字都是 cq，差别只在时间，所以谁都不叫 `cq`。
        current = self._clients.get(job.task_id)

        # ① 代次校验（乐观锁）：同一个 (task, step) 上换了新 CQ → 本段属于上一代，丢弃。
        #
        # **失败动作是丢弃，不是重读重试**——旧 run 的段在新 run 里没有任何意义，重试就是
        # 把它硬塞进去。看见"乐观锁"三个字别顺手补一个重试循环。
        #
        # **必须连 step_id 一起比**：注册表按 task_id 索引，但盘上是一个 (task, step) 一个
        # 目录。同一个 task 从第 2 步切到第 3 步确实换了 CQ，可新 CQ 写的是第 3 步的目录，
        # 跟手上这批第 2 步的段**在盘上根本不冲突，它们照常落盘**。不比 step_id 就会把切步
        # 时 stop_run 交出来的上一步残段全当成"过期"丢掉。
        if current is not None and current is not job.cq and current.step_id == job.step_id:
            logger.debug("[recording] 换代，丢弃上一代的段: %s", job.label)
            return

        # ② 本代次首写自清（懒惰 supersede）：这一代第一次往这个 step 写东西时，先把上一代
        # 的产物整个清掉。懒惰而非在 start_run 时 eager 删——新 run 若一段都没写出来，用户
        # 还能回放上一次的录像。
        #
        # ⚠ `current is job.cq` 这个前提是防炸的，不是优化。少了它，规则退化成「表里不是我
        # 就清」，于是拆除之后才执行的残段（current 已是 None、表已被 forget_task 清掉）会把
        # 这个 step **刚写完的整段录像删掉**再写。加上之后，清目录只可能发生在这个 CQ 还注册
        # 着的时候，残段与迟到段一律只追加、永不删。
        #
        # 表里不可能登记着比自己更新的一代：队列是 FIFO 且只有一个消费线程，新一代的第一
        # 次提交必然晚于旧一代的所有提交。**这就是队列不能加 worker 的原因。**
        key = (job.task_id, job.step_id)
        if current is job.cq and self._claimed_by.get(key) is not job.cq:
            hls.delete(job.task_id, job.step_id)
            self._claimed_by[key] = job.cq

        hls.insert_segment(job.task_id, job.step_id, job.track, job.frames)
