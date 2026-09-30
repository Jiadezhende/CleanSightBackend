"""离线作业服务 —— 离线推理的提交、串行执行与状态查询。

    start() / stop()                          生命周期
    submit(run) -> OfflineJob                 入队；step 未配离线模型抛 ValidationError；该 run 正在运行 /
                                              队满抛 ConflictError；同一 run 在途返回在途那个
    get(run) -> OfflineJob|None               状态快照
    list_jobs() -> List[OfflineJob]           在途 + 最近结束的，按提交序
    cancel(run) -> bool                       排队中的直接取消，运行中的 kill 子进程

执行：一条 `SerialTaskQueue`（一次只跑一个），每个 job 起子进程
`python -m app.services.inference.offline.cli run`（CPU 隔离 + 降优先级 + 可 kill），解析其 stdout 末行 JSON。
**本模块不 import runner / torch**：CLI 只作为子进程命令出现。

作业锁定一个 run（`RunIdentity`，由调用方经 `runs.query` 解析后传入）：子进程总带 `--run-id`，
不同 run 各跑各的；该 run 就是 `clients` 当前注册的 CQ 的 run → 409（输入还在写）。
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

from app.types.run import RunIdentity
from app.services.inference.config import InferenceConfig, load_stage_config
from app.types.exceptions import ConflictError
from app.services.utils.task_queue import SerialTaskQueue

logger = logging.getLogger(__name__)

__all__ = ["OfflineJob", "OfflineJobService"]

THREADS = 2              # 子进程 torch 线程数
QUEUE_SIZE = 20          # 排队上限（不含运行中的那个）
JOB_TIMEOUT_S = 1800.0   # 单个 job 墙钟上限，超时 kill → failed
HISTORY = 200            # 已结束的 job 最多保留几条
POLL_S = 1.0             # 监视子进程的间隔：超时最多延迟这么久被发现

_CLI_MODULE = "app.services.inference.offline.cli"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_STDERR_TAIL_CHARS = 2000

QUEUED, RUNNING = "queued", "running"
COMPLETED, SKIPPED, RECLAIMED = "completed", "skipped", "reclaimed"
FAILED, CANCELLED = "failed", "cancelled"
_ACTIVE = (QUEUED, RUNNING)
_RUNNER_STATUSES = (COMPLETED, SKIPPED, RECLAIMED)  # CLI 退出码 0 时的合法结果


@dataclass
class OfflineJob:
    """一个 run 的离线作业。对外只给快照（`replace` 出来的副本）。"""

    task_id: int
    step_id: int
    run_id: int
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
    """离线作业服务。`launcher` / `config` / `clients` 仅供测试注入（假 Popen / 推理配置 / 注册表）。"""

    def __init__(
        self,
        *,
        launcher: Callable[..., Any] = subprocess.Popen,
        config: Optional[InferenceConfig] = None,
        clients=None,
        job_timeout_s: float = JOB_TIMEOUT_S,
        poll_s: float = POLL_S,
    ) -> None:
        if clients is None:
            # 函数体内 import：同 RecordingService，别把 client → numpy 链摊给每个 import 本模块的人
            from app.services.client.instance import client_service

            clients = client_service
        self._clients = clients
        self._launcher = launcher
        self._config = config  # None = 提交时读 load_stage_config 单例
        self._job_timeout_s = job_timeout_s
        self._poll_s = poll_s

        # SerialTaskQueue 是一次性的，在 start() 里建（同 RecordingService）。
        self._queue: Optional[SerialTaskQueue] = None
        # 下面四个字段都由 `_lock` 保护：路由线程（submit / cancel / get）与队列线程（_execute）都碰。
        # 「起子进程」与「取消 + kill」在同一把锁下，故取消不会漏掉刚起的子进程。
        self._lock = threading.Lock()
        self._jobs: "OrderedDict[RunIdentity, OfflineJob]" = OrderedDict()
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

    def require_offline(self, step_id: int) -> None:
        """step 未配置 / 无离线模型 → ValidationError（400）。路由在解析 run 之前先调，参数错误先于 404。"""
        self._stage_config().require_offline(step_id)

    def submit(self, run: RunIdentity) -> OfflineJob:
        task_id, step_id = run.task_id, run.step_id
        self.require_offline(step_id)
        queue = self._queue
        if queue is None:
            raise ConflictError("离线作业服务未启动", task_id=task_id, step_id=step_id)
        live = self._clients.get(task_id)
        if live is not None and live.run == run:
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

            logger.info(
                "[offline] 开始 task=%s step=%s run=%s pid=%s",
                job.task_id, job.step_id, job.run_id, proc.pid,
            )
            self._watch(proc)
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

    def _watch(self, proc) -> None:
        """等子进程退出；超时 kill。取消与停机由调用方直接 kill。"""
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

    def _stage_config(self) -> InferenceConfig:
        return self._config if self._config is not None else load_stage_config()


# ── 子进程 ─────────────────────────────────────────────────────────────────────


def _command(job: OfflineJob) -> List[str]:
    cmd = [
        sys.executable, "-m", _CLI_MODULE, "run",
        "--task-id", str(job.task_id), "--step-id", str(job.step_id), "--run-id", str(job.run_id),
        "--threads", str(THREADS),
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
