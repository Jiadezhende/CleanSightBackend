"""运行控制：跨服务编排一次 run 的启停（控制面唯一编排出口）。

与 RunRegistry（存储）对仗：Registry 存 run，Controller 控 run 的起/停。
`start_run` / `stop_run` 对称，均在 `client_manager.lock_for(client_id)`（per-client RLock）
下串行——api（经 asyncio.to_thread）与 HealthMonitor（后台线程）共用同一把锁，
消除「HealthMonitor 迟到 cleanup 误删 /start 刚建 CQ」的竞态。

跨域协调居所：允许直接 import 各服务单例来编排（软约定：跨域 import 尽量收敛于此）。

注：当前按 `client_id` 键（与现有代码一致）。task_id 单键等演进见 docs/update/20260628_*。
"""

import logging
from typing import Any, Dict

from app.domain.task import CleaningTask
from app.services.client.manager import client_manager
from app.services.inference.instance import inference_manager
from app.services.stream import stream_service
from app.utils.exceptions import AppError

logger = logging.getLogger(__name__)


class RunController:
    """跨服务运行编排者（控制面）：对称 start_run / stop_run，共用任务级锁。"""

    def start_run(
        self,
        client_id: str,
        task_id: int,
        current_step: str,
        rtsp_url: str,
        fps: int = 30,
    ) -> Dict[str, Any]:
        """启动一次 run：幂等检查 / 重启清理 → set_task（建 CQ+Actor）→ 起流。

        入参均为 primitive（不接触 DB/HTTP）。全程持 `lock_for(client_id)`，与拆除互斥。
        幂等命中直接返回；set_task 失败抛 AppError（由上层 FastAPI handler 处理）。
        """
        with client_manager.lock_for(client_id):
            # 2a. 幂等 / 重启清理
            if client_manager.has_client(client_id):
                cq = client_manager.get(client_id)
                if cq is not None:
                    old_task = cq.get_task()
                    old_task_id = cq.get_task_id()
                    cur_url = (stream_service.get_stream_info(client_id) or {}).get("url")
                    # 完全相同（task_id / step / URL 均未变）才幂等返回，否则全量重建
                    if (
                        old_task_id == task_id
                        and old_task is not None
                        and old_task.current_step == current_step
                        and cur_url == rtsp_url
                    ):
                        logger.info(
                            "[RunController] start_run idempotent: %s task=%s", client_id, task_id
                        )
                        return {
                            "status": "success",
                            "client_id": client_id,
                            "task_id": task_id,
                            "rtsp_url": rtsp_url,
                            "message": f"Task {task_id} already running (idempotent)",
                        }
                    # 任何字段变化 → 停旧 run，全量重建（重入 lock_for，无害）
                    logger.info(
                        "[RunController] start_run restart: %s (task %s->%s)",
                        client_id, old_task_id, task_id,
                    )
                    self.stop_run(client_id, reason=f"restart:{old_task_id}->{task_id}")

            # 2b. set_task（建 CQ + Actor）
            task = CleaningTask(task_id=task_id, current_step=current_step, status="running")
            if not inference_manager.set_task(client_id, task):
                raise AppError(
                    message=f"Failed to set task for client {client_id}", client_id=client_id
                )
            logger.info("[RunController] task set: %s -> task_id=%s", client_id, task_id)

            # 2c. 起流
            stream_service.start_stream(
                client_id=client_id, stream_url=rtsp_url, fps=fps, protocol="RTSP"
            )
            logger.info("[RunController] stream started: %s", client_id)

            return {
                "status": "success",
                "client_id": client_id,
                "task_id": task_id,
                "rtsp_url": rtsp_url,
                "message": f"Task {task_id} started for client {client_id}",
            }

    def stop_run(
        self, client_id: str, reason: str, *, skip_decoder: bool = False
    ) -> Dict[str, Any]:
        """拆除一次 run：停 decoder → 落盘残余（settlement/HLS/feature close）→ 清 registry。

        全程持 `lock_for(client_id)`（唯一锁获取点；start_run 重入亦经此）。
        尽力而为：每步独立 try，单步失败不中断后续；永不抛出。
        `skip_decoder=True` 用于孤儿流（decoder 已不存在）。
        """
        with client_manager.lock_for(client_id):
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

            # 2. 落盘残余数据（owner=InferenceManager：settlement + HLS flush + feature/fact close）
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
                    removal = client_manager.remove(client_id, cleanup=True)
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
