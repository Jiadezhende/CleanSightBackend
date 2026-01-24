"""
FFmpeg 解码器 - 负责从 RTMP/RTSP 流解码视频帧
"""

import os
import subprocess
import threading
import time
from typing import Optional
import logging
import numpy as np
import cv2

from app.services import ai
from app.models.frame import FrameData
from app.settings import settings

# 导入 ClientManager 单例（延迟导入避免循环依赖）
try:
    from app.services.client import client_manager
except ImportError:
    client_manager = None  # 兼容旧版本

# 配置常量
FFMPEG_BIN = os.environ.get("FFMPEG_PATH", "ffmpeg")
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_CHANNELS = 3
CHUNK_READ = 32768

# 背压阈值：推理队列容量的百分比（不是固定值）
# 当 CA-Ready-Queue 达到容量的 90% 时开始丢帧
PER_STREAM_MAX_PENDING_RATIO = 0.90  # 90% 容量


class FFmpegDecoder:
    def __init__(self, manager, client_id: str, stream_url: str, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=30, pix_fmt="bgr24", protocol_opts=None, auto_restart=True, max_restarts=5, client_queues=None):
        self.manager = manager
        self.client_id = client_id
        self.stream_url = stream_url
        self.width = width
        self.height = height
        self.fps = fps
        self.pix_fmt = pix_fmt
        self.protocol_opts = protocol_opts or []
        self.auto_restart = auto_restart
        self.max_restarts = max_restarts

        # 新增：客户端队列实例（用于直接写入队列）
        self.client_queues = client_queues

        self.frame_size = width * height * DEFAULT_CHANNELS
        self.buffer = bytearray()
        self.proc: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._stderr_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self.restart_count = 0
        self.lock = threading.Lock()

        # metrics
        self.frames_received = 0
        self.frames_dropped = 0
        self.frames_written_to_raw = 0  # 新增：写入 CA-Raw-Queue 的帧数
        self.frames_written_to_ready = 0  # 新增：写入 CA-Ready-Queue 的帧数
        self.logger = logging.getLogger(f"app.services.stream.decoder.FFmpegDecoder.{self.client_id}")

        # 活动追踪（用于超时清理）
        self.last_frame_time = time.time()  # 最后一次成功接收帧的时间
        self.last_restart_time = 0  # 最后一次尝试重启的时间

    def _build_cmd(self):
        cmd = [FFMPEG_BIN]
        cmd += self.protocol_opts
        cmd += [
            "-i", self.stream_url,
            "-map", "0:v:0",
            "-vsync", "drop",
            "-vf", f"scale={self.width}:{self.height},fps={self.fps}",
            "-pix_fmt", self.pix_fmt,
            "-f", "rawvideo",
            "pipe:1",
        ]
        return cmd

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._stop_event.clear()
            self.buffer = bytearray()
            cmd = self._build_cmd()
            try:
                self.logger.info("starting ffmpeg: %s", " ".join(cmd))
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                self.logger.info("ffmpeg started pid=%s", getattr(self.proc, 'pid', None))
            except FileNotFoundError:
                self.logger.exception("ffmpeg binary not found")
                raise RuntimeError(f"FFmpeg not found: {FFMPEG_BIN}")

            # set non-blocking on POSIX
            if os.name != "nt":
                try:
                    fd = self.proc.stdout.fileno()
                    os.set_blocking(fd, False)
                except Exception:
                    pass

            self._stderr_thread = threading.Thread(target=self._read_stderr_loop, daemon=True, name=f"stderr-{self.client_id}")
            self._stderr_thread.start()

            if os.name == "nt":
                self._reader_thread = threading.Thread(target=self._windows_reader_loop, daemon=True, name=f"reader-{self.client_id}")
                self._reader_thread.start()

            self.restart_count = 0
            self.last_frame_time = time.time()  # 重置最后帧时间
            self.logger.debug("decoder start complete")
    def stop(self, wait: float = 2.0):
        with self.lock:
            self._stop_event.set()

            # 新增：在关闭进程前注销selector
            # 这样可以避免selector尝试读取已关闭的fd
            if self.manager and hasattr(self.manager, 'sel') and self.manager.sel is not None:
                if self.proc and self.proc.stdout:
                    try:
                        fd = self.proc.stdout.fileno()
                        self.manager.sel.unregister(fd)
                        self.logger.debug("unregistered selector fd=%s", fd)
                    except (KeyError, ValueError, OSError):
                        pass  # fd已经注销或失效

            if self.proc is None:
                return
            try:
                if self.proc.poll() is None:
                    self.logger.info("terminating ffmpeg pid=%s", getattr(self.proc, 'pid', None))
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=wait)
                    except Exception:
                        self.logger.warning("ffmpeg did not exit gracefully, killing pid=%s", getattr(self.proc, 'pid', None))
                        self.proc.kill()
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
                self.logger.info("decoder stopped")

    def is_alive(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def _read_stderr_loop(self):
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            for line in iter(self.proc.stderr.readline, b''):
                if self._stop_event.is_set():
                    break
                try:
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    if line_str:
                        self.logger.debug("ffmpeg stderr: %s", line_str)
                except Exception:
                    pass
        except Exception:
            pass

    def _windows_reader_loop(self):
        """On Windows use a separate thread to read stdout (blocking)."""
        if self.proc is None or self.proc.stdout is None:
            return
        try:
            while not self._stop_event.is_set():
                chunk = self.proc.stdout.read(CHUNK_READ)
                if not chunk:
                    break
                self.buffer.extend(chunk)
                self._process_frames()
        except Exception:
            self.logger.exception("windows reader loop error")

    def on_stdout_ready(self):
        """
        This is called on POSIX systems when stdout is ready, from selector loop.
        We do a non-blocking read.
        """
        if self.proc is None or self.proc.stdout is None:
            return
        try:
            chunk = self.proc.stdout.read(CHUNK_READ)
            if not chunk:
                self._try_restart()
                return
            self.buffer.extend(chunk)
            self._process_frames()
        except BlockingIOError:
            pass
        except Exception:
            self.logger.exception("on_stdout_ready error")

    def _try_restart(self):
        if not self.auto_restart or self.restart_count >= self.max_restarts:
            self.logger.warning("stream ended or crashed, not restarting")
            return

        # 限制重启频率：避免频繁重试
        now = time.time()
        time_since_last_restart = now - self.last_restart_time
        restart_interval = settings.stream_restart_interval

        if time_since_last_restart < restart_interval:
            self.logger.info("skipping restart (last restart was %.1fs ago, interval=%ds)",
                           time_since_last_restart, restart_interval)
            return

        self.restart_count += 1
        self.last_restart_time = now
        self.logger.info("attempting restart %s/%s", self.restart_count, self.max_restarts)
        self.stop(wait=1.0)
        time.sleep(3)  # 等待3秒再重启
        self.start()

        # 新增：通知manager重新注册selector
        # 这对于POSIX系统至关重要，因为新的ffmpeg进程有新的stdout fd
        if hasattr(self.manager, '_reregister_decoder_selector'):
            try:
                self.manager._reregister_decoder_selector(self.client_id)
            except Exception as e:
                self.logger.error("failed to reregister selector after restart: %s", e)

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
        return ratio >= PER_STREAM_MAX_PENDING_RATIO

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
            frame_bytes = bytes(self.buffer[:self.frame_size])
            del self.buffer[:self.frame_size]

            try:
                # 1. 解析帧
                arr = np.frombuffer(frame_bytes, dtype=np.uint8)
                std = arr.reshape((self.height, self.width, DEFAULT_CHANNELS))

                # 2. 背压检测（检查 CA-Ready-Queue 而非 CA-Raw-Queue）
                pending_count = self.manager.get_pending_count(self.client_id)

                # 获取队列容量
                queue_capacity = 0
                if self.client_queues is not None:
                    queue_capacity = self.client_queues.ca_ready.maxlen or 0

                # 判断是否应该丢帧
                if self._should_drop_frame(pending_count, queue_capacity):
                    self.frames_dropped += 1
                    # 仅在每100帧打印一次（避免日志洪水）
                    if self.frames_dropped % 100 == 0:
                        self.logger.warning("[BACKPRESSURE] dropping frame (pending=%s/%s, dropped=%s)",
                                          pending_count, queue_capacity, self.frames_dropped)
                    continue  # 跳过此帧，不写入任何队列

                # 3. 写入队列（如果 client_queues 可用）
                if self.client_queues is not None:
                    now = time.time()
                    frame_data_obj = FrameData(timestamp=now, frame=std)

                    # 3.1 写入原始队列（全帧率，用于落盘）
                    if self.client_queues.append_ca_raw(frame_data_obj):
                        self.frames_written_to_raw += 1

                    # 3.2 写入推理队列（降频）
                    if self.client_queues.append_ca_ready_with_throttle(frame_data_obj):
                        self.frames_written_to_ready += 1
                else:
                    # 兼容旧模式：调用 ai.submit_frame
                    ai.submit_frame(self.client_id, std)

                self.frames_received += 1
                self.last_frame_time = time.time()  # 更新最后帧时间

                # log every N frames to observe liveness
                if self.frames_received % 300 == 0:
                    self.logger.info("received %s frames (raw=%s, ready=%s, dropped=%s)",
                                    self.frames_received, self.frames_written_to_raw,
                                    self.frames_written_to_ready, self.frames_dropped)
            except Exception:
                self.frames_dropped += 1
                self.logger.exception("error processing frame bytes")
