"""
流服务 - 统一管理所有 RTSP 视频流的解码

边界层异常处理架构：
- 业务代码保持纯净（只抛异常，不捕获）
- 重试逻辑在 GuardedExecutor 框架层
- 异常分类：StreamConnectionError, FFmpegError

读帧模型：decoder 自持读线程（阻塞读 stdout，Windows/POSIX 统一），
StreamService 只做 decoder 注册表 + 生命周期编排，不再持有 selector。
"""

import logging
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

from app.settings import settings
from app.utils import (
    ConflictError,
    StreamConnectionError,
    log_call,
)

from .decoder import FFmpegDecoder

logger = logging.getLogger("app.services.stream.service")


def _rewrite_rtsp_url(url: str, proxy_port: int, internal_port: int) -> str:
    """
    将流 URL 中的代理端口替换为 MediaMTX 内部端口，使后端拉流绕过 RTSPProxy。

    仅当 URL 端口与 proxy_port 完全匹配时才重写，不匹配则原样返回。
    支持带 userinfo（rtsp://user:pass@host:port/path）的 URL。
    """
    parsed = urlparse(url)
    if parsed.port != proxy_port:
        return url

    # 端口匹配说明目标是本机 MediaMTX，统一改写为 127.0.0.1
    # （覆盖 localhost / 外网 IP / 任意 host，避免流量绕道外网网卡被 iptables 拦截）
    new_netloc = f"127.0.0.1:{internal_port}"
    if parsed.username:
        creds = parsed.username
        if parsed.password:
            creds += f":{parsed.password}"
        new_netloc = f"{creds}@{new_netloc}"

    return urlunparse(parsed._replace(netloc=new_netloc))


_client_manager = None


def _get_client_manager():
    """惰性获取 ClientManager 单例（首次调用时导入并缓存）。

    不在模块级导入：改为调用时导入。调用发生在运行期（起流/处理帧），此刻各模块
    已完成初始化，无导入顺序问题；且真实 ImportError 会在此直接冒出，不再被模块级
    try/except 静默吞成 None（旧写法一旦 client 模块内部出错就悄然降级、背压/队列失效）。
    """
    global _client_manager
    if _client_manager is None:
        from app.services.client import client_manager
        _client_manager = client_manager
    return _client_manager


# 导入配置加载器
try:
    from app.services.client.config import get_client_config
    from app.services.stream.config import get_stream_config

    _stream_config = get_stream_config()
    _client_config = get_client_config()
except Exception as e:
    logger.warning(f"Failed to load configs: {e}, using defaults")
    _stream_config = None
    _client_config = None


class StreamService:
    def __init__(self):
        self.decoders: Dict[int, FFmpegDecoder] = {}
        self.lock = threading.Lock()
        self.metrics = {}

        # 配置引用
        self.config = _stream_config

        # 注意：健康监控和清理服务现在都是全局服务，在应用启动时初始化，不再由 StreamService 管理。
        # decoder 自持读线程，StreamService 不再需要 selector/轮询线程。

    @log_call(level=logging.INFO, log_args=False)
    def start_stream(self, task_id: int, stream_url: str):
        """
        注册解码器并尝试首次启动。

        decoder 注册成功后立即返回，不等待流连接结果。
        首次 start() 若失败，健康监控会在下一个心跳周期发起重连。

        Args:
            task_id: 运行键（路由标识）
            stream_url: RTSP 流 URL

        Raises:
            ConflictError: 该 task_id 已有存活的流
        """
        self._start_stream_impl(task_id, stream_url)

    def _start_stream_impl(self, task_id: int, stream_url: str):
        """
        业务代码（纯净，只抛异常）

        职责：
        1. 检查解码器状态
        2. 创建并启动解码器
        3. 如果失败，抛出 StreamConnectionError 或 FFmpegError

        Args:
            task_id: 运行键（路由标识）
            stream_url: RTSP 流 URL
        """
        with self.lock:
            # 检查是否已有解码器
            if task_id in self.decoders:
                existing = self.decoders[task_id]

                # 如果解码器已死，先清理
                if not existing.is_alive():
                    logger.warning(
                        f"[{task_id}] Removing dead decoder before restart"
                    )
                    self._cleanup_dead_decoder_unsafe(task_id)
                else:
                    # 解码器还活着，无法重复启动（可能URL不同）
                    logger.warning(
                        f"[{task_id}] Stream already running, cannot start with different URL"
                    )
                    _cq = existing.client_queues
                    raise ConflictError(
                        message=f"Stream already running for task {task_id}. Stop it first to change stream URL.",
                        task_id=task_id,
                        step_id=_cq.step_id if _cq else None,
                        source_ip=_cq.source_ip if _cq else None,
                        resource_type="Stream",
                        resource_id=str(task_id),
                    )

            logger.info(f"[{task_id}] Starting stream: url={stream_url}")

            # 内部拉流直连 MediaMTX，绕过 RTSPProxy
            rewritten = _rewrite_rtsp_url(
                stream_url,
                settings.mediamtx_proxy_port,
                settings.mediamtx_internal_port,
            )
            if rewritten != stream_url:
                logger.info(f"[{task_id}] Rewrote stream URL: {stream_url} → {rewritten}")
                stream_url = rewritten

            # 获取 ClientQueues（只取不建：由 set_task 在起流前建好换槽）
            client_queues = self._get_client_queues(task_id)

            # 创建解码器
            dec = FFmpegDecoder(
                manager=self,
                task_id=task_id,
                stream_url=stream_url,
                decoder_config=self.config.decoder if self.config else None,
                client_queues=client_queues,
            )

            # 先注册解码器，再启动——健康监控可感知启动失败并触发重连
            self.decoders[task_id] = dec
            self.metrics[task_id] = {
                "frames_received": 0,
                "frames_dropped": 0,
                "restarts": 0,
            }

            try:
                dec.start()
            except Exception as e:
                # start() 失败（如推流端尚未就绪），decoder 已注册，
                # 健康监控会在下一个心跳检测到 is_alive=False 并进入重连模式
                logger.warning(f"[{task_id}] Initial start failed: {e}")
                return

            logger.info(
                f"[{task_id}] Stream started successfully (pid={getattr(dec.proc, 'pid', None)})"
            )

    def _get_client_queues(self, task_id: int):
        """获取该 client 的 ClientQueues（**只取不建**）。

        CQ 由 RunController.start_run → InferenceManager.start_workflow 在起流**之前**建好并换槽
        （一 CQ == 一 run，身份不可变）；起流阶段只取。缺失说明调用序错（未先 start_workflow），
        返回 None 由上层容错（decoder 空跑）。
        """
        cq = _get_client_manager().get(task_id)
        if cq is None:
            logger.error(
                "[%s] ClientQueues 不存在（start_stream 早于 set_task？），decoder 将空跑",
                task_id,
            )
        return cq

    @log_call(level=logging.INFO)
    def stop_stream(self, task_id: int):
        """
        停止流解码（业务代码，纯净）

        注意：
        - decoder 进程的停止是异步的（避免阻塞 API 响应）
        - ClientManager 由 InferenceManager 统一清理

        Args:
            task_id: 运行键（路由标识）
        """
        # 1. 从字典中移除decoder（在锁内）
        dec = None
        with self.lock:
            dec = self.decoders.pop(task_id, None)
            if not dec:
                logger.debug(f"[{task_id}] No decoder to stop")
                return

            logger.info(f"[{task_id}] Stopping stream")

            # 清理metrics
            self.metrics.pop(task_id, None)

        # 2. 异步停止decoder进程（避免阻塞）。terminal 路径：无新 run 复用该 CQ，
        #    迟到帧由 CQ 写门（DRAINING/CLOSED）拦截，故异步安全。
        if dec:
            self._stop_decoder_async(dec, task_id)

        logger.info(f"[{task_id}] Stream stopped")

    def _stop_decoder_async(self, decoder: FFmpegDecoder, task_id: int):
        """
        业务代码：异步停止解码器（避免阻塞）

        Args:
            decoder: FFmpegDecoder 实例
            task_id: 运行键（路由标识）
        """

        def stop_decoder_worker():
            """
            Worker 线程（边界层 1）

            职责：
            1. 停止解码器进程
            2. 捕获所有异常，防止线程崩溃
            """
            try:
                # 业务逻辑：停止解码器（kill + reap + join reader）
                decoder.stop()
                logger.debug(f"[{task_id}] FFmpeg process stopped")
            except Exception as e:
                # 边界层 1 捕获异常
                logger.error(
                    f"[BoundaryLayer1] Failed to stop FFmpeg for {task_id}: {e}",
                    exc_info=True,
                )

        stop_thread = threading.Thread(
            target=stop_decoder_worker, daemon=True, name=f"stop-decoder-{task_id}"
        )
        stop_thread.start()

    def _cleanup_dead_decoder_unsafe(self, task_id: int):
        """
        业务代码：清理已死亡的解码器（纯净）

        注意：此方法必须在持有self.lock的情况下调用。

        Args:
            task_id: 运行键（路由标识）
        """
        self.decoders.pop(task_id, None)
        self.metrics.pop(task_id, None)
        logger.debug(f"[{task_id}] Dead decoder cleaned")

    def get_all_task_ids(self) -> set:
        """获取所有活跃 run 的键（有 decoder 的 task_id 集合）。

        Returns:
            task_id(int) 的集合
        """
        with self.lock:
            return set(self.decoders.keys())

    def get_stream_info(self, task_id: int) -> Optional[Dict[str, Any]]:
        """获取流配置信息（用于重连）

        Returns:
            流配置字典，含 url（已是 rewrite 后的内部拉流地址）；不存在返回 None。
            协议固定 RTSP、fps 取自 config，故无需回传，重连只需 url。
        """
        with self.lock:
            dec = self.decoders.get(task_id)
            if not dec:
                return None
            return {"url": dec.stream_url}

    @log_call(level=logging.INFO, log_args=False)
    def restart_stream(self, task_id: int, stream_url: str) -> bool:
        """
        服务层方法：重启流（不使用 GuardedExecutor）

        职责边界：
        - GuardedExecutor 仅用于 start_stream()（用户发起，可以等待）
        - restart_stream() 不使用 GuardedExecutor（自动重连，不能阻塞）
        - 健康监控器负责重试逻辑（在自己的时间间隔内）

        与 start_stream 的区别：
        - 不创建新的 ClientQueues（保留现有队列）
        - 只重启 FFmpegDecoder
        - 失败时返回 False（不重试，不阻塞）

        Args:
            task_id: 运行键（路由标识）
            stream_url: RTSP 流 URL

        Returns:
            True 表示重启成功，False 表示失败

        异常处理：
            - 捕获所有异常，记录日志，返回 False
            - 不向上传播异常（避免阻塞健康监控线程）
        """
        try:
            # 直接调用实现，不使用 GuardedExecutor
            self._restart_stream_impl(task_id, stream_url)
            return True

        except Exception as e:
            # 记录异常，返回 False（健康监控器会在下一个周期重试）
            logger.warning(
                f"[StreamService] restart_stream failed: {task_id}, "
                f"error={str(e)[:100]}, health monitor will retry in next check cycle"
            )
            return False

    def _restart_stream_impl(self, task_id: int, stream_url: str) -> bool:
        """
        业务代码：重启流实现（纯净，只抛异常）

        Args:
            task_id: 运行键（路由标识）
            stream_url: RTSP 流 URL

        Returns:
            True表示重启成功

        Raises:
            StreamConnectionError: 重启失败
        """
        # 1. 同步停止旧 decoder（kill + reap + join reader，SIGKILL 下 ~ms）。
        #    背景：新旧 decoder 复用同一 ClientQueues.ca_ready（无锁 SPSC deque），
        #    必须确保旧 reader 线程退出后新 reader 才写入，消除双生产者窗口；
        #    同时消除旧进程与新进程/Phase-2 push 在 MediaMTX 同路径上的连接竞争。
        #    重连由健康监控线程驱动，此处几 ms 同步阻塞可接受。
        old_dec = None
        with self.lock:
            old_dec = self.decoders.get(task_id)
        if old_dec:
            old_dec.stop()

        # 2. 清理旧记录并创建新decoder（在锁内执行）
        with self.lock:
            self._cleanup_dead_decoder_unsafe(task_id)

            # 3. 获取现有的ClientQueues（不创建新的）
            cm = _get_client_manager()
            if not cm.has_client(task_id):
                raise StreamConnectionError(
                    url=stream_url,
                    task_id=task_id,   # 此刻无 cq，step_id/source_ip 缺省 None
                    details="Cannot restart stream: no ClientQueues",
                )

            client_queues = cm.get(task_id)

            # 4. 创建新的decoder
            dec = FFmpegDecoder(
                manager=self,
                task_id=task_id,
                stream_url=stream_url,
                decoder_config=self.config.decoder if self.config else None,
                client_queues=client_queues,
            )

            self.decoders[task_id] = dec
            dec.start()

            logger.info(f"[{task_id}] Stream restarted successfully")
            return True

    def get_pending_count(self, task_id: int) -> int:
        """
        获取指定客户端的 CA-Ready-Queue 深度（用于背压控制）

        关键点：检查 CA-Ready-Queue（推理队列）而不是 CA-Raw-Queue
        原因：CA-Raw-Queue 用于落盘，即使满了也不应阻塞拉流
              CA-Ready-Queue 用于推理，如果满了说明推理跟不上，需要丢帧
        """
        cm = _get_client_manager()

        # 先检查客户端是否存在
        if not cm.has_client(task_id):
            return 0

        client_queues = cm.get(task_id)
        if client_queues is None:
            logger.warning(
                f"[BACKPRESSURE] client_queues is None for task_id={task_id}"
            )
            return 0

        depths = client_queues.get_queue_depths()
        if not depths:
            logger.warning(
                f"[BACKPRESSURE] get_queue_depths returned empty for task_id={task_id}"
            )
            return 0

        # 返回 CA-Ready-Queue（推理队列）深度
        return depths.get("ca_ready", 0)

    def shutdown(self):
        """优雅关闭服务，同步等待所有 FFmpeg 解码器退出。

        注意：此方法包含 try/except 块是合理的，因为：
        1. 这是清理代码，需要尽可能完成所有步骤
        2. 一个步骤失败不应阻止其他清理步骤
        3. 不符合"纯净业务代码"原则，但符合资源清理最佳实践
        """
        logger.info("Shutting down StreamService...")

        # 原子地取走所有解码器，防止并发 stop_stream() 重复操作
        with self.lock:
            decoders = list(self.decoders.items())
            self.decoders.clear()

        # 同步逐个停止，确保 FFmpeg 子进程在进程退出前被清理（stop 内含 join reader）
        for task_id, decoder in decoders:
            try:
                decoder.stop()
                logger.debug("[StreamService] Decoder stopped: %s", task_id)
            except Exception as e:
                logger.error(
                    "[StreamService] Error stopping decoder %s: %s",
                    task_id, e, exc_info=True,
                )

        logger.info("StreamService shutdown complete")


# singleton service instance
stream_service = StreamService()
