"""
流健康监控服务

后台守护线程，定期检查所有客户端的健康状态，基于帧时间戳检测断流并自动清理。
支持自动重连机制。
"""
import time
import threading
import logging
from typing import Optional, Dict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReconnectState:
    """重连状态"""
    client_id: str
    stream_url: str  # 保存流URL
    fps: int  # 保存FPS
    protocol: str  # 保存协议
    attempt_count: int  # 当前尝试次数
    last_attempt_time: float  # 上次尝试时间
    last_frame_time_before_disconnect: float  # 断流前的最后帧时间


class StreamHealthMonitor:
    """流健康监控器

    职责：
    - 后台守护线程，定期检查所有客户端的健康状态
    - 读取 ClientQueues.latest_raw_timestamp 判断是否有新帧
    - 超时策略：5秒警告，30秒（6次重连）清理
    - 自动重连：检测到断流后周期性尝试重连
    """

    def __init__(self, client_manager, cleanup_service, stream_service, check_interval=3.0):
        """初始化健康监控器

        Args:
            client_manager: ClientManager实例，用于获取所有客户端
            cleanup_service: CleanupService实例，用于清理超时客户端
            stream_service: StreamService实例，用于重启流
            check_interval: 检查间隔（秒），默认3秒
        """
        self._client_manager = client_manager
        self._cleanup_service = cleanup_service
        self._stream_service = stream_service
        self._check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 超时阈值（秒）
        self.suspect_timeout = 5.0  # 5秒警告，进入重连模式
        self.cleanup_timeout = 30.0  # 30秒（6次重连失败后）清理

        # 重连参数
        self.reconnect_interval = 5.0  # 5秒尝试一次
        self.max_reconnect_attempts = 6  # 最多6次
        self.reconnect_success_threshold = 5.0  # 5秒内有新帧视为成功

        # 重连状态跟踪
        self._reconnecting_clients: Dict[str, ReconnectState] = {}

        # 统计信息
        self._stats = {
            "checks": 0,
            "suspects": 0,
            "cleanups": 0,
            "reconnects": 0,
            "reconnect_successes": 0
        }

    def start(self):
        """启动监控线程"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[StreamHealthMonitor] Already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="StreamHealthMonitor"
        )
        self._thread.start()
        logger.info("[StreamHealthMonitor] Started (check_interval=%.1fs)", self._check_interval)

    def stop(self):
        """停止监控线程"""
        if self._thread is None or not self._thread.is_alive():
            return

        logger.info("[StreamHealthMonitor] Stopping...")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        logger.info("[StreamHealthMonitor] Stopped (stats: %s)", self._stats)

    def _monitor_loop(self):
        """监控循环：定期检查所有客户端"""
        logger.info("[StreamHealthMonitor] Monitor loop started")

        while not self._stop_event.is_set():
            try:
                # 获取所有客户端
                clients = self._client_manager.get_all_clients()
                current_time = time.time()

                # 统计
                self._stats["checks"] += 1

                # 检查每个客户端
                for client_id, cq in clients.items():
                    self._check_client_health(client_id, cq, current_time)

                # 等待下一次检查
                self._stop_event.wait(timeout=self._check_interval)

            except Exception as e:
                logger.error(f"[StreamHealthMonitor] Error in monitor loop: {e}", exc_info=True)
                # 出错后短暂等待，避免疯狂重试
                time.sleep(1.0)

        logger.info("[StreamHealthMonitor] Monitor loop exited")

    def _check_client_health(self, client_id: str, cq, current_time: float):
        """检查单个客户端健康状态（支持自动重连）

        Args:
            client_id: 客户端ID
            cq: ClientQueues实例
            current_time: 当前时间戳
        """
        try:
            # 1. 检查是否在重连模式
            if client_id in self._reconnecting_clients:
                self._handle_reconnecting_client(client_id, cq, current_time)
                return

            # 2. 正常监控逻辑
            last_frame_time = cq.latest_raw_timestamp

            # 防御性检查：如果 timestamp 异常为 0，跳过
            # 注：latest_raw_timestamp 现在初始化为创建时间，所以正常情况下不会为 0
            if last_frame_time == 0:
                logger.warning(f"[StreamHealthMonitor] WARN: {client_id} has zero timestamp (unexpected)")
                return

            # 计算距离最后一帧的时间
            time_since_last_frame = current_time - last_frame_time

            # 3. 进入重连模式（5秒无新帧，且未超过30秒）
            if time_since_last_frame >= self.suspect_timeout and time_since_last_frame < self.cleanup_timeout:
                if client_id not in self._reconnecting_clients:
                    self._enter_reconnect_mode(client_id, cq, last_frame_time)

            # 4. 超过30秒，放弃重连，清理资源
            elif time_since_last_frame >= self.cleanup_timeout:
                logger.warning(
                    f"[StreamHealthMonitor] TIMEOUT: {client_id}, "
                    f"no frames for {time_since_last_frame:.1f}s, giving up reconnect"
                )
                self._exit_reconnect_mode(client_id, cleanup=True)

        except Exception as e:
            logger.error(f"[StreamHealthMonitor] Error checking {client_id}: {e}")

    def _enter_reconnect_mode(self, client_id: str, cq, last_frame_time: float):
        """进入重连模式"""
        # 从StreamService获取流配置
        stream_info = self._stream_service.get_stream_info(client_id)
        if not stream_info:
            logger.error(f"Cannot enter reconnect mode: no stream info for {client_id}")
            return

        self._reconnecting_clients[client_id] = ReconnectState(
            client_id=client_id,
            stream_url=stream_info['url'],
            fps=stream_info['fps'],
            protocol=stream_info['protocol'],
            attempt_count=0,
            last_attempt_time=0,
            last_frame_time_before_disconnect=last_frame_time
        )

        logger.warning(
            f"[StreamHealthMonitor] RECONNECT MODE: {client_id}, "
            f"will retry every {self.reconnect_interval}s (max {self.max_reconnect_attempts} times)"
        )
        self._stats["suspects"] += 1

    def _handle_reconnecting_client(self, client_id: str, cq, current_time: float):
        """处理正在重连的客户端"""
        state = self._reconnecting_clients[client_id]

        # 检查是否有新帧（重连成功）
        new_frame_time = cq.latest_raw_timestamp
        if new_frame_time > state.last_frame_time_before_disconnect:
            logger.info(
                f"[StreamHealthMonitor] RECONNECT SUCCESS: {client_id}, "
                f"new frames detected (attempt {state.attempt_count})"
            )
            self._stats["reconnect_successes"] += 1
            self._exit_reconnect_mode(client_id, cleanup=False)
            return

        # 检查是否该尝试重连了
        if current_time - state.last_attempt_time < self.reconnect_interval:
            return  # 还没到重连时间

        # 检查是否超过最大次数
        if state.attempt_count >= self.max_reconnect_attempts:
            logger.error(
                f"[StreamHealthMonitor] RECONNECT FAILED: {client_id}, "
                f"max attempts ({self.max_reconnect_attempts}) reached"
            )
            self._exit_reconnect_mode(client_id, cleanup=True)
            return

        # 尝试重连
        state.attempt_count += 1
        state.last_attempt_time = current_time

        logger.info(
            f"[StreamHealthMonitor] RECONNECT ATTEMPT {state.attempt_count}/{self.max_reconnect_attempts}: "
            f"{client_id}"
        )
        self._stats["reconnects"] += 1

        # 调用StreamService重启decoder
        success = self._stream_service.restart_stream(
            client_id=client_id,
            stream_url=state.stream_url,
            fps=state.fps,
            protocol=state.protocol
        )

        if not success:
            logger.warning(f"[StreamHealthMonitor] Reconnect attempt failed to restart decoder for {client_id}")
            return

        # 在下一次检查周期判断是否有新帧到达

    def _exit_reconnect_mode(self, client_id: str, cleanup: bool):
        """退出重连模式"""
        if client_id in self._reconnecting_clients:
            del self._reconnecting_clients[client_id]

        if cleanup:
            # 清理资源
            self._stats["cleanups"] += 1
            self._cleanup_service.cleanup_client(client_id, reason="reconnect_timeout")

    def get_stats(self):
        """获取监控统计信息"""
        return self._stats.copy()
