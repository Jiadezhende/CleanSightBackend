"""
流服务 - 统一管理所有视频流的解码

边界层异常处理架构：
- 业务代码保持纯净（只抛异常，不捕获）
- 重试逻辑在 GuardedExecutor 框架层
- 异常分类：StreamConnectionError, FFmpegError
"""

import os
import threading
import selectors
from typing import Optional, Dict, Any
import logging

from .decoder import FFmpegDecoder
from app.utils import (
    GuardedExecutor,
    StreamConnectionError,
    FFmpegError,
    log_call,
)

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

        # 创建 GuardedExecutor（框架边界层）
        self.executor = GuardedExecutor()

        # start selector polling thread on POSIX so stdout is consumed
        self._selector_thread: Optional[threading.Thread] = None
        if self.sel is not None:
            self._selector_thread = threading.Thread(target=self._selector_loop, daemon=True, name="stream_service_selector")
            self._selector_thread.start()

        # 健康监控（懒加载）
        self.health_monitor = None
        self.cleanup_service = None

    @log_call(level=logging.INFO, log_args=True)
    def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTMP'):
        """
        服务层方法（调用框架边界层）

        通过 GuardedExecutor 执行流启动，自动处理重试逻辑。
        重试策略：固定延迟 3 秒，最多 5 次。

        Args:
            client_id: 客户端ID
            stream_url: 流URL
            fps: 帧率
            protocol: 协议（RTSP/RTMP）

        Raises:
            StreamConnectionError: 流连接失败
            FFmpegError: FFmpeg 启动失败
        """
        # 第一次启动流时，启动健康监控（懒加载）
        self._ensure_health_monitor()

        # 通过 GuardedExecutor 调用业务逻辑（边界层 2）
        return self.executor.execute(
            func=lambda: self._start_stream_impl(client_id, stream_url, fps, protocol),
            policy_name='stream'  # 固定延迟 3 秒，最多 5 次
        )

    def _start_stream_impl(self, client_id: str, stream_url: str, fps: int, protocol: str):
        """
        业务代码（纯净，只抛异常）

        职责：
        1. 检查解码器状态
        2. 创建并启动解码器
        3. 注册到 selector
        4. 如果失败，抛出 StreamConnectionError 或 FFmpegError

        Args:
            client_id: 客户端ID
            stream_url: 流URL
            fps: 帧率
            protocol: 协议（RTSP/RTMP）
        """
        with self.lock:
            # 检查是否已有解码器
            if client_id in self.decoders:
                existing = self.decoders[client_id]

                # 如果解码器已死，先清理
                if not existing.is_alive():
                    logger.warning(f"[{client_id}] Removing dead decoder before restart")
                    self._cleanup_dead_decoder_unsafe(client_id)
                else:
                    # 解码器还活着，不能重复启动
                    raise StreamConnectionError(
                        url=stream_url,
                        client_id=client_id,
                        details="Stream already started"
                    )

            logger.info(f"[{client_id}] Starting stream: protocol={protocol}, url={stream_url}")

            # 创建或获取 ClientQueues
            client_queues = self._get_or_create_client_queues(client_id)

            # 构建协议选项
            protocol_opts = self._build_protocol_opts(protocol)

            # 创建解码器
            dec = FFmpegDecoder(
                manager=self,
                client_id=client_id,
                stream_url=stream_url,
                decoder_config=self.config.decoder if self.config else None,
                protocol_opts=protocol_opts,
                client_queues=client_queues
            )

            # 启动解码器（可能抛出 FFmpegError）
            dec.start()

            # 保存解码器
            self.decoders[client_id] = dec
            self.metrics[client_id] = {"frames_received": 0, "frames_dropped": 0, "restarts": 0}

            logger.info(f"[{client_id}] Stream started successfully (pid={getattr(dec.proc, 'pid', None)})")

            # 注册到 selector（POSIX 系统）
            if self.sel is not None and dec.proc and dec.proc.stdout:
                self._register_to_selector(dec)

    def _get_or_create_client_queues(self, client_id: str):
        """
        业务代码：获取或创建客户端队列（纯净，只抛异常）

        Args:
            client_id: 客户端ID

        Returns:
            ClientQueues 实例，如果 client_manager 不可用则返回 None
        """
        if client_manager is None:
            return None

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
            f"[{client_id}] ClientQueues created (inference_fps={inference_fps}, "
            f"ca_maxlen={ca_maxlen}, ca_segment_len={ca_segment_len})"
        )

        return client_queues

    def _build_protocol_opts(self, protocol: str) -> list:
        """
        业务代码：构建协议选项（纯净）

        Args:
            protocol: 协议类型（RTSP/RTMP）

        Returns:
            协议选项列表
        """
        if protocol == 'RTSP':
            return [
                "-rtsp_transport", "udp",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-analyzeduration", "1000000",
                "-probesize", "1000000",
            ]
        return []

    def _register_to_selector(self, decoder: FFmpegDecoder):
        """
        业务代码：注册解码器到 selector（纯净，只抛异常）

        Args:
            decoder: FFmpegDecoder 实例

        Raises:
            StreamConnectionError: 注册失败
        """
        if decoder.proc is None or decoder.proc.stdout is None:
            raise StreamConnectionError(
                url=decoder.stream_url,
                client_id=decoder.client_id,
                details="Cannot register to selector: process or stdout is None"
            )

        # 注册到 selector（self.sel 已经在调用前检查过不为 None）
        if self.sel is not None:
            self.sel.register(decoder.proc.stdout.fileno(), selectors.EVENT_READ, data=decoder)

    @log_call(level=logging.INFO)
    def stop_stream(self, client_id: str):
        """
        停止流解码（业务代码，纯净）

        注意：
        - decoder 进程的停止是异步的（避免阻塞）
        - selector 注销在锁内完成
        - ClientManager 由 InferenceManager 统一清理

        Args:
            client_id: 客户端ID
        """
        # 1. 从字典中移除decoder（在锁内）
        dec = None
        with self.lock:
            dec = self.decoders.pop(client_id, None)
            if not dec:
                logger.debug(f"[{client_id}] No decoder to stop")
                return

            logger.info(f"[{client_id}] Stopping stream")

            # 从selector中注销（必须在锁内）
            self._unregister_from_selector(dec)

            # 清理metrics
            self.metrics.pop(client_id, None)

        # 2. 异步停止decoder进程（避免阻塞）
        if dec:
            self._stop_decoder_async(dec, client_id)

        logger.info(f"[{client_id}] Stream stopped")

    def _unregister_from_selector(self, decoder: FFmpegDecoder):
        """
        业务代码：从 selector 注销解码器（纯净）

        Args:
            decoder: FFmpegDecoder 实例
        """
        if self.sel is not None and decoder.proc and decoder.proc.stdout:
            # 注销可能失败（例如已经注销过），不影响整体流程
            self.sel.unregister(decoder.proc.stdout.fileno())

    def _stop_decoder_async(self, decoder: FFmpegDecoder, client_id: str):
        """
        业务代码：异步停止解码器（避免阻塞）

        Args:
            decoder: FFmpegDecoder 实例
            client_id: 客户端ID
        """
        def stop_decoder_worker():
            """
            Worker 线程（边界层 1）

            职责：
            1. 停止解码器进程
            2. 捕获所有异常，防止线程崩溃
            """
            try:
                # 业务逻辑：停止解码器（可能阻塞 2 秒+）
                decoder.stop()
                logger.debug(f"[{client_id}] FFmpeg process stopped")
            except Exception as e:
                # 边界层 1 捕获异常
                logger.error(
                    f"[BoundaryLayer1] Failed to stop FFmpeg for {client_id}: {e}",
                    exc_info=True
                )

        stop_thread = threading.Thread(
            target=stop_decoder_worker,
            daemon=True,
            name=f"stop-decoder-{client_id}"
        )
        stop_thread.start()

    def _cleanup_dead_decoder_unsafe(self, client_id: str):
        """
        业务代码：清理已死亡的解码器（纯净）

        注意：此方法必须在持有self.lock的情况下调用。

        Args:
            client_id: 客户端ID
        """
        dec = self.decoders.pop(client_id, None)
        if dec:
            self._unregister_from_selector(dec)
        self.metrics.pop(client_id, None)
        logger.debug(f"[{client_id}] Dead decoder cleaned")

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

    @log_call(level=logging.INFO, log_args=True)
    def restart_stream(self, client_id: str, stream_url: str, fps: int, protocol: str) -> bool:
        """
        服务层方法：重启流（调用框架边界层）

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
        # 使用 GuardedExecutor 处理重启逻辑（框架边界层）
        return self.executor.execute(
            func=lambda: self._restart_stream_impl(client_id, stream_url, fps, protocol),
            policy_name='stream'  # 固定延迟 3 秒，最多 5 次
        )

    def _restart_stream_impl(self, client_id: str, stream_url: str, fps: int, protocol: str) -> bool:
        """
        业务代码：重启流实现（纯净，只抛异常）

        Args:
            client_id: 客户端ID
            stream_url: 流URL
            fps: 帧率
            protocol: 协议（RTSP/RTMP）

        Returns:
            True表示重启成功

        Raises:
            StreamConnectionError: 重启失败
        """
        # 1. 停止旧的decoder（异步执行，避免阻塞健康监控线程）
        old_dec = None
        with self.lock:
            old_dec = self.decoders.get(client_id)

        if old_dec and old_dec.is_alive():
            # 异步停止旧decoder
            self._stop_decoder_async(old_dec, client_id)
            logger.debug(f"[{client_id}] Async stopping old decoder for restart")

        # 2. 清理旧记录并创建新decoder（在锁内执行）
        with self.lock:
            self._cleanup_dead_decoder_unsafe(client_id)

            # 3. 获取现有的ClientQueues（不创建新的）
            if client_manager is None or not client_manager.has_client(client_id):
                raise StreamConnectionError(
                    url=stream_url,
                    client_id=client_id,
                    details="Cannot restart stream: no ClientQueues"
                )

            client_queues = client_manager.get_client(client_id)

            # 4. 创建新的decoder
            protocol_opts = self._build_protocol_opts(protocol)

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

            # 5. 注册到 selector
            self._register_to_selector(dec)

            logger.info(f"[{client_id}] Stream restarted successfully")
            return True

    def _ensure_health_monitor(self):
        """
        业务代码：确保健康监控已启动（纯净）

        第一次启动流时初始化并启动健康监控。

        Raises:
            Exception: 初始化失败时抛出异常（由调用者处理）
        """
        if self.health_monitor is not None:
            return  # 已启动

        from app.services.stream.health_monitor import StreamHealthMonitor
        from app.services.stream.cleanup import init_cleanup_service
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

        # 先检查客户端是否存在
        if not client_manager.has_client(client_id):
            return 0

        client_queues = client_manager.get_client(client_id)
        if client_queues is None:
            logger.warning(f"[BACKPRESSURE] client_queues is None for client_id={client_id}")
            return 0

        depths = client_queues.get_queue_depths()
        if not depths:
            logger.warning(f"[BACKPRESSURE] get_queue_depths returned empty for client_id={client_id}")
            return 0

        # 关键修改：检查 CA-Ready-Queue（推理队列）深度
        ready_depth = depths.get("ca_ready", 0)
        raw_depth = depths.get("ca_raw", 0)

        # 定期打印队列状态（每100帧打印一次）
        decoder = self.decoders.get(client_id)
        if decoder and decoder.frames_received % 100 == 0:
            logger.info(f"[BACKPRESSURE] client={client_id}: ca_ready={ready_depth}/{client_queues.ca_ready.maxlen}, ca_raw={raw_depth}/{client_queues.ca_raw.maxlen}")

        return ready_depth  # 返回推理队列深度

    def run_once(self, timeout: float = 0.05):
        """
        业务代码：执行一次 selector 轮询（纯净）

        Args:
            timeout: 超时时间（秒）
        """
        if self.sel is None:
            return
        events = self.sel.select(timeout=timeout)
        for key, _ in events:
            dec: FFmpegDecoder = key.data
            # 业务逻辑：调用解码器读取数据
            # 注意：on_stdout_ready() 内部已经处理了异常（返回 False）
            dec.on_stdout_ready()

    def _selector_loop(self, timeout: float = 0.05):
        """
        Selector 轮询线程（边界层 1）

        职责：
        1. 持续轮询 selector，调度 FFmpeg stdout 读取
        2. 捕获所有异常，防止线程崩溃
        3. 清理资源（selector 关闭）

        Args:
            timeout: 轮询超时时间（秒）
        """
        try:
            # 业务逻辑：持续轮询
            while not self._stop_event.is_set():
                try:
                    self.run_once(timeout=timeout)
                except Exception as e:
                    # 边界层 1 捕获异常（防止线程崩溃）
                    logger.error(
                        f"[BoundaryLayer1] Error in selector loop: {e}",
                        exc_info=True
                    )

        finally:
            # 资源清理：关闭 selector
            self._cleanup_selector()

    def _cleanup_selector(self):
        """
        业务代码：清理 selector 资源（纯净）

        注意：此方法在 finally 块中调用，不应抛出异常
        """
        if self.sel is None:
            return

        try:
            # 注销所有文件描述符
            for key in list(self.sel.get_map().values()):
                try:
                    self.sel.unregister(key.fd)
                except Exception:
                    pass  # 忽略注销失败

            # 关闭 selector
            self.sel.close()
            logger.debug("[StreamService] Selector closed")
        except Exception as e:
            logger.error(f"[StreamService] Error cleaning up selector: {e}")

    def shutdown(self):
        """
        关闭服务，释放所有资源（清理代码）

        注意：此方法包含 try/except 块是合理的，因为：
        1. 这是清理代码，需要尽可能完成所有步骤
        2. 一个步骤失败不应阻止其他清理步骤
        3. 不符合"纯净业务代码"原则，但符合资源清理最佳实践
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
                clear_results = client_manager.clear_all()
                failed_count = sum(1 for r in clear_results if not r["success"])
                if failed_count > 0:
                    logger.warning(f"Failed to clean {failed_count}/{len(clear_results)} clients")
            except Exception as e:
                logger.error(f"Error clearing client manager: {e}")

        logger.info("StreamService shutdown complete")


# singleton service instance
stream_service = StreamService()
