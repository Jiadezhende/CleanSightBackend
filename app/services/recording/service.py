"""录制服务 —— 落盘编排：把 CQ 里攒好的东西变成盘上的产物（HLS 段 + 检测结果）。

    start() / stop()                          生命周期
    collect_from(cq)                          取走该 CQ 此刻该落盘的一切（sweeper 每 tick 调）
    submit_segment(cq, track, frames) -> bool 打包 + 入 hls 队列
    submit_detections(cq, frames) -> bool     打包 + 入 detections 队列
    flush_residual(cq, until_ts=None)         把不足一段的残帧切完落盘（拆除期 RunControlService 调）
    request_residual_flush(cq, fence_ts)      断流时登记一次残帧 flush（不就地执行）

落盘格式全在 `app.storage.hls` / `app.storage.inference`；本模块只管何时拉、按什么顺序写。
产物写进 `cq.run` 指向的 run 目录：每次 run 一个目录，换代隔离不归本模块。

**两条队列，不是一条**：段写里有 ffmpeg 转码（单段 0.26–3 s），detections 排在它后面会跟着
一起被背压丢，而丢一次就是十几帧检测结果、静默。两条队列互不阻塞。

**三条不变式，破了都不报错、只是数据静默损坏**（推导见
`docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §5.1）：

1. **队列不能加 worker**（两条都是）：同一 run 内 tfdt 按执行顺序累计，并发会碰撞。配置里因此
   没有 `workers`。
2. **运行期 CQ 的 drain 者只能有 sweeper 一个**，入口是 `collect_from`；断流走
   `request_residual_flush` 登记、由 sweeper 那一轮执行，不要在别的线程直接 drain。
3. **`_pending_flush` 有三个线程碰**（health_monitor 写 / sweeper 取 / 拆除路径回收）。
   免锁靠 `dict` 的 `__setitem__`、`pop` 各自原子。它只服务 HLS：detections 没有"段横跨
   断流 gap"这回事，故不需要栅栏。

依赖：`app.storage.hls` / `app.storage.inference` + `app.services.utils.task_queue` + `client_service`，
不依赖别的 service。
"""

from __future__ import annotations

import logging
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from app.types.detection import FrameDetection
from app.types.frame import Frame
from app.types.run import RunIdentity
from app.storage import hls, inference
from app.services.utils.task_queue import SerialTaskQueue

from ._sweeper import SegmentSweeper
from .config import RecordingConfig, get_recording_config

logger = logging.getLogger(__name__)

# 队列名（出现在线程名与所有队列日志里）。
_HLS_QUEUE_NAME = "recording"
_DETECTION_QUEUE_NAME = "recording-detections"


class _SegmentJob(NamedTuple):
    """一个待落盘的段。**打包形状，不是对外契约**——外面交 `cq` + 帧，拿不到也用不着它。"""

    run: RunIdentity
    track: str
    frames: List[Frame]

    @property
    def label(self) -> str:
        r = self.run
        return f"seg:{r.task_id}/{r.step_id}/{r.run_id}/{self.track}@{hls.ts_to_us(self.frames[0].timestamp)}"


class _DetectionJob(NamedTuple):
    """一批待落盘的帧检测结果。**打包形状，不是对外契约**（同 `_SegmentJob`）。

    没有 track，也没有"攒满一段"的概念——detections 每帧一行、行间无依赖，sweeper 每 tick
    把缓冲里有多少交多少。
    """

    run: RunIdentity
    frames: List[FrameDetection]

    @property
    def label(self) -> str:
        r = self.run
        return f"det:{r.task_id}/{r.step_id}/{r.run_id}×{len(self.frames)}"


class RecordingService:
    """落盘编排者：拉产物 → 打包 → 按序落盘（HLS 段一条队列，检测结果一条）。"""

    def __init__(
        self,
        config: Optional[RecordingConfig] = None,
        *,
        clients=None,
    ) -> None:
        """
        Args:
            config: 不传则用全局单例配置。
            clients: CQ 快照来源（sweeper 用），不传则用 `client_service`。**只注入这一个
                协作者**：包一层 `snapshot_fn` / `current_owner_fn` 之类的窄回调只是
                多两个要记的名字，单测直接塞一个假的 client_service 更短。
        """
        self.config = config if config is not None else get_recording_config()
        if clients is None:
            # 函数体内 import：写在模块级会把 client → numpy 那条链变成每个
            # `import app.services.recording.*` 的过路费（同 PersistenceManager 的写法）。
            from app.services.client.instance import client_service

            clients = client_service
        self._clients = clients

        # 落盘队列。**在 start() 里建**：SerialTaskQueue 是一次性的（stop() 之后不能再
        # start()），放构造函数会让单例在 start/stop 两轮之后炸。
        #
        # **两条而不是一条**：段写里有 ffmpeg 转码（单段 0.26–3 s），detections 排在它后面会
        # 跟着一起被背压丢。两条各自单消费线程，"提交序 = 执行序"在各自内部成立。
        self._hls_queue: Optional[SerialTaskQueue] = None
        self._detection_queue: Optional[SerialTaskQueue] = None
        self._sweeper: Optional[SegmentSweeper] = None

        # (task_id, step_id) → (cq, fence_ts)：断流时挂起的「把这一刻之前的残帧切出来」请求。
        #
        # **三个线程碰它**：health_monitor 写（`request_residual_flush`）、sweeper 线程取走
        # （`collect_from` → `_take_pending_flush`）、拆除路径回收（`flush_residual` 的
        # `until_ts=None`）。生产者与消费者分离是刻意的，见 `request_residual_flush` 为什么不能
        # 就地 drain。免锁靠的是 `dict` 的 `__setitem__` / `pop` 各自是一次原子的 C 调用。
        #
        # 条目带着 cq 是为了**对象身份 fence**：同一个 (task, step) 换代之后，上一代挂起的
        # 请求不能落到新一代的 CQ 上。取走与回收都要核对它。
        self._pending_flush: Dict[Tuple[int, int], Tuple[object, float]] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """起两条队列与 sweeper。"""
        self._hls_queue = SerialTaskQueue(_HLS_QUEUE_NAME, maxsize=self.config.queue_size)
        self._hls_queue.start()
        self._detection_queue = SerialTaskQueue(
            _DETECTION_QUEUE_NAME, maxsize=self.config.queue_size
        )
        self._detection_queue.start()
        self._sweeper = SegmentSweeper(
            clients=self._clients,
            service=self,
            interval_seconds=self.config.sweep_interval_seconds,
        )
        self._sweeper.start()
        logger.info("[recording] 已启动")

    def stop(self, timeout: float = 10.0) -> None:
        """停 sweeper 与两条队列。

        **顺序不能反**：先停 sweeper 不再拉新产物，再停队列让它们把已入队的排空。反过来
        会在队列停机后继续拉，那些产物提交被拒、数据已经从 CQ 弹出去了 —— 真丢。
        两条队列之间没有顺序要求（写的是不同域的不同文件）。
        """
        if self._sweeper is not None:
            self._sweeper.stop(timeout=5.0)
            self._sweeper = None
        if self._hls_queue is not None:
            self._hls_queue.stop(timeout=timeout)
            self._hls_queue = None
        if self._detection_queue is not None:
            self._detection_queue.stop(timeout=timeout)
            self._detection_queue = None
        logger.info("[recording] 已停止")

    # ── 对外 ────────────────────────────────────────────────────────────────────

    def submit_segment(self, cq, track: str, frames: Sequence[Frame]) -> bool:
        """把一段帧打包成落盘任务交给队列。

        Args:
            cq: 这段帧属于哪一次 run（`cq.run`）。
            track: `"raw"` 或 `"processed"`。
            frames: 该段的帧，按时间升序。

        Returns:
            是否入队成功。**False 意味着这段不会被写**（队列满、队列还没起、或入参不合法）。

        入队成功不等于写成功：真正的落盘在队列线程上异步发生（见 `_write`）。要结果的调用方
        说明它本就不该异步。
        """
        if self._hls_queue is None:
            logger.warning("[recording] 队列未启动，丢弃段: track=%s", track)
            return False

        if cq.run is None:
            # 裸建 / 未绑定 run 的 CQ 定位不到落盘目录。不抛：它是运行期的常态之一。
            logger.warning("[recording] cq 未绑定 run，丢弃段: track=%s", track)
            return False
        if not frames:
            # 空段在 storage 层是 ValueError（调用方算错了批次）。在这里拦掉，别让它变成
            # 队列里一条需要人去看的 error log。
            return False

        job = _SegmentJob(run=cq.run, track=track, frames=list(frames))
        return self._hls_queue.submit(lambda: self._write(job), label=job.label)

    def submit_detections(self, cq, frames: Sequence[FrameDetection]) -> bool:
        """把一批帧检测结果打包成落盘任务交给 detections 队列。

        Args:
            cq: 这批检测结果属于哪一次 run（`cq.run`）。
            frames: 帧级 `FrameDetection`，按时间升序（= 写回顺序）。

        Returns:
            是否入队成功。False 意味着这批不会被写（队列满、队列还没起、或入参不合法）。

        入队成功不等于写成功：真正的落盘在队列线程上异步发生（见 `_write_detections`）。
        """
        if self._detection_queue is None:
            logger.warning("[recording] detections 队列未启动，丢弃 %d 帧检测结果", len(frames))
            return False

        if cq.run is None:
            # 同 submit_segment：未绑定 run 的 CQ 定位不到落盘目录，不抛。
            logger.warning("[recording] cq 未绑定 run，丢弃 %d 帧检测结果", len(frames))
            return False
        if not frames:
            # 空批在 storage 层是 no-op，但排一个什么都不干的任务只会占队列。
            return False

        job = _DetectionJob(run=cq.run, frames=list(frames))
        return self._detection_queue.submit(
            lambda: self._write_detections(job), label=job.label
        )

    def flush_residual(self, cq, until_ts: Optional[float] = None) -> None:
        """落盘 CQ 残余产物：drain raw/processed 切段入队，再把剩下的 detections 一并交出。

        Args:
            cq: 要收尾的 CQ。
            until_ts: 时间戳栅栏。`None`（拆除期）= 全排空；给值（断流期）= 只切这一刻之前
                的帧，重连后的新帧留在队列里等 sweeper 照常拉整段。
                **栅栏只作用于段**：detections 无论哪条路径都是全排空，它没有"横跨 gap"的问题
                （见模块不变式 3）。

        拆除期须在 `cq.close()` 释放帧之前调（RunControlService 保证）。切段口径与 sweeper 一致，
        差别只是它拉的是"攒满的整段"、这里拉的是"剩下不足一段的那点"。

        拆除期（`until_ts=None`）顺带回收本 cq 挂起的断流 flush 请求：调用方持 `lock_for`，
        期间不会有新一代登记同一个键。

        **断流期不要直接调本方法**（那会引入第二个 drain 者，见 `request_residual_flush`）；
        断流那条路的入口是 `collect_from`。
        """
        if cq.run is None:
            logger.warning("[recording] flush_residual: cq 未绑定 run，跳过")
            return

        if until_ts is None:
            key = (cq.run.task_id, cq.run.step_id)
            entry = self._pending_flush.get(key)
            if entry is not None and entry[0] is cq:
                self._pending_flush.pop(key, None)

        seg_len = cq.ca_segment_len
        for track, frames in (
            ("raw", cq.drain_ca_raw(until_ts)),
            ("processed", cq.drain_ca_processed(until_ts)),
        ):
            for i in range(0, len(frames), seg_len):
                self.submit_segment(cq, track, frames[i : i + seg_len])

        self.submit_detections(cq, cq.drain_ca_detections())

    def collect_from(self, cq) -> None:
        """把这个 CQ 此刻该落盘的东西全部取走并入队。**由 sweeper 线程周期调用。**

        这是运行期取产物的唯一入口，四件事的顺序在这里定死：

            ① 拉 raw 整段   ② 拉 processed 整段   ③ 断流残帧（如果有挂起请求）   ④ 拉 detections

        **③ 必须在 ①② 之后**：残段的帧 ts 晚于本轮所有整段，反过来提交会让清单 ts 逆序。
        入队序即执行序，而每段的 `tfdt` 是执行时读到的累计 EXTINF——顺序一乱，后写的段就在
        媒体轴上盖掉先写的，不报错，只是画面丢一截。

        **④ 与 ①②③ 之间没有顺序约束**：detections 走另一条队列、写另一个域的另一个文件，
        与段的媒体轴无关。放最后只是因为它最不紧急。

        **这套顺序属于本服务，不属于定时器**：`_sweeper` 只负责"每隔 1 秒对每个活跃 CQ 调
        一次本方法"，它不必知道挂起请求是什么、也不必知道上面那条不变式。

        运行期本方法是 CQ 的唯一 drain 者，所以它只能被 sweeper 那一个线程调
        （理由见 `request_residual_flush`）。
        """
        if cq.run is None:
            # 裸建 / 未绑定 run：定位不到落盘目录。**不取帧**——取了就只能丢。
            return

        while (seg := cq.take_raw_segment()) is not None:
            self.submit_segment(cq, "raw", seg)
        while (seg := cq.take_processed_segment()) is not None:
            self.submit_segment(cq, "processed", seg)

        fence_ts = self._take_pending_flush(cq)
        if fence_ts is not None:
            self.flush_residual(cq, until_ts=fence_ts)
            return  # flush_residual 末尾已经把 detections 一并交出，别再 drain 一次空的

        self.submit_detections(cq, cq.drain_ca_detections())

    def request_residual_flush(self, cq, fence_ts: float) -> None:
        """请求把 `fence_ts` 之前的残帧单独切成段（断流时由 health_monitor 调）。

        Args:
            cq: 断流的那次 run。
            fence_ts: 断流前最后一帧的墙钟 ts —— 只切它之前的帧。

        **只登记，不执行**：真正的 drain 由 sweeper 线程在下一 tick 做。运行期 sweeper 是
        CQ 的唯一 drain 者，就地执行会变成两个——两次 drain 各自被 CQ 的锁保护、帧不重不漏，
        但 `submit_segment` 发生在锁外，谁先入队由调度决定：**入队序一乱，tfdt 就乱**
        （每段的 tfdt = 入队执行时读到的累计 EXTINF），清单 ts 不再升序，读侧段级定位失效。
        走队列挡不住这个——队列只保证执行序 = 入队序。

        为什么不是"重连成功时再切"：成功的判据就是"已经来了新帧"，那时残批里已经混进重连
        后的帧，段仍横跨 gap（只是被吞的比例变了），慢放照旧。

        重复请求按后到的覆盖（同一次断流只会请求一次，见 `_enter_reconnect_mode`）。
        """
        if cq.run is None:
            logger.warning("[recording] request_residual_flush: cq 未绑定 run，跳过")
            return
        self._pending_flush[(cq.run.task_id, cq.run.step_id)] = (cq, fence_ts)
        logger.info(
            "[recording] 已登记断流残帧 flush: task_id=%s step_id=%s fence_ts=%.3f",
            cq.run.task_id, cq.run.step_id, fence_ts,
        )

    def _take_pending_flush(self, cq) -> Optional[float]:
        """取走该 CQ 挂起的 flush 栅栏（一次性）；没有则 `None`。

        **包内私有**，唯一消费者是 `collect_from`。它曾经公开、由 `_sweeper` 直接调——那让
        定时器知道了「挂起请求」这回事，连带把「先整段后残段」的顺序不变式也搬进了定时器，
        而那条不变式成立的理由（tfdt = 执行时读到的累计 EXTINF）整个是本模块的事。

        对象身份不匹配 = 请求属于同一 (task, step) 的上一代 CQ，那一代已经没了，条目直接丢弃。
        """
        if cq.run is None:
            return None
        entry = self._pending_flush.pop((cq.run.task_id, cq.run.step_id), None)
        if entry is None:
            return None
        owner, fence_ts = entry
        if owner is not cq:
            logger.info(
                "[recording] 丢弃上一代的残帧 flush 请求: task_id=%s step_id=%s",
                cq.run.task_id, cq.run.step_id,
            )
            return None
        return fence_ts

    # ── 队列任务体 ─────────────────────────────────────────────────────────────

    def _write(self, job: _SegmentJob) -> None:
        """在队列线程上把一段写进它所属 run 的 `hls/`。

        旧一代迟到的段写进它自己的 run 目录，新一代读不到；run 目录已被回收时
        `insert_segment` 抛 `FileNotFoundError`，这段丢弃。

        **失败不重试。** 异常由 `SerialTaskQueue._execute` 统一记 error 后吞掉，本模块
        刻意不包 `GuardedExecutor`：`insert_segment` 把清单条目排在最后登记，重试若落在
        「条目已追加、统计写失败」之后，会往 playlist 里写出**重复条目**，毁掉整个 run
        的回放；而现在会抛的失败（ffmpeg 缺失/换代、盘满）基本都是非瞬时的，重试也修不好。
        丢一段 ≈ 丢 10 秒录像，比毁一整段回放便宜。
        """
        hls.insert_segment(job.run, job.track, job.frames)

    def _write_detections(self, job: _DetectionJob) -> None:
        """在 detections 队列线程上把一批检测结果追加进它所属 run 的 `inference/`。

        **失败不重试**：`append_detections` 是纯追加，重试会写出重复帧；而能让它抛的
        （盘满、权限、run 已回收）都不是瞬时故障。丢一批 ≈ 丢一个 sweep tick 的检测结果。
        """
        inference.append_detections(job.run, job.frames)
