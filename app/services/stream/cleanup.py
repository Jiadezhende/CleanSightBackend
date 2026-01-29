"""
客户端清理服务

提供统一的客户端资源清理接口，确保原子化、容错的清理流程。
"""
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class CleanupService:
    """客户端资源清理服务

    职责：
    - 提供统一的客户端清理接口
    - 原子化清理，每步独立try-except
    - 永不抛出异常，返回清理结果
    """

    def __init__(self, stream_service, client_manager, inference_manager):
        """初始化清理服务

        Args:
            stream_service: StreamService实例，用于清理解码器
            client_manager: ClientManager实例，用于清理客户端队列
            inference_manager: InferenceManager实例，用于清理推理资源
        """
        self._stream_service = stream_service
        self._client_manager = client_manager
        self._inference_manager = inference_manager

    def cleanup_client(self, client_id: str, reason: str = "manual") -> Dict[str, Any]:
        """清理客户端资源（尽力而为，永不抛异常）

        执行步骤：
        1. 清理StreamService中的解码器（停止ffmpeg进程）
        2. 清理InferenceManager（包括ClientManager清理）

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

        # 步骤1：清理StreamService中的解码器
        try:
            self._stream_service.stop_stream(client_id)
            result["decoder_cleaned"] = True
            logger.info(f"[CleanupService] Decoder cleaned: {client_id}")
        except Exception as e:
            error_msg = f"decoder: {e}"
            result["errors"].append(error_msg)
            logger.warning(f"[CleanupService] Decoder cleanup failed: {client_id} - {e}")

        # 步骤2：清理InferenceManager（会自动清理ClientManager）
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

        # 记录清理完成
        if result["errors"]:
            logger.warning(f"[CleanupService] Cleanup complete with errors: {client_id} - {result['errors']}")
        else:
            logger.info(f"[CleanupService] Cleanup complete: {client_id}")

        return result


# 全局单例（延迟初始化）
cleanup_service: Optional[CleanupService] = None


def init_cleanup_service(stream_service, client_manager, inference_manager):
    """初始化全局清理服务实例

    Args:
        stream_service: StreamService实例
        client_manager: ClientManager实例
        inference_manager: InferenceManager实例
    """
    global cleanup_service
    if cleanup_service is None:
        cleanup_service = CleanupService(stream_service, client_manager, inference_manager)
        logger.info("[CleanupService] Global cleanup service initialized")
