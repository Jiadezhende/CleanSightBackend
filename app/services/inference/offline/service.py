"""离线作业服务 —— 离线推理的提交、串行执行与状态查询。

    start() / stop()                          生命周期
    submit(task_id, step_id) -> OfflineJob    入队；step 正在 live / 队满抛 ConflictError；同键在途返回在途那个
    get(task_id, step_id) -> OfflineJob|None  状态快照
    list_jobs() -> List[OfflineJob]           在途 + 最近结束的，按提交序
    cancel(task_id, step_id) -> bool          排队中的直接取消，运行中的 kill 子进程

执行：一条 `SerialTaskQueue`（一次只跑一个），每个 job 起子进程
`python -m app.services.inference.offline.cli run --strict --json`（CPU 隔离 + 降优先级 + 可 kill）。
**本模块不 import runner / torch**：CLI 只作为子进程命令出现。

结果正确性（换代 / 未封口）由 runner 的输入戳校验保证；本服务的 live 检查只为少白算。
状态只在内存，重启丢失（结果本身已落盘）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.utils.exceptions import ConflictError
from app.utils.task_queue import SerialTaskQueue

logger = logging.getLogger(__name__)

__all__ = ["OfflineJob", "OfflineJobService"]

THREADS = 2              # 子进程 torch 线程数
QUEUE_SIZE = 20          # 排队上限（不含运行中的那个）
JOB_TIMEOUT_S = 1800.0   # 单个 job 墙钟上限，超时 kill → failed
HISTORY = 200            # 已结束的 job 最多保留几条
POLL_S = 1.0             # 监视子进程的间隔：取消 / 超时 / 换代最多延迟这么久被发现

_CLI_MODULE = "app.services.inference.offline.cli"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_STDERR_TAIL_CHARS = 2000

QUEUED, RUNNING = "queued", "running"
COMPLETED, SKIPPED, SUPERSEDED = "completed", "skipped", "superseded"
FAILED, CANCELLED = "failed", "cancelled"
_ACTIVE = (QUEUED, RUNNING)
_RUNNER_STATUSES = (COMPLETED, SKIPPED, SUPERSEDED)  # CLI 退出码 0 时的合法结果


@dataclass
class OfflineJob:
    """一个 (task_id, step_id) 的离线作业。对外只给快照（`replace` 出来的副本）。"""

    task_id: int
    step_id: int
    status: str = QUEUED
    producer: Optional[str] = None
    segment_count: int = 0
    message: str = ""
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OfflineJobService:
    """离线作业服务。`clients` / `launcher` 仅供测试注入（假注册表 / 假 Popen）。"""

    def __init__(
        self,
        *,
        clients=None,
        launcher: Callable[..., Any] = subprocess.Popen,
        job_timeout_s: float = JOB_TIMEOUT_S,
        poll_s: float = POLL_S,
    ) -> None:
        if clients is None:
            # 函数体内 import：同 RecordingService，免得 import 本模块就拉起 client → numpy。
            from app.services.client.manager import client_manager

            clients = client_manager
        self._clients = clients
        self._launcher = launcher
        self._job_timeout_s = job_timeout_s
        self._poll_s = poll_s

        # SerialTaskQueue 是一次性的，在 start() 里建（同 RecordingService）。
        self._queue: Optional[SerialTaskQueue] = None
        # 下面四个字段都由 `_lock` 保护：路由线程（submit / cancel / get）与队列线程（_execute）都碰。
        # 「查 live + 起子进程」与「取消 + kill」在同一把锁下，故取消不会漏掉刚起的子进程。
        self._lock = threading.Lock()
        self._jobs: "OrderedDict[Tuple[int, int], OfflineJob]" = OrderedDict()
        self._running: Optional[Tuple[OfflineJob, Any]] = None  # (job, proc)
        self._abort: Optional[Tuple[str, str]] = None           # 运行中 job 的终止原因 (status, message)
        self._stopping = False

    # ── 生命周期 ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            self._stopping = False
        self._queue = SerialTaskQueue("offline", maxsize=QUEUE_SIZE)
        self._queue.start()
        logger.info("[offline] 作业服务已启动")

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

    def submit(self, task_id: int, step_id: int) -> OfflineJob:
        queue = self._queue
        if queue is None:
            raise ConflictError("离线作业服务未启动", task_id=task_id, step_id=step_id)
        if self._is_live(task_id, step_id):
            raise ConflictError(
                f"task {task_id} step {step_id} 正在运行，检测结果未封口",
                task_id=task_id, step_id=step_id, resource_type="offline_job",
            )
        key = (task_id, step_id)
        with self._lock:
            current = self._jobs.get(key)
            if current is not None and current.status in _ACTIVE:
                return replace(current)
            job = OfflineJob(task_id=task_id, step_id=step_id)
            self._jobs[key] = job
            self._jobs.move_to_end(key)
            self._trim_locked()

        if not queue.submit(lambda: self._execute(job), label=f"offline:{task_id}/{step_id}"):
            with self._lock:
                self._finish_locked(job, FAILED, "队列已满或已停机，未入队")
            raise ConflictError(
                f"离线队列已满（{QUEUE_SIZE}），稍后再试",
                task_id=task_id, step_id=step_id, resource_type="offline_job",
            )
        logger.info("[offline] 已入队 task=%s step=%s", task_id, step_id)
        return replace(job)

    def get(self, task_id: int, step_id: int) -> Optional[OfflineJob]:
        with self._lock:
            job = self._jobs.get((task_id, step_id))
            return replace(job) if job is not None else None

    def list_jobs(self) -> List[OfflineJob]:
        with self._lock:
            return [replace(job) for job in self._jobs.values()]

    def cancel(self, task_id: int, step_id: int) -> bool:
        """返回是否取消了一个在途 job。"""
        with self._lock:
            job = self._jobs.get((task_id, step_id))
            if job is None or job.status not in _ACTIVE:
                return False
            if job.status == QUEUED:
                self._finish_locked(job, CANCELLED, "已取消")
            else:
                self._abort_locked(CANCELLED, "已取消")
            return True

    # ── 队列任务体 ─────────────────────────────────────────────────────────────

    def _execute(self, job: OfflineJob) -> None:
        """在队列线程上跑一个 job：起子进程 → 监视 → 解析结果。异常不出本函数。"""
        stdout = tempfile.TemporaryFile()
        stderr = tempfile.TemporaryFile()
        try:
            with self._lock:
                if job.status != QUEUED:  # 排队期间已被取消
                    return
                if self._stopping:
                    self._finish_locked(job, CANCELLED, "服务停机")
                    return
                if self._is_live(job.task_id, job.step_id):
                    self._finish_locked(job, SKIPPED, "该 step 正在运行，检测结果未封口")
                    return
                try:
                    proc = self._launcher(
                        _command(job), cwd=str(_REPO_ROOT), env=_child_env(),
                        stdout=stdout, stderr=stderr, **_priority_kwargs(),
                    )
                except Exception as e:
                    self._finish_locked(job, FAILED, f"子进程启动失败: {e}")
                    return
                job.status = RUNNING
                job.started_at = time.time()
                self._running = (job, proc)
                self._abort = None

            logger.info("[offline] 开始 task=%s step=%s pid=%s", job.task_id, job.step_id, proc.pid)
            self._watch(job, proc)
            with self._lock:
                abort, self._abort, self._running = self._abort, None, None
                if abort is not None:
                    self._finish_locked(job, *abort)
                else:
                    self._finish_from_output(job, proc.returncode, _read(stdout), _read(stderr))
        except Exception as e:  # 兜底：job 不能卡在 running
            logger.error("[offline] 执行异常 task=%s step=%s: %s", job.task_id, job.step_id, e, exc_info=True)
            with self._lock:
                self._running = None
                if job.status in _ACTIVE:
                    self._finish_locked(job, FAILED, f"执行异常: {e}")
        finally:
            stdout.close()
            stderr.close()
            logger.info(
                "[offline] 结束 task=%s step=%s status=%s %s",
                job.task_id, job.step_id, job.status, job.message,
            )

    def _watch(self, job: OfflineJob, proc) -> None:
        """等子进程退出；超时 / 该 step 重新 live 时 kill。取消与停机由调用方直接 kill。"""
        deadline = time.monotonic() + self._job_timeout_s
        while True:
            try:
                proc.wait(timeout=self._poll_s)
                return
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                with self._lock:
                    self._abort_locked(FAILED, f"超时（>{self._job_timeout_s:.0f}s）")
            elif self._is_live(job.task_id, job.step_id):
                with self._lock:
                    self._abort_locked(SUPERSEDED, "该 step 已开始新一轮运行，终止")

    def _finish_from_output(self, job: OfflineJob, returncode: int, out: str, err: str) -> None:
        result = _last_json(out)
        if returncode == 0 and result is not None and result.get("status") in _RUNNER_STATUSES:
            job.producer = result.get("producer")
            job.segment_count = int(result.get("segment_count") or 0)
            self._finish_locked(job, result["status"], str(result.get("message") or ""))
            return
        message = (result or {}).get("message") or err[-_STDERR_TAIL_CHARS:].strip()
        self._finish_locked(job, FAILED, message or f"子进程退出码 {returncode}")

    # ── 锁内小工具（调用方持 `_lock`）────────────────────────────────────────────

    def _abort_locked(self, status: str, message: str) -> None:
        """给运行中的 job 记终止原因并 kill；已有原因则不覆盖（先到先得）。"""
        if self._running is None or self._abort is not None:
            return
        self._abort = (status, message)
        try:
            self._running[1].kill()
        except OSError:
            pass  # 已自行退出

    @staticmethod
    def _finish_locked(job: OfflineJob, status: str, message: str) -> None:
        job.status = status
        job.message = message
        job.finished_at = time.time()

    def _trim_locked(self) -> None:
        finished = [k for k, j in self._jobs.items() if j.status not in _ACTIVE]
        for key in finished[: max(0, len(finished) - HISTORY)]:
            del self._jobs[key]

    def _is_live(self, task_id: int, step_id: int) -> bool:
        cq = self._clients.get(task_id)
        return cq is not None and cq.step_id == step_id


# ── 子进程 ─────────────────────────────────────────────────────────────────────


def _command(job: OfflineJob) -> List[str]:
    cmd = [
        sys.executable, "-m", _CLI_MODULE, "run",
        "--task-id", str(job.task_id), "--step-id", str(job.step_id),
        "--strict", "--json", "--threads", str(THREADS),
    ]
    nice = shutil.which("nice") if os.name != "nt" else None
    return [nice, "-n", "15", *cmd] if nice else cmd


def _priority_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}
    return {}  # POSIX 走命令前缀 `nice`（preexec_fn 在多线程进程里不安全）


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)  # 含 CLEANSIGHT_ENV 与已加载的 .env，子进程落到同一个存储根
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _read(f) -> str:
    f.seek(0)
    return f.read().decode("utf-8", errors="replace")


def _last_json(out: str) -> Optional[Dict[str, Any]]:
    for line in reversed(out.strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        return payload if isinstance(payload, dict) else None
    return None
