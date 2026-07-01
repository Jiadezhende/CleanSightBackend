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

import logging
import threading
import time
from typing import Any, Dict, List, Optional

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
        config: Optional[HealthMonitorConfig] = None,
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

        # 累计统计（生命周期内的总计）
        self._stats = {
            "checks": 0,
            "suspects": 0,
            "cleanups": 0,
            "reconnects": 0,
            "reconnect_successes": 0,
            "orphans_detected": 0,
        }

        # 实时客户端状态统计（每轮检查更新）
        self._client_stats = {
            "total_clients": 0,
            "active_streams": 0,
            "reconnecting": 0,
            "orphan_streams": 0,
            "orphan_decoders": 0,
        }

    def start(self):
        """启动监控线程"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[GlobalHealthMonitor] Already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="GlobalHealthMonitor"
        )
        self._thread.start()
        logger.info(
            "[GlobalHealthMonitor] Started (check_interval=%.1fs, timeout=%.1fs)",
            self._check_interval,
            self.suspect_timeout,
        )

    def stop(self):
        """停止监控线程"""
        if self._thread is None or not self._thread.is_alive():
            return
        
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        logger.info("[GlobalHealthMonitor] Stopped")

    def _monitor_loop(self):
        """监控循环"""

        while not self._stop_event.is_set():
            try:
                self._check_all_clients()
                self._stats["checks"] += 1
                self._stop_event.wait(timeout=self._check_interval)
            except Exception as e:
                logger.error(
                    f"[GlobalHealthMonitor] Error in monitor loop: {e}", exc_info=True
                )
                time.sleep(1.0)

    def _check_all_clients(self):
        """检查所有客户端的健康状态（含孤儿流检测和孤儿解码器检测）"""
        current_time = time.time()
        all_clients = self._client_manager.snapshot()

        # 获取所有活跃的解码器
        active_decoders = set(self._stream_service.get_all_client_ids())
        reconnecting_set = set(self._reconnecting_clients.keys())

        # 集中统计所有客户端状态（确保分类互斥）
        self._client_stats = {
            "total_clients": len(all_clients),
            "active_streams": len(active_decoders - reconnecting_set),
            "reconnecting": len(reconnecting_set),
            "orphan_streams": sum(
                1
                for cid in all_clients
                if cid not in active_decoders and cid not in reconnecting_set
            ),
            "orphan_decoders": sum(
                1
                for cid in active_decoders
                if cid not in all_clients and cid not in reconnecting_set
            ),
        }

        # 第一轮：检查有队列的客户端
        for client_id, cq in all_clients.items():
            # 1. 检查是否在重连模式
            if client_id in self._reconnecting_clients:
                self._handle_reconnecting_client(client_id, cq, current_time)
                continue

            # 2. 检查任务运行时长是否超过最大限制
            if self.config.task_max_duration > 0:
                task_started_at = getattr(cq, "task_started_at", 0.0)
                if task_started_at > 0:
                    task_age = current_time - task_started_at
                    if task_age >= self.config.task_max_duration:
                        self._handle_task_timeout(client_id, cq, task_age)
                        continue

            # 3. 检查是否有活跃的解码器
            has_decoder = client_id in active_decoders

            if has_decoder:
                # 有解码器：检查流健康
                last_frame_time = cq.latest_raw_timestamp

                # 防御性检查：如果 timestamp 异常为 0，跳过
                if last_frame_time == 0:
                    logger.warning(
                        "[GlobalHealthMonitor] WARN: %s has zero timestamp (unexpected)", client_id
                    )
                    continue

                idle_time = current_time - last_frame_time

                # 进入重连模式（suspect_timeout ~ cleanup_timeout 之间）
                if (
                    idle_time >= self.suspect_timeout
                    and idle_time < self.cleanup_timeout
                ):
                    self._enter_reconnect_mode(client_id, last_frame_time)

                # 超过 cleanup_timeout，放弃重连，执行清理
                elif idle_time >= self.cleanup_timeout:
                    logger.warning(
                        "[GlobalHealthMonitor] TIMEOUT: %s, no frames for %.1fs, giving up reconnect",
                        client_id, idle_time
                    )
                    self._exit_reconnect_mode(client_id, cleanup=True)
            else:
                # 无解码器：检查是否为孤儿流（有队列但无解码器）
                self._handle_potential_orphan(client_id, cq, current_time)

        # 第二轮：检查孤儿解码器（有解码器但无队列）
        orphan_decoders = active_decoders - set(all_clients.keys())
        for client_id in orphan_decoders:
            # 跳过重连中的客户端（它们可能暂时没有队列）
            if client_id not in self._reconnecting_clients:
                self._handle_orphan_decoder(client_id)

    def _enter_reconnect_mode(self, client_id: str, last_frame_time: float):
        """进入重连模式"""
        # 从 StreamService 获取流配置
        stream_info = self._stream_service.get_stream_info(client_id)
        if not stream_info:
            logger.debug(
                "[GlobalHealthMonitor] Cannot enter reconnect mode: no stream info for %s (decoder may not be ready yet)", client_id
            )
            return

        self._reconnecting_clients[client_id] = ReconnectState(
            client_id=client_id,
            stream_url=stream_info["url"],
            fps=stream_info["fps"],
            protocol=stream_info["protocol"],
            attempt_count=0,
            last_attempt_time=0,  # 初始为 0，表示还未尝试
            last_frame_time_before_disconnect=last_frame_time,  # 记录断流前的最后帧时间
        )

        logger.warning(
            "[GlobalHealthMonitor] RECONNECT MODE: %s, will retry every %ss (max %d times)",
            client_id, self.reconnect_interval, self.max_reconnect_attempts
        )
        self._stats["suspects"] += 1  # 累计统计：进入重连模式的次数

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
                    "[GlobalHealthMonitor] RECONNECT SUCCESS: %s, new frames detected (attempt %d)",
                    client_id, state.attempt_count
                )
                self._stats["reconnect_successes"] += 1  # 累计统计：重连成功的次数
                self._exit_reconnect_mode(client_id, cleanup=False)
                return

        # 检查是否到达重连间隔
        time_since_last_attempt = current_time - state.last_attempt_time
        if time_since_last_attempt < self.reconnect_interval:
            logger.debug(
                "[GlobalHealthMonitor] %s waiting for reconnect interval "
                "(elapsed=%.1fs, need=%ss, attempts=%d/%d)",
                client_id, time_since_last_attempt, self.reconnect_interval,
                state.attempt_count, self.max_reconnect_attempts
            )
            return

        # 检查是否超过最大重连次数
        if state.attempt_count >= self.max_reconnect_attempts:
            idle_time = current_time - new_frame_time
            logger.error(
                "[GlobalHealthMonitor] RECONNECT FAILED: %s, no frames for %.1fs, max attempts (%d) reached",
                client_id, idle_time, self.max_reconnect_attempts
            )
            self._exit_reconnect_mode(client_id, cleanup=True)
            return

        # 尝试重连
        state.attempt_count += 1
        state.last_attempt_time = current_time
        state.last_frame_time_before_disconnect = new_frame_time

        logger.info(
            "[GlobalHealthMonitor] RECONNECT ATTEMPT %d/%d: %s",
            state.attempt_count, self.max_reconnect_attempts, client_id
        )
        self._stats["reconnects"] += 1  # 累计统计：重连尝试的总次数

        # 调用 StreamService 重启 decoder
        # 职责边界：健康监控器自己管理重试逻辑，不依赖 GuardedExecutor
        success = self._stream_service.restart_stream(
            client_id=client_id,
            stream_url=state.stream_url,
            fps=state.fps,
            protocol=state.protocol,
        )

        if success:
            logger.debug(
                "[GlobalHealthMonitor] Decoder restarted for %s, waiting for frames...", client_id
            )
        else:
            # 重试将在下一个检查周期自动触发（由 reconnect_interval 控制）
            logger.warning(
                "[GlobalHealthMonitor] Reconnect attempt %d failed for %s, will retry in %ss",
                state.attempt_count, client_id, self.reconnect_interval
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
            self._stats["cleanups"] += 1  # 累计统计：清理操作的次数
            self._cleanup_failed_client(client_id)

    def cleanup_client(
        self, client_id: str, reason: str, *, skip_decoder: bool = False
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
        logger.info(
            "[GlobalHealthMonitor] cleanup_client: %s, reason='%s', skip_decoder=%s",
            client_id, reason, skip_decoder
        )

        # 步骤 0: 清理监控器自身的客户端状态（HealthMonitor 专属，防内存泄漏）
        self._reconnecting_clients.pop(client_id, None)
        self._last_activity.pop(client_id, None)

        # 步骤 1-3: 委托给 RunController（唯一拆除实现：停 decoder → 落盘 → 清 registry）
        from app.services.run_control import run_controller

        return run_controller.stop_run(client_id, reason, skip_decoder=skip_decoder)

    def _cleanup_failed_client(self, client_id: str):
        """清理重连失败的客户端（委托给 cleanup_client）

        职责边界：
        - 此方法只是 cleanup_client() 的包装器
        - 实际清理逻辑集中在 cleanup_client() 中
        """
        logger.error(
            "[GlobalHealthMonitor] STREAM CONNECTION FAILED: %s | "
            "Reason: Reconnect failed after %d attempts | Action: Executing full cleanup...",
            client_id, self.max_reconnect_attempts
        )

        # 委托给统一的清理方法
        result = self.cleanup_client(
            client_id=client_id,
            reason=f"Reconnect failed after {self.max_reconnect_attempts} attempts",
        )

        if result["errors"]:
            logger.error(
                "[GlobalHealthMonitor] Cleanup completed with errors: %s | Errors: %s | Action: Call /api/start to restart the stream.",
                client_id, result['errors']
            )
        else:
            logger.info(
                "[GlobalHealthMonitor] Full cleanup completed: %s | Action: Call /api/start to restart the stream.",
                client_id
            )

    def _handle_task_timeout(self, client_id: str, cq, task_age: float):
        """处理任务超时：仅执行运维治理动作，不产出业务告警。"""
        task_id = cq.get_task_id()
        hours = task_age / 3600
        max_hours = self.config.task_max_duration / 3600

        logger.error(
            "[GlobalHealthMonitor] TASK TIMEOUT: client=%s, task_id=%s, "
            "running=%.1fh, max=%.1fh",
            client_id, task_id, hours, max_hours,
        )

        self._stats["cleanups"] += 1
        self.cleanup_client(client_id, reason=f"Task timeout ({task_age:.0f}s)")

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
            self._stats["orphans_detected"] += 1  # 累计统计：检测到孤儿的总次数

            # 委托给统一的清理方法（跳过解码器）
            result = self.cleanup_client(
                client_id=client_id,
                reason=f"Orphan stream (idle for {idle_time:.1f}s)",
                skip_decoder=True,  # 孤儿流没有解码器
            )

            # 清理活跃时间记录
            self._last_activity.pop(client_id, None)

            if result["errors"]:
                logger.error(
                    "[GlobalHealthMonitor] Orphan cleanup with errors: %s - %s", client_id, result['errors']
                )
            else:
                logger.info(
                    "[GlobalHealthMonitor] Orphan cleanup completed: %s", client_id
                )

    def _handle_orphan_decoder(self, client_id: str):
        """处理孤儿解码器（有 Decoder 但没有 ClientQueues）

        职责边界：
        - 检测孤儿解码器（有 Decoder 但无 ClientQueues）
        - 立即停止并移除无用的解码器

        Args:
            client_id: 客户端ID
        """
        logger.warning(
            f"[GlobalHealthMonitor] ORPHAN DECODER detected: {client_id}, "
            f"decoder running but no client queue found, stopping decoder"
        )
        self._stats["orphans_detected"] += 1

        # 无条件调用 stop_stream()：即使进程已死（is_alive=False），
        # 仍需从 decoders 字典中移除条目，否则下一轮检查会重复检测到孤儿。
        # 绕过 stop_run（无 CQ），故显式持 lock_for 防与并发 start 撞。
        try:
            with self._client_manager.lock_for(client_id):
                self._stream_service.stop_stream(client_id)
            logger.info(
                f"[GlobalHealthMonitor] Orphan decoder stopped: {client_id}"
            )
        except Exception as e:
            logger.error(
                "[GlobalHealthMonitor] Failed to stop orphan decoder: %s - %s", client_id, e, exc_info=True
            )

    def get_stats(self):
        """获取监控统计信息"""
        return {
            **self._stats,
            "reconnecting_count": len(self._reconnecting_clients),
            "reconnecting_clients": list(self._reconnecting_clients.keys()),
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
                    "orphan_streams": int,
                    "orphan_decoders": int
                },
                "monitor_stats": {
                    "checks": int,
                    "suspects": int,
                    "cleanups": int,
                    ...
                }
            }
        """

        # 返回完整的系统状态（使用 _check_all_clients 中统计的实时数据）
        return {
            "clients": self._client_stats,
            "monitor_stats": {
                **self._stats,
                "reconnecting_clients": list(self._reconnecting_clients.keys()),
            },
        }
