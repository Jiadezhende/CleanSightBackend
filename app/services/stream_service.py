import os
import subprocess
import threading
import time
import selectors
import queue
import traceback
from typing import Optional, Dict, Callable

import numpy as np
import cv2
import logging

logger = logging.getLogger("app.services.stream_service")

from app.services import ai
from app.models.frame import FrameData

# 导入 ClientManager 单例（延迟导入避免循环依赖）
try:
    from app.services.client_manager import client_manager
except ImportError:
    client_manager = None  # 兼容旧版本

# configuration
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
        self.logger = logging.getLogger(f"app.services.stream_service.FFmpegDecoder.{self.client_id}")

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
            self.logger.debug("decoder start complete")
    def stop(self, wait: float = 2.0):
        with self.lock:
            self._stop_event.set()
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

    def restart(self):
        with self.lock:
            self.logger.info("restarting decoder (count before restart=%s)", self.restart_count)
            self.stop()
            time.sleep(0.5)
            try:
                self.start()
                self.restart_count += 1
                self.logger.info("restart succeeded (count=%s)", self.restart_count)
            except Exception:
                self.logger.exception("restart failed")

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def on_stdout_ready(self):
        if not self.is_alive():
            return
        try:
            fd = self.proc.stdout.fileno()
            chunk = os.read(fd, CHUNK_READ)
            if not chunk:
                return
            self._process_bytes(chunk)
        except BlockingIOError:
            return
        except Exception:
            self.logger.exception("error reading stdout")

    def _windows_reader_loop(self):
        proc = self.proc
        if proc is None:
            return
        try:
            while not self._stop_event.is_set() and self.is_alive():
                chunk = proc.stdout.read(CHUNK_READ)
                if not chunk:
                    break
                self._process_bytes(chunk)
        except Exception:
            self.logger.exception("windows reader exception")

    def _read_stderr_loop(self):
        proc = self.proc
        if proc is None:
            return
        try:
            while not self._stop_event.is_set() and self.is_alive():
                line = proc.stderr.readline()
                if not line:
                    break
                try:
                    s = line.decode(errors='ignore').strip()
                except Exception:
                    s = str(line)
                # log at debug level to avoid noisy INFO
                self.logger.debug("ffmpeg stderr: %s", s)
        except Exception:
            pass

    def _standardize_frame(self, frm: np.ndarray) -> np.ndarray:
        """
        标准化帧（拉流层处理）：仅做统一 resize，不做模型相关预处理
        
        移除了：
        - MODEL_INPUT_WIDTH/HEIGHT 相关的 resize（已移到 ai 服务的预处理）
        - MODEL_INPUT_COLOR 相关的颜色转换（已移到 ai 服务的预处理）
        
        保留：
        - 基本格式转换（GRAY→BGR、4通道→3通道）
        - 统一 resize 到配置尺寸（默认 640x480）
        """
        if frm is None:
            return frm
        if not isinstance(frm, np.ndarray):
            frm = np.array(frm)
        
        # 基本格式转换
        if frm.ndim == 2:
            frm = cv2.cvtColor(frm, cv2.COLOR_GRAY2BGR)
        if frm.shape[2] == 4:
            frm = frm[:, :, :3]
        if frm.dtype != np.uint8:
            try:
                frm = np.clip(frm, 0, 255).astype(np.uint8)
            except Exception:
                frm = frm.astype(np.uint8, copy=False)
        
        # 统一 resize（使用 self.width/height，通常为 640x480）
        try:
            frm = cv2.resize(frm, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
        except Exception:
            pass
        
        frm = np.ascontiguousarray(frm)
        return frm

    def _process_bytes(self, chunk: bytes):
        if not chunk:
            return
        self.buffer += chunk
        while len(self.buffer) >= self.frame_size:
            frame_data = bytes(self.buffer[: self.frame_size])
            del self.buffer[: self.frame_size]
            
            # 背压控制：检查推理队列深度
            pending = self.manager.get_pending_count(self.client_id)
            
            # 动态计算背压阈值（基于队列容量）
            if self.client_queues is not None:
                # 使用队列实际容量计算阈值
                max_ready = self.client_queues.ca_ready.maxlen or 2700
                threshold = int(max_ready * PER_STREAM_MAX_PENDING_RATIO)
            else:
                # 兼容旧模式：使用固定阈值
                threshold = 2430  # 默认 2700 * 0.9
            
            if pending >= threshold:
                self.frames_dropped += 1
                # log occasional drop
                if self.frames_dropped % 50 == 0:
                    self.logger.warning("[BACKPRESSURE] dropped %s frames (pending=%s >= threshold=%s)", 
                                       self.frames_dropped, pending, threshold)
                continue
            
            try:
                # 1. 组帧
                arr = np.frombuffer(frame_data, dtype=np.uint8).reshape((self.height, self.width, 3))
                
                # 2. 标准化（仅 resize，不做模型预处理）
                std = self._standardize_frame(arr)
                
                # 3. 写入队列或调用旧接口
                if self.client_queues is not None:
                    # 新模式：直接写入 ClientQueues
                    now = time.time()
                    frame_data_obj = FrameData(timestamp=now, frame=std)
                    
                    # 3.1 写入落盘队列（全量）
                    if self.client_queues.append_ca_raw(frame_data_obj):
                        self.frames_written_to_raw += 1
                    
                    # 3.2 写入推理队列（降频）
                    if self.client_queues.append_ca_ready_with_throttle(frame_data_obj):
                        self.frames_written_to_ready += 1
                else:
                    # 兼容旧模式：调用 ai.submit_frame
                    ai.submit_frame(self.client_id, std)
                
                self.frames_received += 1
                
                # log every N frames to observe liveness
                if self.frames_received % 300 == 0:
                    self.logger.info("received %s frames (raw=%s, ready=%s, dropped=%s)", 
                                    self.frames_received, self.frames_written_to_raw, 
                                    self.frames_written_to_ready, self.frames_dropped)
            except Exception:
                self.frames_dropped += 1
                self.logger.exception("error processing frame bytes")


class StreamService:
    def __init__(self):
        self.decoders: Dict[str, FFmpegDecoder] = {}
        self.sel = selectors.DefaultSelector() if os.name != 'nt' else None
        self.lock = threading.Lock()
        self.metrics = {}
        self._stop_event = threading.Event()
        # start selector polling thread on POSIX so stdout is consumed
        self._selector_thread: Optional[threading.Thread] = None
        if self.sel is not None:
            self._selector_thread = threading.Thread(target=self._selector_loop, daemon=True, name="stream_service_selector")
            self._selector_thread.start()

    def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTMP'):
        with self.lock:
            if client_id in self.decoders:
                raise RuntimeError(f"stream {client_id} already started")
            logger.info("start_stream client=%s protocol=%s url=%s", client_id, protocol, stream_url)
            
            # 创建或获取 ClientQueues（通过 ClientManager）
            client_queues = None
            if client_manager is not None:
                client_queues = client_manager.get_client(
                    client_id,
                    resize_width=640,
                    resize_height=480,
                    inference_fps=10,
                    ca_maxlen=2700,  # 90秒缓冲
                    ca_segment_len=150  # 5秒段
                )
                logger.info("ClientQueues created/retrieved for client=%s", client_id)
            
            protocol_opts = []
            if protocol == 'RTSP':
                protocol_opts = [
                    "-rtsp_transport", "udp",
                    "-fflags", "nobuffer",
                    "-flags", "low_delay",
                    "-analyzeduration", "1000000",
                    "-probesize", "1000000",
                ]
            dec = FFmpegDecoder(
                manager=self, 
                client_id=client_id, 
                stream_url=stream_url, 
                fps=fps, 
                protocol_opts=protocol_opts,
                client_queues=client_queues  # 传入 ClientQueues
            )
            self.decoders[client_id] = dec
            self.metrics[client_id] = {"frames_received": 0, "frames_dropped": 0, "restarts": 0}
            dec.start()
            logger.info("stream started client=%s pid=%s", client_id, getattr(dec.proc, 'pid', None))
            if self.sel is not None and dec.proc and dec.proc.stdout:
                try:
                    self.sel.register(dec.proc.stdout.fileno(), selectors.EVENT_READ, data=dec)
                except Exception:
                    pass

    def stop_stream(self, client_id: str):
        with self.lock:
            dec = self.decoders.pop(client_id, None)
            if not dec:
                return
            logger.info("stopping stream client=%s", client_id)
            if self.sel is not None and dec.proc and dec.proc.stdout:
                try:
                    self.sel.unregister(dec.proc.stdout.fileno())
                except Exception:
                    pass
            dec.stop()
            self.metrics.pop(client_id, None)
            
            # 清理 ClientQueues（可选：保留用于查询历史）
            if client_manager is not None:
                # cleanup=False 保留队列数据，cleanup=True 清空队列
                client_manager.remove_client(client_id, cleanup=False)
            
            logger.info("stream stopped client=%s", client_id)

    def has_stream(self, client_id: str) -> bool:
        with self.lock:
            dec = self.decoders.get(client_id)
            return dec is not None and dec.is_alive()

    def get_pending_count(self, client_id: str) -> int:
        """
        获取指定客户端的 CA-Ready-Queue 深度（用于背压控制）
        
        关键修改：检查 CA-Ready-Queue（推理队列）而不是 CA-Raw-Queue
        原因：CA-Raw-Queue 用于落盘，即使满了也不应阻塞拉流
              CA-Ready-Queue 用于推理，如果满了说明推理跟不上，需要丢帧
        """
        if client_manager is None:
            logger.warning("[BACKPRESSURE] client_manager is None, import may have failed")
            return 0
        
        try:
            client_queues = client_manager.get_client(client_id)
            if client_queues is None:
                logger.warning("[BACKPRESSURE] client_queues is None for client_id=%s", client_id)
                return 0
            
            depths = client_queues.get_queue_depths()
            if not depths:
                logger.warning("[BACKPRESSURE] get_queue_depths returned empty for client_id=%s", client_id)
                return 0
            
            # 关键修改：检查 CA-Ready-Queue（推理队列）深度
            ready_depth = depths.get("ca_ready", 0)
            raw_depth = depths.get("ca_raw", 0)
            
            # 定期打印队列状态（每100帧打印一次）
            decoder = self.decoders.get(client_id)
            if decoder and decoder.frames_received % 100 == 0:
                logger.info("[BACKPRESSURE] client=%s: ca_ready=%s/%s, ca_raw=%s/%s", 
                           client_id, ready_depth, client_queues.ca_ready.maxlen,
                           raw_depth, client_queues.ca_raw.maxlen)
            
            return ready_depth  # 返回推理队列深度
            
        except Exception as e:
            logger.error("[BACKPRESSURE] get_pending_count failed for client_id=%s: %s", 
                        client_id, e, exc_info=True)
            return 0

    def run_once(self, timeout: float = 0.05):
        if self.sel is None:
            return
        events = self.sel.select(timeout=timeout)
        for key, mask in events:
            dec: FFmpegDecoder = key.data
            try:
                dec.on_stdout_ready()
            except Exception:
                traceback.print_exc()

    def _selector_loop(self, timeout: float = 0.05):
        """Background loop that polls the selector and dispatches stdout reads.

        This ensures on POSIX systems we actually consume ffmpeg stdout even
        if no external loop is calling `run_once`.
        """
        while not self._stop_event.is_set():
            try:
                self.run_once(timeout=timeout)
            except Exception:
                traceback.print_exc()
        # cleanup on exit
        try:
            if self.sel is not None:
                try:
                    # unregister any fds
                    for key in list(self.sel.get_map().values()):
                        try:
                            self.sel.unregister(key.fd)
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    self.sel.close()
                except Exception:
                    pass
        except Exception:
            pass
    
    def shutdown(self):
        """
        关闭服务，释放所有资源
        
        停止所有流，清理所有客户端资源
        """
        logger.info("Shutting down StreamService...")
        
        # 停止所有流
        with self.lock:
            client_ids = list(self.decoders.keys())
        
        for client_id in client_ids:
            try:
                self.stop_stream(client_id)
            except Exception as e:
                logger.error(f"Error stopping stream {client_id}: {e}")
        
        # 清空所有客户端队列
        if client_manager is not None:
            try:
                client_manager.clear_all()
            except Exception as e:
                logger.error(f"Error clearing client manager: {e}")
        
        logger.info("StreamService shutdown complete")


# singleton service instance
stream_service = StreamService()
