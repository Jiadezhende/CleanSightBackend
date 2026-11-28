"""离线作业服务 —— 离线推理的提交、串行执行与状态查询。

    start() / stop()                          生命周期
    submit(run) -> OfflineJob                 入队；step 未配离线模型抛 ValidationError；该 run 正在运行 /
                                              队满抛 ConflictError；同一 run 在途返回在途那个
    get(run) -> OfflineJob|None               状态快照
    list_jobs() -> List[OfflineJob]           在途 + 最近结束的，按提交序
    cancel(run) -> bool                       排队中的直接取消，运行中的 kill 子进程

执行：一条 `SerialTaskQueue`（一次只跑一个），每个 job 用本模块的 `_spawn` 起一个 daemon 子进程
直接执行 `OfflineWorker.run(spec)`（子进程内钉卡 + 限核 + 降优先级 + 可 kill），结果经单向 Pipe
回传，本模块按结构化结果收口状态。`WorkerProcess` 是父侧进程句柄（等结果 / kill / 回收），只被本
服务使用，故不单拆模块。
**本模块不 import worker / torch**；子进程侧入口（钉卡顺序契约）在 analysis_child。

作业锁定一个 run（`RunIdentity`，由调用方经 `runs.query` 解析后传入）：子进程总带 `run_id`，
不同 run 各跑各的；该 run 就是注册表里当前在跑的 run → 409（输入还在写）。
状态只在内存，重启丢失（结果本身已落盘）。
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from multiprocessing.connection import Connection
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.types.run import RunIdentity
from app.services.inference.config import InferenceConfig, load_stage_config
from app.types.exceptions import ConflictError
from app.services.utils.task_queue import SerialTaskQueue

from .analysis_child import worker_main

logger = logging.getLogger(__name__)

__all__ = ["OfflineJob", "OfflineJobService"]

THREADS = 2              # 子进程 torch 线程数
QUEUE_SIZE = 20          # 排队上限（不含运行中的那个）
JOB_TIMEOUT_S = 1800.0   # 单个 job 墙钟上限，超时 kill → failed
HISTORY = 200            # 已结束的 job 最多保留几条
POLL_S = 1.0             # 等子进程 / 读结果的间隔：超时最多延迟这么久被发现
KILL_GRACE_S = 5.0       # kill 后等子进程退出的宽限；仍不退则放弃回收，不卡队列

QUEUED, RUNNING = "queued", "running"
COMPLETED, SKIPPED, RECLAIMED = "completed", "skipped", "reclaimed"
FAILED, CANCELLED = "failed", "cancelled"
_ACTIVE = (QUEUED, RUNNING)
_SUCCESS_STATUSES = (COMPLETED, SKIPPED, RECLAIMED)


@dataclass
class OfflineJob:
    """一个 run 的离线作业。对外只给快照（`replace` 出来的副本）。"""

    task_id: int
    step_id: int
    run_id: int
    status: str = QUEUED
    producer: Optional[str] = None
    segment_count: int = 0
    alarm_count: Optional[int] = None  # 规则核对产出的告警数；未核对为 None
    message: str = ""
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── 子进程（父侧：spawn + 句柄；只有本服务用，不单拆模块）───────────────────────


class WorkerProcess:
    """一次离线作业的子进程句柄：读结果、等退出、kill 与回收。"""

    def __init__(self, proc, conn: Connection) -> None:
        self._proc = proc
        self._conn = conn
        self._result: Optional[Dict[str, Any]] = None
        self._at_eof = False
        self._closed = False

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid

    @property
    def exitcode(self) -> Optional[int]:
        return self._proc.exitcode

    @property
    def at_eof(self) -> bool:
        """结果通道已 EOF（子进程关闭写端或已退出）。"""
        return self._at_eof

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        self._proc.join(timeout)

    def kill(self) -> None:
        try:
            if self._proc.is_alive():
                self._proc.kill()
        except (OSError, ValueError):  # 已退出 / 句柄已关
            pass

    def poll_result(self, timeout: float) -> Optional[Dict[str, Any]]:
        """最多等 timeout 秒取结果：取到后重复调用返回同一份；超时 / EOF / 非 dict → None。"""
        if self._result is not None:
            return self._result
        if self._at_eof:
            return None
        try:
            if not self._conn.poll(timeout):
                return None
            payload = self._conn.recv()
        except (EOFError, OSError):
            self._at_eof = True
            return None
        self._result = payload if isinstance(payload, dict) else None
        return self._result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - 关闭失败无后续影响
            pass
        try:
            if not self._proc.is_alive():
                self._proc.close()
        except (OSError, ValueError):  # pragma: no cover - 已关 / 仍在跑
            pass


def _spawn(
    task_id: int, step_id: int, run_id: Optional[int], threads: int,
) -> WorkerProcess:
    """起一个 spawn 子进程跑一次 run；父进程只留读端（子进程退出即 EOF）。"""
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=worker_main,
        args=(send_conn, task_id, step_id, run_id, threads),
        name=f"OfflineWorker-{task_id}/{step_id}/{run_id}",
        daemon=True,
    )
    try:
        proc.start()
    except BaseException:
        recv_conn.close()
        send_conn.close()
        raise
    # 父进程不能留着写端：否则子进程退出后父进程永远读不到 EOF。
    send_conn.close()
    return WorkerProcess(proc, recv_conn)


class OfflineJobService:
    """离线作业服务。`spawner` / `config` / `sessions` 仅供测试注入（假子进程 / 推理配置 / 注册表）。"""

    def __init__(
        self,
        *,
        spawner: Callable[..., WorkerProcess] = _spawn,
        config: Optional[InferenceConfig] = None,
        sessions=None,
        job_timeout_s: float = JOB_TIMEOUT_S,
        poll_s: float = POLL_S,
    ) -> None:
        if sessions is None:
            # 函数体内 import：同 RecordingService，别把 session → numpy 链摊给每个 import 本模块的人
            from app.services.session.instance import session_service

            sessions = session_service
        self._sessions = sessions
        self._spawner = spawner
        self._config = config  # None = 提交时读 load_stage_config 单例
        self._job_timeout_s = job_timeout_s
        self._poll_s = poll_s

        # SerialTaskQueue 是一次性的，在 start() 里建（同 RecordingService）。
        self._queue: Optional[SerialTaskQueue] = None
        # 下面四个字段都由 `_lock` 保护：路由线程（submit / cancel / get）与队列线程（_execute）都碰。
        # 「起子进程」与「取消 + kill」在同一把锁下，故取消不会漏掉刚起的子进程。
        self._lock = threading.Lock()
        self._jobs: "OrderedDict[RunIdentity, OfflineJob]" = OrderedDict()
        self._running: Optional[Tuple[OfflineJob, WorkerProcess]] = None  # (job, handle)
        self._abort: Optional[Tuple[str, str]] = None                      # 运行中 job 的终止原因 (status, message)
        self._stopping = False

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            self._stopping = False
        self._queue = SerialTaskQueue("offline", maxsize=QUEUE_SIZE)
        self._queue.start()
        logger.info("[offline] 作业服务已启动")
        self._preflight_materials()

    def _preflight_materials(self) -> None:
        """启动时核对激活策略的物料：缺失只告警（不影响在线，提交该 step 的作业时才会失败）。"""
        try:
            from .segmentation.materials import check_materials

            config = self._config if self._config is not None else load_stage_config()
            missing = [s for s in check_materials(config) if not s.exists]
        except Exception as e:
            logger.warning("[offline] 物料预检未完成: %s", e)
            return
        for s in missing:
            logger.warning("[offline] 离线物料缺失 | stage=%s | producer=%s | %s", s.stage, s.producer, s.path)

    def stop(self, timeout: float = 10.0) -> None:
        """kill 运行中的子进程，排队中的在排空时逐个记 cancelled（不会被「排空」拖住）。"""
        with self._lock:
            self._stopping = True
            if self._running is not None:
                self._abort_locked(CANCELLED, "服务停机")
        if self._queue is not None:
            self._queue.stop(timeout=timeout)
            self._queue = None
        logger.info("[offline] 作业服务已停止")

    # ── 对外 ────────────────────────────────────────────────────────────────────

    def require_offline(self, step_id: int) -> None:
        """step 未配置 / 无离线模型 → ValidationError（400）。路由在解析 run 之前先调，参数错误先于 404。"""
        self._stage_config().require_offline(step_id)

    def submit(self, run: RunIdentity) -> OfflineJob:
        task_id, step_id = run.task_id, run.step_id
        self.require_offline(step_id)
        queue = self._queue
        if queue is None:
            raise ConflictError("离线作业服务未启动", task_id=task_id, step_id=step_id)
        live = self._sessions.get(task_id)
        if live is not None and live.info.run == run:
            # 输入（detections.jsonl）还在写：按提交时已落盘的部分出结论就是错的
            raise ConflictError(
                f"该 run 正在运行，停止后再提交离线作业: task={task_id} step={step_id} run={run.run_id}",
                task_id=task_id, step_id=step_id, resource_type="run",
            )
        with self._lock:
            current = self._jobs.get(run)
            if current is not None and current.status in _ACTIVE:
                return replace(current)
            job = OfflineJob(task_id=task_id, step_id=step_id, run_id=run.run_id)
            self._jobs[run] = job
            self._jobs.move_to_end(run)
            self._trim_locked()

        label = f"offline:{task_id}/{step_id}/{run.run_id}"
        if not queue.submit(lambda: self._execute(job), label=label):
            with self._lock:
                self._finish_locked(job, FAILED, "队列已满或已停机，未入队")
            raise ConflictError(
                f"离线队列已满（{QUEUE_SIZE}），稍后再试",
                task_id=task_id, step_id=step_id, resource_type="offline_job",
            )
        logger.info("[offline] 已入队 task=%s step=%s run=%s", task_id, step_id, run.run_id)
        return replace(job)

    def get(self, run: RunIdentity) -> Optional[OfflineJob]:
        with self._lock:
            job = self._jobs.get(run)
            return replace(job) if job is not None else None

    def list_jobs(self) -> List[OfflineJob]:
        with self._lock:
            return [replace(job) for job in self._jobs.values()]

    def cancel(self, run: RunIdentity) -> bool:
        """返回是否取消了一个在途 job。"""
        with self._lock:
            job = self._jobs.get(run)
            if job is None or job.status not in _ACTIVE:
                return False
            if job.status == QUEUED:
                self._finish_locked(job, CANCELLED, "已取消")
            else:
                self._abort_locked(CANCELLED, "已取消")
            return True

    # ── 队列任务体 ─────────────────────────────────────────────────────────────

    def _execute(self, job: OfflineJob) -> None:
        """在队列线程上跑一个 job：起子进程 → 等结果 → 收口状态。异常不出本函数。"""
        handle: Optional[WorkerProcess] = None
        try:
            with self._lock:
                if job.status != QUEUED:  # 排队期间已被取消
                    return
                if self._stopping:
                    self._finish_locked(job, CANCELLED, "服务停机")
                    return
                try:
                    handle = self._spawner(job.task_id, job.step_id, job.run_id, THREADS)
                except Exception as e:
                    self._finish_locked(job, FAILED, f"子进程启动失败: {e}")
                    return
                job.status = RUNNING
                job.started_at = time.time()
                self._running = (job, handle)
                self._abort = None

            logger.info(
                "[offline] 开始 task=%s step=%s run=%s pid=%s",
                job.task_id, job.step_id, job.run_id, handle.pid,
            )
            result, exitcode = self._watch(handle)
            with self._lock:
                abort, self._abort, self._running = self._abort, None, None
                if abort is not None:
                    self._finish_locked(job, *abort)
                else:
                    self._finish_from_result(job, exitcode, result)
        except Exception as e:  # 兜底：job 不能卡在 running
            logger.error("[offline] 执行异常 task=%s step=%s: %s", job.task_id, job.step_id, e, exc_info=True)
            with self._lock:
                self._running = None
                if job.status in _ACTIVE:
                    self._finish_locked(job, FAILED, f"执行异常: {e}")
        finally:
            if handle is not None:
                handle.close()
            logger.info(
                "[offline] 结束 task=%s step=%s status=%s %s",
                job.task_id, job.step_id, job.status, job.message,
            )

    def _watch(self, handle: WorkerProcess) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
        """等子进程退出并取回结果；超时 kill，kill 后给有限宽限回收。

        子进程存活期间持续 poll 结果通道：结果（含长 message）不会让子进程堵在 send 上。
        取消与停机由调用方直接 kill（见 `_abort_locked`），其终止原因优先于结果。
        """
        deadline = time.monotonic() + self._job_timeout_s
        result: Optional[Dict[str, Any]] = None
        while handle.is_alive():
            if result is None and not handle.at_eof:
                result = handle.poll_result(self._poll_s)
            else:
                handle.join(self._poll_s)
            if not handle.is_alive():
                break
            if time.monotonic() < deadline:
                continue
            with self._lock:
                self._abort_locked(FAILED, f"超时（>{self._job_timeout_s:.0f}s）")
            if not self._join_within(handle, KILL_GRACE_S):
                logger.error(
                    "[offline] 子进程 kill 后 %.0fs 未退出，放弃回收并先收口 pid=%s",
                    KILL_GRACE_S, handle.pid,
                )
                return result, None
            break
        handle.join(0)
        if result is None and not handle.at_eof:
            result = handle.poll_result(0.1)  # 退出后把已缓冲的结果读完
        return result, handle.exitcode

    def _join_within(self, handle: WorkerProcess, timeout: float) -> bool:
        """在 timeout 秒内等子进程退出；返回是否已退出。"""
        deadline = time.monotonic() + timeout
        while handle.is_alive():
            handle.join(max(0.0, min(self._poll_s, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                return not handle.is_alive()
        return True

    def _finish_from_result(
        self, job: OfflineJob, exitcode: Optional[int], result: Optional[Dict[str, Any]],
    ) -> None:
        """按「有效结果 + 正常退出」登记 Worker 状态，其余一律 failed。"""
        if result is not None:
            status = result.get("status")
            if exitcode == 0 and status in _SUCCESS_STATUSES:
                job.producer = result.get("producer")
                job.segment_count = int(result.get("segment_count") or 0)
                alarm_count = result.get("alarm_count")
                job.alarm_count = None if alarm_count is None else int(alarm_count)
                self._finish_locked(job, status, str(result.get("message") or ""))
                return
            message = str(result.get("message") or "")
            self._finish_locked(job, FAILED, message or f"子进程退出码 {exitcode}")
            return
        if exitcode is None:
            self._finish_locked(job, FAILED, "子进程未退出，未回传结果")
            return
        self._finish_locked(job, FAILED, f"子进程异常退出（exitcode={exitcode}），未回传结果")

    # ── 锁内小工具（调用方持 `_lock`）────────────────────────────────────────────

    def _abort_locked(self, status: str, message: str) -> None:
        """给运行中的 job 记终止原因并 kill；已有原因则不覆盖（先到先得）。"""
        if self._running is None or self._abort is not None:
            return
        self._abort = (status, message)
        self._running[1].kill()

    @staticmethod
    def _finish_locked(job: OfflineJob, status: str, message: str) -> None:
        job.status = status
        job.message = message
        job.finished_at = time.time()

    def _trim_locked(self) -> None:
        finished = [k for k, j in self._jobs.items() if j.status not in _ACTIVE]
        for key in finished[: max(0, len(finished) - HISTORY)]:
            del self._jobs[key]

    def _stage_config(self) -> InferenceConfig:
        return self._config if self._config is not None else load_stage_config()
