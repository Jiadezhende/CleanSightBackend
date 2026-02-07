"""
客户端清理服务

提供统一的客户端资源清理接口，确保原子化、容错的清理流程。
支持后台定期检查孤儿流（有ClientQueues但没有Decoder的流）。
"""
import logging
import threading
import time
from typing import Dict, Any, Optional

from app.services.stream.config import CleanupConfig

logger = logging.getLogger(__name__)


class CleanupService:
    """客户端资源清理服务

    职责：
    - 提供统一的客户端清理接口
    - 原子化清理，每步独立try-except
    - 永不抛出异常，返回清理结果
    - 后台定期检查孤儿流（有ClientQueues但没有Decoder的流）
    """

    def __init__(self, stream_service, client_manager, inference_manager, cleanup_config: Optional[CleanupConfig] = None):
        """初始化清理服务

        Args:
            stream_service: StreamService实例，用于清理解码器
            client_manager: ClientManager实例，用于清理客户端队列
            inference_manager: InferenceManager实例，用于清理推理资源
            cleanup_config: 清理配置对象（如果未提供则使用默认配置）
        """
        self._stream_service = stream_service
        self._client_manager = client_manager
        self._inference_manager = inference_manager

        # 使用配置对象
        self.config = cleanup_config or CleanupConfig()

        # 后台线程相关
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 跟踪客户端最后活跃时间
        self._last_activity: Dict[str, float] = {}

    def cleanup_client(self, client_id: str, reason: str = "manual") -> Dict[str, Any]:
        """清理客户端资源（尽力而为，永不抛异常）

        执行步骤：
        1. 清理InferenceManager（落盘残余数据，清理ClientManager）
        2. 清理StreamService中的解码器（停止ffmpeg进程）

        Args:
            client_id: 客户端ID
            reason: 清理原因（manual, timeout, api_stop等）

        Returns:
            清理结果字典，包含以下字段：
            - client_id: 客户端ID
            - reason: 清理原因
            - decoder_cleaned: 解码器是否清理成功
            - inference_cleaned: 推理资源是否清理成功
            - errors: 清理过程中的错误列表
        """
        result = {
            "client_id": client_id,
            "reason": reason,
            "decoder_cleaned": False,
            "inference_cleaned": False,
            "errors": []
        }

        logger.info(f"[CleanupService] Cleaning up client: {client_id} (reason: {reason})")

        # 诊断信息（DEBUG级别）
        if self._client_manager:
            has_client = self._client_manager.has_client(client_id)
            client_count = len(self._client_manager.get_all_clients())
            logger.debug(f"[CleanupService] ClientManager state: has_client={has_client}, total={client_count}")
        else:
            logger.warning(f"[CleanupService] ClientManager is None")

        # 步骤1：清理InferenceManager（优先落盘数据，然后清理ClientManager）
        try:
            if self._inference_manager:
                self._inference_manager.remove_client(client_id)
                result["inference_cleaned"] = True
                logger.info(f"[CleanupService] Inference cleaned: {client_id}")
            else:
                logger.warning(f"[CleanupService] InferenceManager is None")
        except Exception as e:
            error_msg = f"inference: {e}"
            result["errors"].append(error_msg)
            logger.warning(f"[CleanupService] Inference cleanup failed: {client_id} - {e}")

        # 步骤2：清理StreamService中的解码器
        try:
            self._stream_service.stop_stream(client_id)
            result["decoder_cleaned"] = True
            logger.info(f"[CleanupService] Decoder cleaned: {client_id}")
        except Exception as e:
            error_msg = f"decoder: {e}"
            result["errors"].append(error_msg)
            logger.warning(f"[CleanupService] Decoder cleanup failed: {client_id} - {e}")

        # 记录清理完成
        if result["errors"]:
            logger.warning(f"[CleanupService] Cleanup complete with errors: {client_id} - {result['errors']}")
        else:
            logger.info(f"[CleanupService] Cleanup complete: {client_id}")

        return result

    def start(self):
        """启动后台清理线程"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[CleanupService] Background thread already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="CleanupService"
        )
        self._thread.start()
        logger.info(
            f"[CleanupService] Background thread started "
            f"(check_interval={self.config.check_interval}s, orphan_timeout={self.config.orphan_timeout}s)"
        )

    def stop(self):
        """停止后台清理线程"""
        if self._thread is None or not self._thread.is_alive():
            return

        logger.info("[CleanupService] Stopping background thread...")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        logger.info("[CleanupService] Background thread stopped")

    def _cleanup_loop(self):
        """后台清理循环：定期检查孤儿流"""
        logger.info("[CleanupService] Cleanup loop started")

        while not self._stop_event.is_set():
            try:
                self._check_orphan_streams()
                # 等待下一次检查
                self._stop_event.wait(timeout=self.config.check_interval)
            except Exception as e:
                logger.error(f"[CleanupService] Error in cleanup loop: {e}", exc_info=True)
                time.sleep(1.0)

        logger.info("[CleanupService] Cleanup loop exited")

    def _check_orphan_streams(self):
        """检查孤儿流（有ClientQueues但没有Decoder的流）"""
        try:
            current_time = time.time()

            # 获取所有客户端
            all_clients = self._client_manager.get_all_clients()
            if not all_clients:
                return

            # 获取所有活跃的decoder
            active_decoders = set(self._stream_service.get_all_client_ids())

            # 找出孤儿流（有ClientQueues但没有Decoder）
            orphan_clients = set(all_clients.keys()) - active_decoders

            if orphan_clients:
                logger.debug(f"[CleanupService] Found {len(orphan_clients)} potential orphan streams")

            for client_id in orphan_clients:
                cq = all_clients[client_id]
                last_frame_time = cq.latest_raw_timestamp

                # 更新最后活跃时间
                if client_id not in self._last_activity:
                    self._last_activity[client_id] = last_frame_time

                # 计算空闲时间
                idle_time = current_time - last_frame_time

                # 如果超过孤儿流超时时间，清理资源
                if idle_time >= self.config.orphan_timeout:
                    logger.warning(
                        f"[CleanupService] Orphan stream detected: {client_id}, "
                        f"idle for {idle_time:.1f}s, cleaning up"
                    )
                    self.cleanup_client(client_id, reason="orphan_timeout")
                    # 清理后删除活跃时间记录
                    self._last_activity.pop(client_id, None)

            # 清理已经不存在的客户端的活跃时间记录
            for client_id in list(self._last_activity.keys()):
                if client_id not in all_clients:
                    self._last_activity.pop(client_id, None)

        except Exception as e:
            logger.error(f"[CleanupService] Error checking orphan streams: {e}", exc_info=True)


# 全局单例（延迟初始化）
cleanup_service: Optional[CleanupService] = None


def init_cleanup_service(stream_service, client_manager, inference_manager, cleanup_config: Optional[CleanupConfig] = None):
    """初始化全局清理服务实例

    Args:
        stream_service: StreamService实例
        client_manager: ClientManager实例
        inference_manager: InferenceManager实例
        cleanup_config: 清理配置对象（如果未提供则使用默认配置）
    """
    global cleanup_service
    if cleanup_service is None:
        cleanup_service = CleanupService(stream_service, client_manager, inference_manager, cleanup_config)
        logger.info("[CleanupService] Global cleanup service initialized")
