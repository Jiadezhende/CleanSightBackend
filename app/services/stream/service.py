"""
流服务 - 统一管理所有视频流的解码
"""

import os
import threading
import traceback
import selectors
from typing import Optional, Dict
import logging

from .decoder import FFmpegDecoder

logger = logging.getLogger("app.services.stream.service")

# 导入 ClientManager 单例（延迟导入避免循环依赖）
try:
    from app.services.client import client_manager
except ImportError:
    client_manager = None  # 兼容旧版本

# 导入配置加载器
try:
    from app.services.inference.config_loader import load_stage_config
    _inference_config = load_stage_config()
except Exception as e:
    logger.warning(f"Failed to load inference config: {e}, using defaults")
    _inference_config = None


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
                # 从配置文件读取推理FPS（base_fps=30, decimation=2 → 15fps）
                inference_fps = _inference_config.get_inference_fps(30) if _inference_config else 15

                client_queues = client_manager.get_client(
                    client_id,
                    resize_width=640,
                    resize_height=480,
                    inference_fps=inference_fps,
                    ca_maxlen=600,  # 20秒缓冲
                    ca_segment_len=150  # 5秒段
                )
                logger.info("ClientQueues created for client=%s (inference_fps=%d)", client_id, inference_fps)

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

            # TODO: 清理 ClientQueues（可选：保留用于查询历史）
            if client_manager is not None:
                # cleanup=False 保留队列数据，cleanup=True 清空队列
                client_manager.remove_client(client_id, cleanup=True)

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
            # 先检查客户端是否存在
            if not client_manager.has_client(client_id):
                return 0

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
        for key, _ in events:
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
