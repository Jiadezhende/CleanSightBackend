"""
流服务 - 统一管理所有视频流的解码
"""

import os
import threading
import traceback
import selectors
from typing import Optional, Dict, Any
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
    from app.services.inference.config import load_stage_config
    from app.services.stream.config import get_stream_config
    from app.services.client.config import get_client_config

    _inference_config = load_stage_config()
    _stream_config = get_stream_config()
    _client_config = get_client_config()
except Exception as e:
    logger.warning(f"Failed to load configs: {e}, using defaults")
    _inference_config = None
    _stream_config = None
    _client_config = None


class StreamService:
    def __init__(self):
        self.decoders: Dict[str, FFmpegDecoder] = {}
        self.sel = selectors.DefaultSelector() if os.name != 'nt' else None
        self.lock = threading.Lock()
        self.metrics = {}
        self._stop_event = threading.Event()

        # 配置引用
        self.config = _stream_config

        # start selector polling thread on POSIX so stdout is consumed
        self._selector_thread: Optional[threading.Thread] = None
        if self.sel is not None:
            self._selector_thread = threading.Thread(target=self._selector_loop, daemon=True, name="stream_service_selector")
            self._selector_thread.start()

        # 健康监控（懒加载）
        self.health_monitor = None
        self.cleanup_service = None

    def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTMP'):
        # 第一次启动流时，启动健康监控（懒加载）
        self._ensure_health_monitor()

        with self.lock:
            # 检查是否已有解码器
            if client_id in self.decoders:
                existing = self.decoders[client_id]

                # 如果解码器已死，先清理
                if not existing.is_alive():
                    logger.warning(f"Removing dead decoder for {client_id} before restart")
                    self._cleanup_dead_decoder_unsafe(client_id)
                else:
                    # 解码器还活着，不能重复启动
                    raise RuntimeError(f"stream {client_id} already started")

            logger.info("start_stream client=%s protocol=%s url=%s", client_id, protocol, stream_url)

            # 创建或获取 ClientQueues（通过 ClientManager）
            client_queues = None
            if client_manager is not None:
                # 从配置文件读取推理FPS
                inference_fps = _inference_config.get_inference_fps(30) if _inference_config else 15

                # 从配置文件读取帧和队列参数
                resize_width = _client_config.frame.resize_width if _client_config else 640
                resize_height = _client_config.frame.resize_height if _client_config else 480
                ca_maxlen = _inference_config.ca_maxlen if _inference_config else 600
                ca_segment_len = _inference_config.ca_segment_len if _inference_config else 150

                client_queues = client_manager.get_client(
                    client_id,
                    resize_width=resize_width,
                    resize_height=resize_height,
                    inference_fps=inference_fps,
                    ca_maxlen=ca_maxlen,
                    ca_segment_len=ca_segment_len
                )
                logger.info(
                    "ClientQueues created for client=%s (inference_fps=%d, ca_maxlen=%d, ca_segment_len=%d)",
                    client_id, inference_fps, ca_maxlen, ca_segment_len
                )

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
                decoder_config=self.config.decoder if self.config else None,
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
        """停止流解码（异步停止decoder，避免阻塞）"""
        # 1. 从字典中移除decoder（在锁内）
        dec = None
        with self.lock:
            dec = self.decoders.pop(client_id, None)
            if not dec:
                return

            logger.info(f"[StreamService] Stopping stream: {client_id}")

            # 从selector中注销（必须在锁内）
            if self.sel is not None and dec.proc and dec.proc.stdout:
                try:
                    self.sel.unregister(dec.proc.stdout.fileno())
                except Exception:
                    pass

            # 清理metrics
            self.metrics.pop(client_id, None)

        # 2. 异步停止decoder进程（避免阻塞）
        if dec:
            def stop_decoder_async():
                try:
                    dec.stop()  # 可能阻塞2秒+，在后台线程执行
                    logger.debug(f"[StreamService] FFmpeg process stopped: {client_id}")
                except Exception as e:
                    logger.error(f"[StreamService] Failed to stop FFmpeg: {client_id} - {e}")

            stop_thread = threading.Thread(
                target=stop_decoder_async,
                daemon=True,
                name=f"stop-decoder-{client_id}"
            )
            stop_thread.start()

        # 3. 清理ClientManager（快速操作，在主线程执行）
        if client_manager is not None:
            client_manager.remove_client(client_id, cleanup=True)

        logger.info(f"[StreamService] Stream stopped: {client_id}")

    def _cleanup_dead_decoder_unsafe(self, client_id: str):
        """内部清理方法（必须持有锁）

        用于清理已死亡的解码器进程，支持重连场景。

        注意：此方法必须在持有self.lock的情况下调用。

        Args:
            client_id: 客户端ID
        """
        dec = self.decoders.pop(client_id, None)
        if dec and self.sel is not None and dec.proc and dec.proc.stdout:
            try:
                self.sel.unregister(dec.proc.stdout.fileno())
            except Exception:
                pass
        self.metrics.pop(client_id, None)
        logger.debug(f"[StreamService] Dead decoder cleaned: {client_id}")

    def has_stream(self, client_id: str) -> bool:
        with self.lock:
            dec = self.decoders.get(client_id)
            return dec is not None and dec.is_alive()

    def get_all_client_ids(self) -> set:
        """获取所有活跃的客户端ID（有decoder的）

        Returns:
            客户端ID的集合
        """
        with self.lock:
            return set(self.decoders.keys())

    def get_stream_info(self, client_id: str) -> Optional[Dict[str, Any]]:
        """获取流配置信息（用于重连）

        Returns:
            流配置字典，包含url, fps, protocol，如果不存在返回None
        """
        with self.lock:
            dec = self.decoders.get(client_id)
            if not dec:
                return None

            # 判断协议类型
            protocol = 'RTMP'
            if dec.protocol_opts and any('rtsp' in str(opt).lower() for opt in dec.protocol_opts):
                protocol = 'RTSP'

            return {
                'url': dec.stream_url,
                'fps': dec.fps,
                'protocol': protocol
            }

    def restart_stream(self, client_id: str, stream_url: str, fps: int, protocol: str) -> bool:
        """重启流（用于自动重连）

        与start_stream的区别：
        - 不创建新的ClientQueues（保留现有队列）
        - 只重启FFmpegDecoder

        Args:
            client_id: 客户端ID
            stream_url: 流URL
            fps: 帧率
            protocol: 协议（RTSP/RTMP）

        Returns:
            True表示重启成功，False表示失败
        """
        # 1. 停止旧的decoder（异步执行，避免阻塞健康监控线程）
        old_dec = None
        with self.lock:
            old_dec = self.decoders.get(client_id)

        if old_dec:
            if old_dec.is_alive():
                # 在后台线程中停止旧decoder，不等待完成
                # 这样restart_stream可以快速返回，让健康监控线程继续执行
                def stop_decoder_async():
                    try:
                        logger.debug(f"[StreamService] Stopping old decoder: {client_id}")
                        old_dec.stop()  # 可能阻塞几秒，但在后台线程中执行
                        logger.debug(f"[StreamService] Old decoder stopped: {client_id}")
                    except Exception as e:
                        logger.error(f"[StreamService] Failed to stop old decoder: {client_id} - {e}")

                stop_thread = threading.Thread(
                    target=stop_decoder_async,
                    daemon=True,
                    name=f"stop-decoder-{client_id}"
                )
                stop_thread.start()
                logger.debug(f"[StreamService] Async stopping old decoder: {client_id}")
            else:
                logger.debug(f"[StreamService] Old decoder already dead: {client_id}")

        # 2. 清理旧记录并创建新decoder（在锁内执行）
        with self.lock:
            self._cleanup_dead_decoder_unsafe(client_id)

            # 3. 获取现有的ClientQueues（不创建新的）
            client_queues = None
            if client_manager is not None and client_manager.has_client(client_id):
                client_queues = client_manager.get_client(client_id)
            else:
                logger.error(f"Cannot restart stream: no ClientQueues for {client_id}")
                return False

            # 4. 创建新的decoder
            try:
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
                    decoder_config=self.config.decoder if self.config else None,
                    protocol_opts=protocol_opts,
                    client_queues=client_queues
                )

                self.decoders[client_id] = dec
                dec.start()

                if self.sel is not None and dec.proc and dec.proc.stdout:
                    try:
                        self.sel.register(dec.proc.stdout.fileno(), selectors.EVENT_READ, data=dec)
                    except Exception:
                        pass

                logger.info(f"Stream restarted for {client_id}")
                return True

            except Exception as e:
                logger.error(f"Failed to restart stream for {client_id}: {e}")
                return False

    def _ensure_health_monitor(self):
        """确保健康监控已启动（懒加载）

        第一次启动流时初始化并启动健康监控。
        """
        if self.health_monitor is not None:
            return  # 已启动

        try:
            from app.services.stream.health_monitor import StreamHealthMonitor
            from app.services.stream.cleanup import CleanupService, init_cleanup_service
            from app.services import ai  # AI服务模块

            # 初始化清理服务（使用ai.manager作为InferenceManager实例，传入配置）
            init_cleanup_service(
                stream_service=self,
                client_manager=client_manager,
                inference_manager=ai.manager,
                cleanup_config=self.config.cleanup if self.config else None
            )

            # 导入全局cleanup_service
            from app.services.stream.cleanup import cleanup_service
            self.cleanup_service = cleanup_service

            # 启动清理服务的后台线程
            self.cleanup_service.start()
            logger.info("[StreamService] Cleanup service background thread started")

            # 初始化健康监控（传入stream_service以支持自动重连，传入配置）
            self.health_monitor = StreamHealthMonitor(
                client_manager=client_manager,
                cleanup_service=self.cleanup_service,
                stream_service=self,
                health_config=self.config.health_monitor if self.config else None
            )

            # 启动监控线程
            self.health_monitor.start()
            logger.info("[StreamService] Health monitor started")

        except Exception as e:
            logger.error(f"[StreamService] Failed to start health monitor: {e}", exc_info=True)
            # 失败不影响流服务，只是没有自动清理功能

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

        # 停止健康监控
        if self.health_monitor:
            try:
                self.health_monitor.stop()
                logger.info("Health monitor stopped")
            except Exception as e:
                logger.error(f"Error stopping health monitor: {e}")

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
