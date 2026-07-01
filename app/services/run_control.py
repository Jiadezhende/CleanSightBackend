"""运行控制：跨服务编排一次 run 的启停（控制面唯一编排出口）。

与 RunRegistry（存储）对仗：Registry 存 run，Controller 控 run 的起/停。
本模块把原先重复的两份三步拆除（`api._manual_cleanup_fallback` 与
`HealthMonitor.cleanup_client`）合并为唯一实现 `stop_run`。

跨域协调居所：允许直接 import 各服务单例来编排（软约定：跨域 import 尽量收敛于此）。

注：当前按 `client_id` 键（与现有代码一致）。task_id 单键、CQ 状态机 /
对象身份 fencing、start_run 等演进见 docs/update/20260628_* 的 T1–T6。
"""

import logging
from typing import Any, Dict

from app.services.client.manager import client_manager
from app.services.inference.instance import inference_manager
from app.services.stream import stream_service

logger = logging.getLogger(__name__)


class RunController:
    """跨服务运行编排者（控制面）。"""

    def stop_run(
        self, client_id: str, reason: str, *, skip_decoder: bool = False
    ) -> Dict[str, Any]:
        """拆除一次 run：停 decoder → 落盘残余（settlement/HLS/feature close）→ 清 registry。

        尽力而为：每步独立 try，单步失败不中断后续；永不抛出。
        `skip_decoder=True` 用于孤儿流（decoder 已不存在）。
        返回每步状态的结果字典（与旧 cleanup_client 同形，供调用方判定 status）。
        """
        result: Dict[str, Any] = {
            "client_id": client_id,
            "reason": reason,
            "decoder_stopped": False,
            "data_flushed": False,
            "client_cleaned": False,
            "errors": [],
        }

        # 1. 停 decoder（owner=StreamService）
        if not skip_decoder:
            try:
                stream_service.stop_stream(client_id)
                result["decoder_stopped"] = True
            except Exception as e:
                result["errors"].append(f"decoder: {e}")
                logger.error(
                    "[RunController] stop decoder failed: %s - %s", client_id, e, exc_info=True
                )

        # 2. 落盘残余数据（owner=InferenceManager：actor.finalize settlement + HLS flush + feature/fact close）
        try:
            inference_manager.remove_client(client_id)
            result["data_flushed"] = True
        except Exception as e:
            result["errors"].append(f"flush: {e}")
            logger.error(
                "[RunController] flush data failed: %s - %s", client_id, e, exc_info=True
            )

        # 3. 清 registry（owner=ClientManager）
        try:
            if client_manager.has_client(client_id):
                removal = client_manager.remove_client(client_id, cleanup=True)
                result["client_cleaned"] = removal["removed"]
                if removal["error"]:
                    result["errors"].append(f"client_manager: {removal['error']}")
        except Exception as e:
            result["errors"].append(f"client_manager: {e}")
            logger.error(
                "[RunController] clean registry failed: %s - %s", client_id, e, exc_info=True
            )

        if result["errors"]:
            logger.warning(
                "[RunController] stop_run(reason=%r) completed with errors: %s - %s",
                reason, client_id, result["errors"],
            )
        else:
            logger.info(
                "[RunController] stop_run(reason=%r) completed: %s", reason, client_id
            )
        return result


# 全局单例
run_controller = RunController()
