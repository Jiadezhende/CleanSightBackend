"""录制服务 —— 落盘编排：把 CQ 里攒好的东西变成盘上的产物（HLS 段 + 检测结果）。

    start() / stop()                          生命周期
    collect_from(cq)                          取走该 CQ 此刻该落盘的一切（sweeper 每 tick 调）
    submit_segment(cq, track, frames) -> bool 打包 + 入 hls 队列
    submit_detections(cq, frames) -> bool     打包 + 入 detections 队列
    flush_residual(cq, until_ts=None)         把残帧切完落盘（拆除期 RunControlService、断流期 health_monitor 调）

落盘格式全在 `app.storage.hls` / `app.storage.inference`；本模块只管何时拉、按什么顺序写。
产物写进 `cq.run` 指向的 run 目录：每次 run 一个目录，换代隔离不归本模块。

**两条队列，不是一条**：段写里有 ffmpeg 编码，detections 排在它后面会跟着
一起被背压丢，而丢一次就是十几帧检测结果、静默。两条队列互不阻塞。

**两条不变式，破了都不报错、只是数据静默损坏**（推导见
`docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §5.1）：

1. **队列不能加 worker**（两条都是）：同一 run 内 tfdt 按执行顺序累计，并发会碰撞。配置里因此
   没有 `workers`。
2. **取帧必须持 `_collect_lock`，且覆盖「取走 + 入队」**：sweeper、断流 flush、拆除 flush 三条
   路径都从 CQ 队头取帧，锁让先取的先入队、段的 tfdt 与 ts 同序。

依赖：`app.storage.hls` / `app.storage.inference` + `app.services.utils.task_queue` + `client_service`，
不依赖别的 service。
"""

from __future__ import annotations

import logging
import threading
from typing import List, NamedTuple, Optional, Sequence

from app.types.detection import FrameDetection
from app.types.frame import Frame
from app.types.run import RunIdentity
from app.storage import hls, inference
from app.services.utils.task_queue import SerialTaskQueue
from app.services.utils.metrics import frame_drop_total

from .sweep_worker import SegmentSweeper
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
        return f"seg:{r.task_id}/{r.step_id}/{r.run_id}/{self.track}@{hls.ts_to_ms(self.frames[0].timestamp)}"


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
            # `import app.services.recording.*` 的过路费（同 AlarmService 的写法）。
            from app.services.client.instance import client_service

            clients = client_service
        self._clients = clients

        # 落盘队列。**在 start() 里建**：SerialTaskQueue 是一次性的（stop() 之后不能再
        # start()），放构造函数会让单例在 start/stop 两轮之后炸。
        #
        # **两条而不是一条**：段写里有 ffmpeg 编码，detections 排在它后面会
        # 跟着一起被背压丢。两条各自单消费线程，"提交序 = 执行序"在各自内部成立。
        self._hls_queue: Optional[SerialTaskQueue] = None
        self._detection_queue: Optional[SerialTaskQueue] = None
        self._sweep_worker: Optional[SegmentSweeper] = None

        # HLS 段队列的在途字节（已入队、未写完）。段是像素帧，只按段数限长挡不住内存：
        # 编码一停滞，100 段 × 285MB 就先把内存吃满才轮到丢段。提交线程（sweeper / 断流 / 拆除路径）
        # 加、队列线程减，故带锁。
        self._hls_inflight_bytes = 0
        self._hls_inflight_lock = threading.Lock()

        # 取帧锁：`collect_from` / `flush_residual` 全程持有，「取走 + 入队」对所有取帧路径串行。
        # 只护取走原子不够：入队在 CQ 锁外，后取的段可能先入队 → tfdt 倒序、画面丢一截。
        # 锁序：本锁 → CQ 锁 / `_hls_inflight_lock`；生产方（decoder / 写回 / viz）不取本锁。
        self._collect_lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """起两条队列与 sweeper。"""
        with self._hls_inflight_lock:
            self._hls_inflight_bytes = 0
        self._hls_queue = SerialTaskQueue(_HLS_QUEUE_NAME, maxsize=self.config.queue_size)
        self._hls_queue.start()
        self._detection_queue = SerialTaskQueue(
            _DETECTION_QUEUE_NAME, maxsize=self.config.queue_size
        )
        self._detection_queue.start()
        self._sweep_worker = SegmentSweeper(
            clients=self._clients,
            service=self,
            interval_seconds=self.config.sweep_interval_seconds,
        )
        self._sweep_worker.start()
        logger.info("[recording] 已启动")

    def stop(self, timeout: float = 10.0) -> None:
        """停 sweeper 与两条队列。

        **顺序不能反**：先停 sweeper 不再拉新产物，再停队列让它们把已入队的排空。反过来
        会在队列停机后继续拉，那些产物提交被拒、数据已经从 CQ 弹出去了 —— 真丢。
        两条队列之间没有顺序要求（写的是不同域的不同文件）。
        """
        if self._sweep_worker is not None:
            self._sweep_worker.stop(timeout=5.0)
            self._sweep_worker = None
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
            是否入队成功。**False 意味着这段不会被写**（队列满、在途字节超
            `queue_max_mb`、队列还没起、或入参不合法）。

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
        nbytes = sum(f.frame.nbytes for f in job.frames)
        if not self._reserve_hls_bytes(nbytes):
            frame_drop_total.labels(reason="recording_budget").inc(len(job.frames))
            logger.warning(
                "[recording] 段队列在途字节超预算，丢弃段: %s | %.0fMB in flight, budget %dMB",
                job.label, self._hls_inflight_bytes / 2**20, self.config.queue_max_mb,
            )
            return False
        ok = self._hls_queue.submit(lambda: self._write(job, nbytes), label=job.label)
        if not ok:
            self._release_hls_bytes(nbytes)
        return ok

    def _reserve_hls_bytes(self, nbytes: int) -> bool:
        """在途字节记账：未超预算则计入并返回 True。"""
        budget = self.config.queue_max_mb * 2**20
        with self._hls_inflight_lock:
            if self._hls_inflight_bytes + nbytes > budget:
                return False
            self._hls_inflight_bytes += nbytes
            return True

    def _release_hls_bytes(self, nbytes: int) -> None:
        with self._hls_inflight_lock:
            self._hls_inflight_bytes -= nbytes

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
                **栅栏只作用于段**：detections 无论哪条路径都是全排空，它没有"横跨 gap"的问题。

        拆除期须在 `cq.close()` 释放帧之前调（RunControlService 保证）。切段口径与 sweeper 一致，
        差别只是它拉的是"攒满的整段"、这里拉的是"剩下不足一段的那点"。任意线程可直接调：
        与 sweeper 的先后由取帧锁串行（模块不变式 2）。
        """
        if cq.run is None:
            logger.warning("[recording] flush_residual: cq 未绑定 run，跳过")
            return

        with self._collect_lock:
            seg_len = cq.ca_segment_len
            for track, frames in (
                ("raw", cq.drain_ca_raw(until_ts)),
                ("processed", cq.drain_ca_processed(until_ts)),
            ):
                for i in range(0, len(frames), seg_len):
                    self.submit_segment(cq, track, frames[i : i + seg_len])

            self.submit_detections(cq, cq.drain_ca_detections())

    def collect_from(self, cq) -> None:
        """把这个 CQ 此刻攒满的整段与检测结果取走并入队。**由 sweeper 线程周期调用。**

            ① 拉 raw 整段   ② 拉 processed 整段   ③ 拉 detections

        ③ 与 ①② 之间没有顺序约束：detections 走另一条队列、写另一个域的另一个文件，与段的
        媒体轴无关。放最后只是因为它最不紧急。

        **这套顺序属于本服务，不属于定时器**：`sweep_worker` 只负责"每隔 1 秒对每个活跃 CQ 调
        一次本方法"。
        """
        if cq.run is None:
            # 裸建 / 未绑定 run：定位不到落盘目录。**不取帧**——取了就只能丢。
            return

        with self._collect_lock:
            while (seg := cq.take_raw_segment()) is not None:
                self.submit_segment(cq, "raw", seg)
            while (seg := cq.take_processed_segment()) is not None:
                self.submit_segment(cq, "processed", seg)

            self.submit_detections(cq, cq.drain_ca_detections())

    # ── 队列任务体 ─────────────────────────────────────────────────────────────

    def _write(self, job: _SegmentJob, nbytes: int = 0) -> None:
        """在队列线程上把一段写进它所属 run 的 `hls/`；无论成败都归还在途字节 `nbytes`。

        旧一代迟到的段写进它自己的 run 目录，新一代读不到；run 目录已被回收时
        `insert_segment` 抛 `FileNotFoundError`，这段丢弃。

        **失败不重试。** 异常由 `SerialTaskQueue._execute` 统一记 error 后吞掉，本模块
        刻意不包重试：`insert_segment` 把清单条目排在最后登记，重试若落在
        「条目已追加、统计写失败」之后，会往 playlist 里写出**重复条目**，毁掉整个 run
        的回放；而现在会抛的失败（ffmpeg 缺失/换代、盘满）基本都是非瞬时的，重试也修不好。
        丢一段 ≈ 丢 10 秒录像，比毁一整段回放便宜。
        """
        try:
            hls.insert_segment(job.run, job.track, job.frames)
        finally:
            self._release_hls_bytes(nbytes)

    def _write_detections(self, job: _DetectionJob) -> None:
        """在 detections 队列线程上把一批检测结果追加进它所属 run 的 `inference/`。

        **失败不重试**：`append_detections` 是纯追加，重试会写出重复帧；而能让它抛的
        （盘满、权限、run 已回收）都不是瞬时故障。丢一批 ≈ 丢一个 sweep tick 的检测结果。
        """
        inference.append_detections(job.run, job.frames)
