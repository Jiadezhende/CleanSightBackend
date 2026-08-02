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

        # 配置：各阈值一律在用处直读 `self.config.*`，**不在此摊成同名实例属性**。
        # 那层拷贝原是给 cleanup_timeout 的派生式（heartbeat + interval×attempts）安身的；
        # 派生式删掉后它们全成了恒等副本，只是把「这个数打哪来」多藏了一跳。
        self.config = config or HealthMonitorConfig()

        # 线程控制
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 重连状态（键 = task_id）
        self._reconnecting_clients: Dict[int, ReconnectState] = {}

        # 累计统计（生命周期内的总计）。三个重连计数是一组，读法：
        # 检测到断线 disconnects 次 → 发起 respawn reconnects 次 → 恢复 reconnect_successes 次。
        self._stats = {
            "checks": 0,
            "disconnects": 0,          # 检测到 decoder 进程死、进入重连模式的次数（含首启失败）
            "cleanups": 0,
            "reconnects": 0,           # 发起 respawn 的次数
            "reconnect_successes": 0,  # 真来新帧、判定恢复的次数
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
        # 报 cleanup_timeout 而非 heartbeat_timeout：前者才是这条线上唯一还在做判定的时限
        # （「可疑」区间随进程死活判据一并删除，heartbeat_timeout 现只作重连成功的新帧新鲜度阈值）。
        logger.info(
            "[GlobalHealthMonitor] Started (check_interval=%.1fs, cleanup_timeout=%.1fs)",
            self.config.check_interval,
            self.config.cleanup_timeout,
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
                self._stop_event.wait(timeout=self.config.check_interval)
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
        active_decoders = set(self._stream_service.get_all_task_ids())
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
        for task_id, cq in all_clients.items():
            # 1. 检查是否在重连模式
            if task_id in self._reconnecting_clients:
                self._handle_reconnecting_client(task_id, cq, current_time)
                continue

            # 2. 检查任务运行时长是否超过最大限制
            if self.config.task_max_duration > 0:
                task_started_at = getattr(cq, "task_started_at", 0.0)
                if task_started_at > 0:
                    task_age = current_time - task_started_at
                    if task_age >= self.config.task_max_duration:
                        self._handle_task_timeout(task_id, cq, task_age)
                        continue

            # 3. 检查是否有活跃的解码器
            has_decoder = task_id in active_decoders

            if has_decoder:
                # 判据 = decoder 子进程死活（非帧 staleness）。实测：RTSP 断流时 ffmpeg 从 TCP
                # 控制通道即收 EOF 退出（且 -timeout 兜底把真挂死也转成退出），故「进程是否活」
                # 能干净区分：进程死=断流/崩溃(该重启)，进程活但无帧=等首个关键帧/瞬时停(该等)。
                last_frame_time = cq.latest_raw_timestamp
                idle_time = current_time - last_frame_time

                if not self._stream_service.is_decoder_alive(task_id):
                    # 进程已退出（断流 EOF / 崩溃 / 首启失败）→ 进入重连（respawn）。
                    # 比旧的「等 5s 帧 staleness」更快：下一 tick 即感知。
                    self._enter_reconnect_mode(task_id, last_frame_time, cq)
                elif last_frame_time > 0 and idle_time >= self.config.cleanup_timeout:
                    # 进程活着却长时间无帧（真挂死正常已被 decoder 的 -timeout 转成进程死，
                    # 此为最后防线）→ 放弃清理。
                    logger.warning(
                        "[GlobalHealthMonitor] TIMEOUT: %s alive but no frames for %.1fs, giving up",
                        task_id, idle_time
                    )
                    self._exit_reconnect_mode(task_id, cleanup=True)
                # else: 进程活着且无帧未超时（等首帧/恢复中）→ 只等，不动
            else:
                # 无解码器：检查是否为孤儿流（有队列但无解码器）
                self._handle_potential_orphan(task_id, cq, current_time)

        # 第二轮：检查孤儿解码器（有解码器但无队列）
        orphan_decoders = active_decoders - set(all_clients.keys())
        for task_id in orphan_decoders:
            # 跳过重连中的客户端（它们可能暂时没有队列）
            if task_id not in self._reconnecting_clients:
                self._handle_orphan_decoder(task_id)

    def _enter_reconnect_mode(self, task_id: int, last_frame_time: float, cq):
        """进入重连模式：decoder 进程已死，起 respawn 循环（捕获 cq 作对象身份 fence 基准）。"""
        # 从 StreamService 获取流配置
        stream_info = self._stream_service.get_stream_info(task_id)
        if not stream_info:
            logger.debug(
                "[GlobalHealthMonitor] Cannot enter reconnect mode: no stream info for %s (decoder may not be ready yet)", task_id
            )
            return

        self._reconnecting_clients[task_id] = ReconnectState(
            task_id=task_id,
            stream_url=stream_info["url"],
            last_attempt_time=0,  # 初始为 0，表示还未尝试
            last_frame_time_before_disconnect=last_frame_time,  # 记录断流前的最后帧时间
            cq=cq,  # 捕获进入重连时的 CQ，作为拆除时的对象身份核对基准
        )

        logger.warning(
            "[GlobalHealthMonitor] RECONNECT MODE: %s, decoder process dead; "
            "will respawn every %ss until frames resume or cleanup_timeout(%.0fs)",
            task_id, self.config.reconnect_interval, self.config.cleanup_timeout
        )
        self._stats["disconnects"] += 1  # 累计统计：检测到断线、进入重连模式的次数

    def _handle_reconnecting_client(self, task_id: int, cq, current_time: float):
        """处理重连中的客户端"""
        state = self._reconnecting_clients[task_id]

        # 对象身份 fence：当前槽位 cq 已非进入重连时捕获的 cq_A（被 /start 重启换槽），
        # 说明本次重连针对的 run 已被新 run 取代——放弃本次重连，绝不误动新 run。
        if state.cq is not None and cq is not state.cq:
            logger.info(
                "[GlobalHealthMonitor] Reconnect abandoned (slot replaced by newer run): %s",
                task_id,
            )
            del self._reconnecting_clients[task_id]
            return

        # 成功判定 = 真的来了新帧（不是「进程活着」——respawn 后新进程还在等首个关键帧时
        # 也活着但无帧，此刻不能判成功、更不能重杀）。新帧须足够新，避免读到陈旧 ts 误判。
        new_frame_time = cq.latest_raw_timestamp
        if new_frame_time > state.last_frame_time_before_disconnect:
            frame_age = current_time - new_frame_time
            # 新鲜度阈值复用 heartbeat_timeout（此处直读而非另起别名：别名会把
            # 「调 heartbeat_timeout 会连带动到重连成功判定」这层耦合藏起来）
            if frame_age < self.config.heartbeat_timeout:
                logger.info(
                    "[GlobalHealthMonitor] RECONNECT SUCCESS: %s, new frames detected", task_id
                )
                self._stats["reconnect_successes"] += 1  # 累计统计：重连成功的次数
                self._exit_reconnect_mode(task_id, cleanup=False)
                return

        # 放弃判定 = 纯时间：无帧时长 ≥ cleanup_timeout（不再数重连次数）。
        idle_time = current_time - new_frame_time
        if idle_time >= self.config.cleanup_timeout:
            logger.error(
                "[GlobalHealthMonitor] RECONNECT FAILED: %s, no frames for %.1fs "
                "(>= cleanup_timeout %.0fs), giving up",
                task_id, idle_time, self.config.cleanup_timeout
            )
            self._exit_reconnect_mode(task_id, cleanup=True)
            return

        # 进程活着但还没来帧（respawn 已起活进程、正等首个关键帧 / 连接中）→ 只等，
        # 绝不 restart 一个活着的进程（否则把「等首帧被杀」的 bug 搬进重连循环）。
        if self._stream_service.is_decoder_alive(task_id):
            return

        # 进程仍死：按 reconnect_interval 节流后 respawn（无次数上限，由上面的 cleanup_timeout 收口）。
        if current_time - state.last_attempt_time < self.config.reconnect_interval:
            return
        state.last_attempt_time = current_time
        self._stats["reconnects"] += 1  # 累计统计：respawn 次数

        # respawn dead decoder。restart_stream 自带成功("Stream restarted successfully")/
        # 失败("restart_stream failed: <error>")日志，此处不再重复记，避免一次 attempt 打双 WARNING。
        # 成功 → 下一 tick 检测到新帧走 RECONNECT SUCCESS；失败 → 下个 reconnect_interval 再试，
        # 无帧满 cleanup_timeout 收口。
        logger.info(
            "[GlobalHealthMonitor] RECONNECT ATTEMPT: %s (respawn dead decoder)", task_id
        )
        self._stream_service.restart_stream(task_id=task_id, stream_url=state.stream_url)

    def _exit_reconnect_mode(self, task_id: int, cleanup: bool):
        """退出重连模式

        Args:
            task_id: 客户端ID
            cleanup: 是否执行完整清理
        """
        # 捕获进入重连时的 cq_A（在删除 state 前取），作为拆除时的对象身份核对基准。
        state = self._reconnecting_clients.pop(task_id, None)

        if cleanup:
            # 重连失败，执行完整清理（类似 /api/terminate）
            self._stats["cleanups"] += 1  # 累计统计：清理操作的次数
            self._cleanup_failed_client(
                task_id, expected=state.cq if state else None
            )

    def cleanup_client(
        self,
        task_id: int,
        reason: str,
        *,
        skip_decoder: bool = False,
        expected=None,
    ) -> Dict[str, Any]:
        """清理协调器（唯一的清理入口点）

        职责边界：
        - 这是唯一的清理入口点
        - 所有清理操作（API、健康监控、孤儿流）都通过此方法
        - 协调三个模块的清理：StreamService + InferenceManager + ClientManager

        Args:
            task_id: 客户端ID
            reason: 清理原因（用于日志和调试）
            skip_decoder: 是否跳过解码器清理（孤儿流使用）
            expected: 决策时捕获的 CQ 对象（对象身份 fence 基准）。HM 自动结束路径传入，
                stop_run 核对当前槽位仍是它才拆除，防过期决策误删被 /start 换上的新 run。

        Returns:
            清理结果字典：
            {
                "task_id": str,
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
            task_id, reason, skip_decoder
        )

        # 步骤 0: 清理监控器自身的客户端状态（HealthMonitor 专属，防内存泄漏）
        self._reconnecting_clients.pop(task_id, None)

        # 步骤 1-3: 委托给 RunController（唯一拆除实现：封闸 → 停 decoder → 落盘 → 清 registry）
        from app.services.run_control import run_controller

        return run_controller.stop_run(
            task_id, reason, skip_decoder=skip_decoder, expected=expected
        )

    def _cleanup_failed_client(self, task_id: int, expected=None):
        """清理重连失败的客户端（委托给 cleanup_client）

        职责边界：
        - 此方法只是 cleanup_client() 的包装器
        - 实际清理逻辑集中在 cleanup_client() 中

        `expected`：进入重连时捕获的 cq_A，透传给 stop_run 作对象身份 fence。
        """
        logger.error(
            "[GlobalHealthMonitor] STREAM CONNECTION FAILED: %s | "
            "Reason: no frames within cleanup_timeout(%.0fs) | Action: Executing full cleanup...",
            task_id, self.config.cleanup_timeout
        )

        # 委托给统一的清理方法
        result = self.cleanup_client(
            task_id=task_id,
            reason=f"No frames within cleanup_timeout ({self.config.cleanup_timeout:.0f}s)",
            expected=expected,
        )

        if result["errors"]:
            logger.error(
                "[GlobalHealthMonitor] Cleanup completed with errors: %s | Errors: %s | Action: Call /api/start to restart the stream.",
                task_id, result['errors']
            )
        else:
            logger.info(
                "[GlobalHealthMonitor] Full cleanup completed: %s | Action: Call /api/start to restart the stream.",
                task_id
            )

    def _handle_task_timeout(self, task_id: int, cq, task_age: float):
        """处理任务超时：仅执行运维治理动作，不产出业务告警。"""
        # 注：入参 task_id 即 cq.task_id（client_id/task_id 合一后同一个键），不再重取覆盖
        logger.error(
            "[GlobalHealthMonitor] TASK TIMEOUT: task_id=%s, running=%.1fh, max=%.1fh",
            task_id, task_age / 3600, self.config.task_max_duration / 3600,
        )

        self._stats["cleanups"] += 1
        # cq 即本轮 snapshot 迭代所持的槽位对象，传作对象身份 fence 基准。
        self.cleanup_client(
            task_id, reason=f"Task timeout ({task_age:.0f}s)", expected=cq
        )

    def _handle_potential_orphan(self, task_id: int, cq, current_time: float):
        """处理潜在的孤儿流（有 ClientQueues 但没有 Decoder）

        职责边界：
        - 检测孤儿流（有 ClientQueues 但无 Decoder）
        - 委托清理给 cleanup_client(skip_decoder=True)

        Args:
            task_id: 客户端ID
            cq: ClientQueues 实例
            current_time: 当前时间戳
        """
        # 空闲时长直接由帧 ts 算（不另存"最后活跃时间"：cq.latest_raw_timestamp 本身就是它，
        # 再存一份只会多一处要同步的状态）
        idle_time = current_time - cq.latest_raw_timestamp

        # 如果超过孤儿流超时时间，执行完整清理
        if idle_time >= self.config.orphan_timeout:
            logger.warning(
                f"[GlobalHealthMonitor] ORPHAN STREAM detected: {task_id}, "
                f"idle for {idle_time:.1f}s (no decoder), cleaning up"
            )
            self._stats["orphans_detected"] += 1  # 累计统计：检测到孤儿的总次数

            # 委托给统一的清理方法（跳过解码器）；cq 传作对象身份 fence 基准。
            result = self.cleanup_client(
                task_id=task_id,
                reason=f"Orphan stream (idle for {idle_time:.1f}s)",
                skip_decoder=True,  # 孤儿流没有解码器
                expected=cq,
            )

            if result["errors"]:
                logger.error(
                    "[GlobalHealthMonitor] Orphan cleanup with errors: %s - %s", task_id, result['errors']
                )
            else:
                logger.info(
                    "[GlobalHealthMonitor] Orphan cleanup completed: %s", task_id
                )

    def _handle_orphan_decoder(self, task_id: int):
        """处理孤儿解码器（有 Decoder 但没有 ClientQueues）

        职责边界：
        - 检测孤儿解码器（有 Decoder 但无 ClientQueues）
        - 立即停止并移除无用的解码器

        Args:
            task_id: 客户端ID
        """
        logger.warning(
            f"[GlobalHealthMonitor] ORPHAN DECODER detected: {task_id}, "
            f"decoder running but no client queue found, stopping decoder"
        )
        self._stats["orphans_detected"] += 1

        # 无条件调用 stop_stream()：即使进程已死（is_alive=False），
        # 仍需从 decoders 字典中移除条目，否则下一轮检查会重复检测到孤儿。
        # 绕过 stop_run（无 CQ），故显式持 lock_for 防与并发 start 撞。
        try:
            with self._client_manager.lock_for(task_id):
                self._stream_service.stop_stream(task_id)
            logger.info(
                f"[GlobalHealthMonitor] Orphan decoder stopped: {task_id}"
            )
        except Exception as e:
            logger.error(
                "[GlobalHealthMonitor] Failed to stop orphan decoder: %s - %s", task_id, e, exc_info=True
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
                    "disconnects": int,
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
