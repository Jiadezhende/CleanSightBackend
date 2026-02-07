"""
全局健康监控服务

独立的全局服务，负责：
1. 监控所有客户端的流健康状态
2. 检测断流并自动重连
3. 重连失败后协调完整清理（Stream + Inference + ClientManager）
4. 检测孤儿流（有 ClientQueues 但没有 Decoder）

职责：
- 监控流健康（读取 ClientQueues.latest_raw_timestamp）
- 自动重连（重启流解码器）
- 孤儿流检测（定期清理无 Decoder 的客户端）
- 完整清理（协调多个模块，类似 API 层）
"""
import time
import threading
import logging
from typing import Optional, Dict, Any, List

from app.services.health_monitor.config import HealthMonitorConfig
from app.services.health_monitor.types import ReconnectState

logger = logging.getLogger(__name__)


class GlobalHealthMonitor:
    """全局健康监控服务

    作为独立的全局服务，有权限协调多个模块进行清理。
    类似于 API 层，但是自动化执行。
    """

    def __init__(
        self,
        client_manager,
        stream_service,
        inference_manager,
        config: Optional[HealthMonitorConfig] = None
    ):
        """初始化全局健康监控

        Args:
            client_manager: ClientManager 实例
            stream_service: StreamService 实例
            inference_manager: InferenceManager 实例
            config: 健康监控配置
        """
        self._client_manager = client_manager
        self._stream_service = stream_service
        self._inference_manager = inference_manager

        # 配置
        self.config = config or HealthMonitorConfig()
        self._check_interval = self.config.check_interval
        self.suspect_timeout = self.config.heartbeat_timeout

        # 使用新的配置参数名 reconnect_interval（旧名 restart_delay）
        self.reconnect_interval = self.config.reconnect_interval
        self.max_reconnect_attempts = self.config.max_reconnect_attempts

        # 计算派生超时值
        self.cleanup_timeout = self.config.heartbeat_timeout + (
            self.config.reconnect_interval * self.config.max_reconnect_attempts
        )
        self.orphan_timeout = self.config.orphan_timeout

        # 重连成功判定阈值（与心跳超时一致）
        self.reconnect_success_threshold = self.config.heartbeat_timeout

        # 线程控制
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 重连状态
        self._reconnecting_clients: Dict[str, ReconnectState] = {}

        # 孤儿流跟踪（记录最后活跃时间）
        self._last_activity: Dict[str, float] = {}

        # 统计
        self._stats = {
            "checks": 0,
            "suspects": 0,
            "cleanups": 0,
            "reconnects": 0,
            "reconnect_successes": 0,
            "orphans_detected": 0
        }

    def start(self):
        """启动监控线程"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[GlobalHealthMonitor] Already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="GlobalHealthMonitor"
        )
        self._thread.start()
        logger.info(
            "[GlobalHealthMonitor] Started (check_interval=%.1fs, timeout=%.1fs)",
            self._check_interval,
            self.suspect_timeout
        )

    def stop(self):
        """停止监控线程"""
        if self._thread is None or not self._thread.is_alive():
            return

        logger.info("[GlobalHealthMonitor] Stopping...")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        logger.info("[GlobalHealthMonitor] Stopped")

    def _monitor_loop(self):
        """监控循环"""
        logger.info("[GlobalHealthMonitor] Monitor loop started")

        while not self._stop_event.is_set():
            try:
                self._check_all_clients()
                self._stats["checks"] += 1
                self._stop_event.wait(timeout=self._check_interval)
            except Exception as e:
                logger.error(f"[GlobalHealthMonitor] Error in monitor loop: {e}", exc_info=True)
                time.sleep(1.0)

        logger.info("[GlobalHealthMonitor] Monitor loop exited")

    def _check_all_clients(self):
        """检查所有客户端的健康状态（含孤儿流检测）"""
        current_time = time.time()
        all_clients = self._client_manager.get_all_clients()

        # 获取所有活跃的解码器
        active_decoders = set(self._stream_service.get_all_client_ids())

        # 统计
        self._stats["checks"] += 1

        for client_id, cq in all_clients.items():
            # 1. 检查是否在重连模式
            if client_id in self._reconnecting_clients:
                self._handle_reconnecting_client(client_id, cq, current_time)
                continue

            # 2. 检查是否有活跃的解码器
            has_decoder = client_id in active_decoders

            if has_decoder:
                # 有解码器：检查流健康
                last_frame_time = cq.latest_raw_timestamp

                # 防御性检查：如果 timestamp 异常为 0，跳过
                if last_frame_time == 0:
                    logger.warning(f"[GlobalHealthMonitor] WARN: {client_id} has zero timestamp (unexpected)")
                    continue

                idle_time = current_time - last_frame_time

                # 进入重连模式（suspect_timeout ~ cleanup_timeout 之间）
                if idle_time >= self.suspect_timeout and idle_time < self.cleanup_timeout:
                    self._enter_reconnect_mode(client_id, last_frame_time)

                # 超过 cleanup_timeout，放弃重连，执行清理
                elif idle_time >= self.cleanup_timeout:
                    logger.warning(
                        f"[GlobalHealthMonitor] TIMEOUT: {client_id}, "
                        f"no frames for {idle_time:.1f}s, giving up reconnect"
                    )
                    self._exit_reconnect_mode(client_id, cleanup=True)
            else:
                # 无解码器：检查是否为孤儿流
                self._handle_potential_orphan(client_id, cq, current_time)

    def _enter_reconnect_mode(self, client_id: str, last_frame_time: float):
        """进入重连模式"""
        # 从 StreamService 获取流配置
        stream_info = self._stream_service.get_stream_info(client_id)
        if not stream_info:
            logger.debug(f"[GlobalHealthMonitor] Cannot enter reconnect mode: no stream info for {client_id} (decoder may not be ready yet)")
            return

        self._reconnecting_clients[client_id] = ReconnectState(
            client_id=client_id,
            stream_url=stream_info['url'],
            fps=stream_info['fps'],
            protocol=stream_info['protocol'],
            attempt_count=0,
            last_attempt_time=0,  # 初始为 0，表示还未尝试
            last_frame_time_before_disconnect=last_frame_time  # 记录断流前的最后帧时间
        )

        logger.warning(
            f"[GlobalHealthMonitor] RECONNECT MODE: {client_id}, "
            f"will retry every {self.reconnect_interval}s (max {self.max_reconnect_attempts} times)"
        )
        self._stats["suspects"] += 1

    def _handle_reconnecting_client(self, client_id: str, cq, current_time: float):
        """处理重连中的客户端"""
        state = self._reconnecting_clients[client_id]

        # 检查是否有新帧（重连成功）
        new_frame_time = cq.latest_raw_timestamp
        if new_frame_time > state.last_frame_time_before_disconnect:
            # 检查新帧是否足够新（避免误判）
            frame_age = current_time - new_frame_time
            if frame_age < self.reconnect_success_threshold:
                logger.info(
                    f"[GlobalHealthMonitor] RECONNECT SUCCESS: {client_id}, "
                    f"new frames detected (attempt {state.attempt_count})"
                )
                self._stats["reconnect_successes"] += 1
                self._exit_reconnect_mode(client_id, cleanup=False)
                return

        # 检查是否到达重连间隔
        time_since_last_attempt = current_time - state.last_attempt_time
        if time_since_last_attempt < self.reconnect_interval:
            logger.debug(
                f"[GlobalHealthMonitor] {client_id} waiting for reconnect interval "
                f"(elapsed={time_since_last_attempt:.1f}s, need={self.reconnect_interval}s, "
                f"attempts={state.attempt_count}/{self.max_reconnect_attempts})"
            )
            return

        # 检查是否超过最大重连次数
        if state.attempt_count >= self.max_reconnect_attempts:
            idle_time = current_time - new_frame_time
            logger.error(
                f"[GlobalHealthMonitor] RECONNECT FAILED: {client_id}, "
                f"no frames for {idle_time:.1f}s, "
                f"max attempts ({self.max_reconnect_attempts}) reached"
            )
            self._exit_reconnect_mode(client_id, cleanup=True)
            return

        # 尝试重连
        state.attempt_count += 1
        state.last_attempt_time = current_time
        state.last_frame_time_before_disconnect = new_frame_time

        logger.info(
            f"[GlobalHealthMonitor] RECONNECT ATTEMPT {state.attempt_count}/{self.max_reconnect_attempts}: "
            f"{client_id}"
        )
        self._stats["reconnects"] += 1

        # 调用 StreamService 重启 decoder
        # 职责边界：健康监控器自己管理重试逻辑，不依赖 GuardedExecutor
        success = self._stream_service.restart_stream(
            client_id=client_id,
            stream_url=state.stream_url,
            fps=state.fps,
            protocol=state.protocol
        )

        if success:
            logger.debug(f"[GlobalHealthMonitor] Decoder restarted for {client_id}, waiting for frames...")
        else:
            # 重试将在下一个检查周期自动触发（由 reconnect_interval 控制）
            logger.warning(
                f"[GlobalHealthMonitor] Reconnect attempt {state.attempt_count} failed for {client_id}, "
                f"will retry in {self.reconnect_interval}s"
            )

        # 在下一次检查周期判断是否有新帧到达（无论本次成功与否）

    def _exit_reconnect_mode(self, client_id: str, cleanup: bool):
        """退出重连模式

        Args:
            client_id: 客户端ID
            cleanup: 是否执行完整清理
        """
        if client_id in self._reconnecting_clients:
            del self._reconnecting_clients[client_id]

        if cleanup:
            # 重连失败，执行完整清理（类似 /api/terminate）
            self._stats["cleanups"] += 1
            self._cleanup_failed_client(client_id)

    def cleanup_client(
        self,
        client_id: str,
        reason: str,
        *,
        skip_decoder: bool = False
    ) -> Dict[str, Any]:
        """清理协调器（唯一的清理入口点）

        职责边界：
        - 这是唯一的清理入口点
        - 所有清理操作（API、健康监控、孤儿流）都通过此方法
        - 协调三个模块的清理：StreamService + InferenceManager + ClientManager

        Args:
            client_id: 客户端ID
            reason: 清理原因（用于日志和调试）
            skip_decoder: 是否跳过解码器清理（孤儿流使用）

        Returns:
            清理结果字典：
            {
                "client_id": str,
                "reason": str,
                "decoder_stopped": bool,
                "data_flushed": bool,
                "client_cleaned": bool,
                "errors": List[str]
            }

        线程安全：
            - 此方法是线程安全的（可以从 API 或健康监控线程调用）
            - 委托给线程安全的服务方法

        异常处理：
            - 尽力而为：即使某步骤失败也继续执行后续步骤
            - 永不抛出异常
            - 返回详细的每步状态
        """
        result = {
            "client_id": client_id,
            "reason": reason,
            "decoder_stopped": False,
            "data_flushed": False,
            "client_cleaned": False,
            "errors": []
        }

        logger.info(
            f"[GlobalHealthMonitor] cleanup_client: {client_id}, reason='{reason}', "
            f"skip_decoder={skip_decoder}"
        )

        # 步骤 1: 停止解码器（除非跳过）
        if not skip_decoder:
            try:
                if self._stream_service.has_stream(client_id):
                    self._stream_service.stop_stream(client_id)
                    result["decoder_stopped"] = True
                    logger.info(f"[GlobalHealthMonitor] Decoder stopped: {client_id}")
            except Exception as e:
                result["errors"].append(f"decoder: {e}")
                logger.error(f"[GlobalHealthMonitor] Failed to stop decoder: {client_id} - {e}")

        # 步骤 2: 落盘残余数据（总是尝试）
        try:
            self._inference_manager.remove_client(client_id)
            result["data_flushed"] = True
            logger.info(f"[GlobalHealthMonitor] Data flushed: {client_id}")
        except Exception as e:
            result["errors"].append(f"flush: {e}")
            logger.error(f"[GlobalHealthMonitor] Failed to flush data: {client_id} - {e}")

        # 步骤 3: 清理 ClientManager（总是尝试）
        try:
            if self._client_manager.has_client(client_id):
                removal_result = self._client_manager.remove_client(client_id, cleanup=True)
                result["client_cleaned"] = removal_result["removed"]
                if removal_result["error"]:
                    result["errors"].append(f"client_manager: {removal_result['error']}")
                logger.info(f"[GlobalHealthMonitor] ClientManager cleaned: {client_id}")
        except Exception as e:
            result["errors"].append(f"client_manager: {e}")
            logger.error(f"[GlobalHealthMonitor] Failed to clean ClientManager: {client_id} - {e}")

        if result["errors"]:
            logger.warning(
                f"[GlobalHealthMonitor] Cleanup completed with errors: {client_id}\n"
                f"Errors: {result['errors']}"
            )
        else:
            logger.info(f"[GlobalHealthMonitor] Cleanup completed successfully: {client_id}")

        return result

    def _cleanup_failed_client(self, client_id: str):
        """清理重连失败的客户端（委托给 cleanup_client）

        职责边界：
        - 此方法只是 cleanup_client() 的包装器
        - 实际清理逻辑集中在 cleanup_client() 中
        """
        logger.error(
            f"[GlobalHealthMonitor] ⚠️  STREAM CONNECTION FAILED: {client_id}\n"
            f"Reason: Reconnect failed after {self.max_reconnect_attempts} attempts\n"
            f"Action: Executing full cleanup..."
        )

        # 委托给统一的清理方法
        result = self.cleanup_client(
            client_id=client_id,
            reason=f"Reconnect failed after {self.max_reconnect_attempts} attempts"
        )

        if result["errors"]:
            logger.error(
                f"[GlobalHealthMonitor] Cleanup completed with errors: {client_id}\n"
                f"Errors: {result['errors']}\n"
                f"Action: Call /api/start to restart the stream."
            )
        else:
            logger.info(
                f"[GlobalHealthMonitor] Full cleanup completed: {client_id}\n"
                f"Action: Call /api/start to restart the stream."
            )

    def _handle_potential_orphan(self, client_id: str, cq, current_time: float):
        """处理潜在的孤儿流（有 ClientQueues 但没有 Decoder）

        职责边界：
        - 检测孤儿流（有 ClientQueues 但无 Decoder）
        - 委托清理给 cleanup_client(skip_decoder=True)

        Args:
            client_id: 客户端ID
            cq: ClientQueues 实例
            current_time: 当前时间戳
        """
        last_frame_time = cq.latest_raw_timestamp

        # 更新最后活跃时间
        if client_id not in self._last_activity:
            self._last_activity[client_id] = last_frame_time

        # 计算空闲时间
        idle_time = current_time - last_frame_time

        # 如果超过孤儿流超时时间，执行完整清理
        if idle_time >= self.orphan_timeout:
            logger.warning(
                f"[GlobalHealthMonitor] ORPHAN STREAM detected: {client_id}, "
                f"idle for {idle_time:.1f}s (no decoder), cleaning up"
            )
            self._stats["orphans_detected"] += 1

            # 委托给统一的清理方法（跳过解码器）
            result = self.cleanup_client(
                client_id=client_id,
                reason=f"Orphan stream (idle for {idle_time:.1f}s)",
                skip_decoder=True  # 孤儿流没有解码器
            )

            # 清理活跃时间记录
            self._last_activity.pop(client_id, None)

            if result["errors"]:
                logger.error(f"[GlobalHealthMonitor] Orphan cleanup with errors: {client_id} - {result['errors']}")
            else:
                logger.info(f"[GlobalHealthMonitor] Orphan cleanup completed: {client_id}")

    def get_stats(self):
        """获取监控统计信息"""
        return {
            **self._stats,
            "reconnecting_count": len(self._reconnecting_clients),
            "reconnecting_clients": list(self._reconnecting_clients.keys())
        }

    def get_system_status(self) -> Dict[str, Any]:
        """获取系统整体状态（包含所有客户端及其队列状态）

        职责边界：
        - 健康监控负责系统级别的状态汇总
        - 整合来自多个模块的信息（ClientManager、StreamService、InferenceManager）
        - 提供统一的系统状态视图

        Returns:
            系统状态字典：
            {
                "clients": {
                    "total": int,
                    "active_streams": int,
                    "reconnecting": int,
                    "orphans": int
                },
                "queues": {
                    "client_id": {
                        "raw_queue_size": int,
                        "ready_queue_size": int,
                        "latest_timestamp": float,
                        ...
                    }
                },
                "monitor_stats": {
                    "checks": int,
                    "suspects": int,
                    "cleanups": int,
                    ...
                }
            }
        """
        # 获取所有客户端队列
        all_clients = self._client_manager.get_all_clients()

        # 获取活跃的解码器
        active_decoders = set(self._stream_service.get_all_client_ids())

        # 统计客户端类型
        total_clients = len(all_clients)
        active_streams = len(active_decoders)
        reconnecting = len(self._reconnecting_clients)

        # 孤儿流数量 = 有队列但无解码器的客户端
        orphans = total_clients - active_streams - reconnecting
        orphans = max(0, orphans)  # 确保不为负数

        # 构建队列状态字典
        queue_stats = {}
        for client_id, cq in all_clients.items():
            queue_stats[client_id] = cq.to_status_dict()

        # 返回完整的系统状态
        return {
            "clients": {
                "total": total_clients,
                "active_streams": active_streams,
                "reconnecting": reconnecting,
                "orphans": orphans
            },
            "queues": queue_stats,
            "monitor_stats": {
                **self._stats,
                "reconnecting_count": reconnecting,
                "reconnecting_clients": list(self._reconnecting_clients.keys())
            }
        }
