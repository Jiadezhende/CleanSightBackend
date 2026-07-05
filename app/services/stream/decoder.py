"""
FFmpeg 解码器 - 负责从 RTSP 流解码视频帧
"""

import logging
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from app.domain.frame import Frame
from app.services.stream.config import DecoderConfig
from app.settings import settings
from app.utils.exceptions import FFmpegError, StreamConnectionError
from app.utils.metrics import frame_drop_total

DEFAULT_CHANNELS = 3

# RTSP 输入固定选项（系统只用 RTSP）：UDP 传输 + 低延迟 + 容错解析。
# 作为 ffmpeg 命令的固定前缀，见 _build_cmd。
_RTSP_INPUT_OPTS = [
    "-rtsp_transport", "udp",
    "-fflags", "nobuffer+discardcorrupt",
    "-flags", "low_delay",
    "-err_detect", "ignore_err",
    "-analyzeduration", "1000000",
    "-probesize", "1000000",
]


class FFmpegDecoder:
    def __init__(
        self,
        manager,
        task_id: int,     # 运行键（路由标识）
        stream_url: str,
        decoder_config: Optional[DecoderConfig] = None,
        client_queues=None,
    ):
        self.manager = manager
        self.task_id = task_id
        self.stream_url = stream_url

        # 使用配置对象（如果未提供则使用默认配置）
        self.config = decoder_config or DecoderConfig()
        self.width = self.config.default_width
        self.height = self.config.default_height
        self.fps = self.config.default_fps
        self.pix_fmt = self.config.pix_fmt
        self.chunk_read_size = self.config.chunk_read_size
        self.backpressure_ratio = self.config.backpressure_ratio

        # 客户端队列实例（用于直接写入队列）
        self.client_queues = client_queues

        self.frame_size = self.width * self.height * DEFAULT_CHANNELS
        self.buffer = bytearray()
        self.proc: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._stderr_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        # metrics
        self.frames_received = 0
        self.frames_dropped = 0
        self.frames_written_to_raw = 0  # 新增：写入 CA-Raw-Queue 的帧数
        self.frames_written_to_ready = 0  # 新增：写入 CA-Ready-Queue 的帧数
        self.logger = logging.getLogger(
            f"app.services.stream.decoder.FFmpegDecoder.{self.task_id}"
        )

    def _build_cmd(self):
        cmd = [settings.ffmpeg_path]
        cmd += _RTSP_INPUT_OPTS
        cmd += [
            "-i",
            self.stream_url,
            "-map",
            "0:v:0",
            "-vsync",
            "drop",
            "-vf",
            f"scale={self.width}:{self.height},fps={self.fps}",
            "-pix_fmt",
            self.pix_fmt,
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        return cmd

    def _err_identity(self) -> dict:
        """异常身份三元组：task_id（路由）+ step_id/source_ip（从 cq 派生，None 守卫）。"""
        cq = self.client_queues
        return {
            "task_id": self.task_id,
            "step_id": cq.step_id if cq else None,
            "source_ip": cq.source_ip if cq else None,
        }

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._stop_event.clear()
            self.buffer = bytearray()
            cmd = self._build_cmd()
            try:
                self.logger.info("starting ffmpeg: %s", " ".join(cmd))
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
                )
                self.logger.info(
                    "ffmpeg started pid=%s", getattr(self.proc, "pid", None)
                )
            except FileNotFoundError:
                self.logger.exception("ffmpeg binary not found")
                raise FFmpegError(
                    message=f"FFmpeg binary not found: {settings.ffmpeg_path}",
                    **self._err_identity(),
                )

            # 快速检查进程是否立即崩溃
            time.sleep(0.1)  # 给进程一点启动时间
            if self.proc.poll() is not None:
                # 进程已经退出 — 此时 stderr 管道已关闭，可安全同步读取；
                # 同步 wait() 回收僵尸（poll 已非 None，wait 立即返回），避免
                # 秒退进程滞留为僵尸等 GC。
                exit_code = self.proc.returncode
                try:
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass
                try:
                    stderr_output = (
                        self.proc.stderr.read().decode("utf-8", errors="ignore").strip()
                        if self.proc.stderr
                        else ""
                    )
                except Exception:
                    stderr_output = ""
                # 区分"流暂不可用"（可重试）与"FFmpeg 真实崩溃"（致命）
                _TRANSIENT_MARKERS = (
                    "404",
                    "not found",
                    "connection refused",
                    "connection timed out",
                    "no route to host",
                    "network unreachable",
                )
                is_transient = any(
                    m in stderr_output.lower() for m in _TRANSIENT_MARKERS
                )
                # 最后一行非空行作为摘要，完整 stderr 降至 DEBUG
                last_line = next(
                    (l for l in reversed(stderr_output.splitlines()) if l.strip()), ""
                )
                self.logger.debug("FFmpeg stderr:\n%s", stderr_output)
                if is_transient:
                    self.logger.debug("FFmpeg: stream not available — %s", last_line)
                    raise StreamConnectionError(
                        url=self.stream_url,
                        **self._err_identity(),
                        details=last_line or None,
                    )
                self.logger.error(
                    "FFmpeg exited (code=%s): %s", exit_code, last_line or "(no output)"
                )
                raise FFmpegError(
                    message=f"FFmpeg process failed to start (exit_code={exit_code})",
                    **self._err_identity(),
                    exit_code=exit_code,
                )

            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop,
                daemon=True,
                name=f"stderr-{self.task_id}",
            )
            self._stderr_thread.start()

            # decoder 自持读线程（Windows/POSIX 统一：阻塞读 stdout）
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                daemon=True,
                name=f"reader-{self.task_id}",
            )
            self._reader_thread.start()

            self.logger.debug("decoder start complete")

    def stop(self, wait: float = 2.0):
        reader_thread = None
        stderr_thread = None
        with self.lock:
            self._stop_event.set()
            if self.proc is None:
                return
            reader_thread = self._reader_thread
            stderr_thread = self._stderr_thread
            try:
                # 无条件 SIGKILL + wait 回收：poll 已结束的进程 wait 立即返回，
                # 不再把僵尸留给 GC/subprocess._cleanup。直接 SIGKILL 依据：
                # 1) 此 ffmpeg 仅解码到 pipe（不写文件），强杀无产物损坏风险；
                # 2) RTSP 对端是自有 mediamtx_gateway，对断连(RST)会超时回收会话，不需优雅 TEARDOWN；
                # 3) 拆流多在流已不健康时发生，此时 ffmpeg 卡在死 socket 读上、收不到 SIGTERM，
                #    优雅等待只会白耗满 wait 秒（实测 ~2s）后照样 kill。
                if self.proc.poll() is None:
                    self.logger.info(
                        "killing ffmpeg pid=%s", getattr(self.proc, "pid", None)
                    )
                    self.proc.kill()
                try:
                    self.proc.wait(timeout=wait)  # reap 防僵尸；SIGKILL 必达，通常 ~ms 返回
                except Exception:
                    self.logger.warning(
                        "ffmpeg pid=%s not reaped within %ss after SIGKILL",
                        getattr(self.proc, "pid", None), wait,
                    )
                try:
                    if self.proc.stdout:
                        self.proc.stdout.close()
                except Exception:
                    pass
                try:
                    if self.proc.stderr:
                        self.proc.stderr.close()
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self.proc = None
                self._reader_thread = None
                self._stderr_thread = None
                self.logger.info("decoder stopped")

        # 锁外 join reader + stderr 两线程（对称回收）：管道已关，两个读循环会读到
        # EOF/异常后退出。放锁外避免与持 self.lock 的其它路径互等；不 join 自身线程。
        current = threading.current_thread()
        for t in (reader_thread, stderr_thread):
            if t is not None and t is not current:
                t.join(timeout=wait)

    def is_alive(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def _read_stderr_loop(self):
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if self._stop_event.is_set():
                    break
                try:
                    line_str = line.decode("utf-8", errors="ignore").strip()
                    if line_str:
                        self.logger.debug("ffmpeg stderr: %s", line_str)
                except Exception:
                    pass  # 单行解码失败可忽略
        except ValueError:
            pass  # pipe closed during stop() — expected on reconnect/shutdown
        except Exception:
            self.logger.error("stderr reader loop crashed", exc_info=True)

    def _reader_loop(self):
        """decoder 自持读循环（Windows/POSIX 统一）：阻塞读 stdout 直到 EOF/stop。

        起始处捕获 stdout 本地引用，避免 stop() 将 self.proc 置 None 造成的 TOCTOU；
        管道被 stop() 关闭时 read 抛 ValueError（closed file）或返回 b""，均视为流结束正常退出，
        由 StreamHealthMonitor 决定是否重连，本循环不自动重启。
        """
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            while not self._stop_event.is_set():
                chunk = stdout.read(self.chunk_read_size)
                if not chunk:
                    self.logger.debug("stream ended")
                    break
                self.buffer.extend(chunk)
                self._process_frames()
        except ValueError:
            pass  # pipe closed during stop() — expected on reconnect/shutdown
        except Exception:
            self.logger.exception("reader loop error")

    def _should_drop_frame(self, pending_count: int, queue_capacity: int) -> bool:
        """
        背压逻辑：根据 CA-Ready-Queue 的占用率决定是否丢帧

        Args:
            pending_count: 当前队列中帧数
            queue_capacity: 队列最大容量

        Returns:
            True 表示应该丢帧，False 表示可以写入
        """
        if queue_capacity <= 0:
            return False  # 无限队列，不丢帧

        ratio = pending_count / float(queue_capacity)
        return ratio >= self.backpressure_ratio

    def _process_frames(self):
        """
        处理帧数据：解码 → 背压检测 → 写入队列

        架构改进：
        1. 直接从 buffer 解析完整帧
        2. 根据 CA-Ready-Queue（推理队列）的占用率决定是否丢帧
        3. 写入两个队列：
           - CA-Raw-Queue: 原始帧（用于落盘生成 HLS）
           - CA-Ready-Queue: 原始帧（用于推理）
        """
        while len(self.buffer) >= self.frame_size:
            frame_bytes = bytes(self.buffer[: self.frame_size])
            del self.buffer[: self.frame_size]

            try:
                # 1. 解析帧
                arr = np.frombuffer(frame_bytes, dtype=np.uint8)
                std = arr.reshape((self.height, self.width, DEFAULT_CHANNELS))

                # 2. 背压检测（检查 CA-Ready-Queue 而非 CA-Raw-Queue）
                pending_count = self.manager.get_pending_count(self.task_id)

                # 获取队列容量
                queue_capacity = 0
                if self.client_queues is not None:
                    queue_capacity = self.client_queues.get_ca_ready_capacity()

                # 判断是否应该丢帧（仅针对推理队列）
                drop_inference = self._should_drop_frame(pending_count, queue_capacity)
                if drop_inference:
                    self.frames_dropped += 1
                    frame_drop_total.labels(reason="ingress_backpressure").inc()
                    # 仅在每100帧打印一次（避免日志洪水），使用 DEBUG 级别
                    if self.frames_dropped % 100 == 0:
                        self.logger.debug(
                            "[BACKPRESSURE] dropping inference frame (pending=%s/%s, dropped=%s)",
                            pending_count,
                            queue_capacity,
                            self.frames_dropped,
                        )

                # 3. 写入队列（如果 client_queues 可用）
                if self.client_queues is not None:
                    now = time.time()
                    frame_data_obj = Frame(timestamp=now, frame=std)

                    # 3.1 写入原始队列（全帧率，用于落盘；背压时也写入，保证 HLS 录制完整）
                    if self.client_queues.append_ca_raw(frame_data_obj):
                        self.frames_written_to_raw += 1

                    # 3.2 写入推理队列（降频；背压时跳过）
                    if not drop_inference:
                        if self.client_queues.append_ca_ready_with_throttle(frame_data_obj):
                            self.frames_written_to_ready += 1

                self.frames_received += 1

                # log every N frames to observe liveness
                if self.frames_received % 300 == 0:
                    self.logger.info(
                        "received %s frames (raw=%s, ready=%s, dropped=%s)",
                        self.frames_received,
                        self.frames_written_to_raw,
                        self.frames_written_to_ready,
                        self.frames_dropped,
                    )
            except Exception:
                self.frames_dropped += 1
                frame_drop_total.labels(reason="decode_error").inc()
                self.logger.exception("error processing frame bytes")
