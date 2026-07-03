"""运行控制：跨服务编排一次 run 的启停（控制面唯一编排出口）。

与 RunRegistry（存储）对仗：Registry 存 run，Controller 控 run 的起/停。
`start_run` / `stop_run` 对称，均在 `client_manager.lock_for(run_key)`（per-run RLock，
`run_key == str(task_id)`）下串行——api（经 asyncio.to_thread）与 HealthMonitor（后台线程）
共用同一把锁，消除「HealthMonitor 迟到 cleanup 误删 /start 刚建 CQ」的竞态。

CQ 构造职责在此（编排者建 CQ → start_workflow(cq)）；source_ip 作被动身份字段注入 CQ，
运行键统一走 `run_key`，不再用 source_ip 当键。

跨域协调居所：允许直接 import 各服务单例来编排（软约定：跨域 import 尽量收敛于此）。
"""

import logging
from typing import Any, Dict, Optional

from app.domain.alarm import ALARM_MODE_SETTLEMENT
from app.services.client.config import get_client_config
from app.services.client.manager import client_manager
from app.services.client.queues import ClientQueues
from app.services.inference.instance import inference_manager
from app.services.persistence import persistence_manager
from app.services.stream import stream_service
from app.utils.exceptions import AppError

logger = logging.getLogger(__name__)


class RunController:
    """跨服务运行编排者（控制面）：对称 start_run / stop_run，共用任务级锁。"""

    def start_run(
        self,
        task_id: int,
        current_step: str,
        rtsp_url: str,
        fps: int = 30,
        source_ip: str = "",
    ) -> Dict[str, Any]:
        """启动一次 run：幂等检查 / 重启清理 → 建 CQ + start_workflow → 起流。

        运行键 `run_key = str(task_id)`。入参均为 primitive（不接触 DB/HTTP）。全程持
        `lock_for(run_key)`，与拆除互斥。幂等命中直接返回；start_workflow 失败抛 AppError。
        CQ 在此构造（编排者持有构造职责），身份含 source_ip 被动字段；stage 由 inference 解析。
        """
        run_key = str(task_id)
        with client_manager.lock_for(run_key):
            # 2a. 幂等 / 重启清理（同 task_id 同槽位；不同 task_id 走不同 run_key，天然并发）
            if client_manager.has_client(run_key):
                old_cq = client_manager.get(run_key)
                if old_cq is not None:
                    cur_url = (stream_service.get_stream_info(run_key) or {}).get("url")
                    # 完全相同（step / URL 均未变）才幂等返回，否则全量重建
                    if old_cq.current_step == current_step and cur_url == rtsp_url:
                        logger.info(
                            "[RunController] start_run idempotent: run=%s task=%s", run_key, task_id
                        )
                        return {
                            "status": "success",
                            "task_id": task_id,
                            "rtsp_url": rtsp_url,
                            "message": f"Task {task_id} already running (idempotent)",
                        }
                    # 字段变化（改 step/url）→ 停旧 run，全量重建（重入 lock_for，无害）
                    logger.info(
                        "[RunController] start_run restart: run=%s (step %s->%s)",
                        run_key, old_cq.current_step, current_step,
                    )
                    self.stop_run(run_key, reason=f"restart:{run_key}")

            # 2b. 建 CQ（构造上移编排者；stage 由 inference 解析，是 CQ 不可变身份的一部分）
            stage = inference_manager.resolve_stage(current_step)
            cq = ClientQueues(
                task_id=task_id,
                current_step=current_step,
                status="running",
                source_ip=source_ip,
                stage=stage,
                **get_client_config().cq_kwargs(),
            )

            # 2c. start_workflow（换槽注册 + open_fresh + Actor）
            if not inference_manager.start_workflow(cq):
                raise AppError(
                    message=f"Failed to start workflow for run {run_key}", client_id=run_key
                )
            logger.info("[RunController] workflow started: run=%s -> task_id=%s", run_key, task_id)

            # 2d. 起流（decoder 键 = run_key，与注册表一致）
            stream_service.start_stream(
                client_id=run_key, stream_url=rtsp_url, fps=fps, protocol="RTSP"
            )
            logger.info("[RunController] stream started: run=%s", run_key)

            return {
                "status": "success",
                "task_id": task_id,
                "rtsp_url": rtsp_url,
                "message": f"Task {task_id} started (run={run_key})",
            }

    def stop_run(
        self,
        run_key: str,
        reason: str,
        *,
        skip_decoder: bool = False,
        expected: Optional[ClientQueues] = None,
    ) -> Dict[str, Any]:
        """拆除一次 run：封闸(DRAINING) → 停 decoder → 落盘残余（settlement/HLS/feature close）→ 清 registry。

        `run_key`（== str(task_id)）唯一定位一次 run。全程持 `lock_for(run_key)`（唯一锁获取点；
        start_run 重入亦经此）。尽力而为：每步独立 try，单步失败不中断后续；永不抛出。
        `skip_decoder=True` 用于孤儿流（decoder 已不存在）。

        `expected`（仅 HealthMonitor 自动结束路径传）：对象身份 fence。HM 在 monitor 线程
        「先决策后拿锁」，决策→拿锁之间槽位可能被 `/start` 重启换成新 CQ。若当前槽位已非
        当初捕获的 `expected`，整段拆除放弃（不停新 run 的 decoder、不清其数据），防误删健康新 run。
        api 控制面（start/terminate）持锁内决策+执行、无 ABA，故不传 expected。
        """
        with client_manager.lock_for(run_key):
            result: Dict[str, Any] = {
                "client_id": run_key,   # 诊断字段（保键名兼容），值为 run_key
                "reason": reason,
                "decoder_stopped": False,
                "data_flushed": False,
                "client_cleaned": False,
                "errors": [],
            }

            cq = client_manager.get(run_key)

            # 0. 对象身份 fence：拆除前先核对槽位仍是当初决策捕获的 cq——不是则整段放弃，
            #    避免误停/误清「同键新实例」（被 /start 抢占重启后装入的新 run）。
            if expected is not None and cq is not expected:
                logger.info(
                    "[RunController] stop_run(reason=%r) skipped by identity fence: %s "
                    "(slot replaced by newer run)", reason, run_key,
                )
                result["skipped"] = True
                return result

            # 0b. 封闸：ACTIVE→DRAINING，封生产者写（decoder 抽帧 / 结果写回 / tick）。
            #     迟到写落到本 CQ 被门拒、不串台到后续新 run；settlement 告警 + HLS flush 仍放行。
            if cq is not None:
                cq.to_draining()

            # 1. 停 decoder（owner=StreamService）
            if not skip_decoder:
                try:
                    stream_service.stop_stream(run_key)
                    result["decoder_stopped"] = True
                except Exception as e:
                    result["errors"].append(f"decoder: {e}")
                    logger.error(
                        "[RunController] stop decoder failed: %s - %s", run_key, e, exc_info=True
                    )

            # 2. 落盘残余数据（按 owner 归位，inference 一把拆、persistence 两个独立 sink）：
            #    ① inference 停 workflow（停 actor + 关 feature 分区）交出 settlement；
            #    ② persistence 落 settlement 告警（别名已由 actor 烧进 alarm.stage）；
            #    ③ 清前端槽 + persistence 落 HLS 残段。
            #    顺序保证：actor.finalize 天然先于①落 settlement；③ flush 先于 step 3 registry.remove
            #    （→cq.close 释放帧）——本 try 早于下方清理。
            try:
                if cq is not None:
                    settlement = inference_manager.stop_workflow(cq)  # Inference owner
                    if settlement:
                        persistence_manager.persist_alarms(
                            settlement,
                            cq=cq,
                            client_id=run_key,
                            mode=ALARM_MODE_SETTLEMENT,
                            log_each=True,
                        )
                    cq.set_latest_temporal([])   # 提前清前端槽，防 WS 读到结束后残留
                    cq.set_latest_rendered(None)
                    persistence_manager.flush_residual_segments(cq)         # Persistence owner
                result["data_flushed"] = True
            except Exception as e:
                result["errors"].append(f"flush: {e}")
                logger.error(
                    "[RunController] flush data failed: %s - %s", run_key, e, exc_info=True
                )

            # 3. 清 registry（owner=ClientManager）：cleanup=True 内含 cq.close()（置 CLOSED + 释放 payload）。
            #    expected 提供 → 对象身份核对删除（remove_if）；否则普通 remove。
            try:
                if expected is not None:
                    removed = client_manager.remove_if(run_key, expected, cleanup=True)
                    result["client_cleaned"] = removed
                elif client_manager.has_client(run_key):
                    removal = client_manager.remove(run_key, cleanup=True)
                    result["client_cleaned"] = removal["removed"]
                    if removal["error"]:
                        result["errors"].append(f"client_manager: {removal['error']}")
            except Exception as e:
                result["errors"].append(f"client_manager: {e}")
                logger.error(
                    "[RunController] clean registry failed: %s - %s", run_key, e, exc_info=True
                )

            if result["errors"]:
                logger.warning(
                    "[RunController] stop_run(reason=%r) completed with errors: %s - %s",
                    reason, run_key, result["errors"],
                )
            else:
                logger.info(
                    "[RunController] stop_run(reason=%r) completed: %s", reason, run_key
                )
            return result


# 全局单例
run_controller = RunController()
