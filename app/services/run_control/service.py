"""运行控制：跨服务编排一次 run 的启停（控制面唯一编排出口）。

`start_run` / `stop_run` / `restart_stream` / `stop_orphan_decoder` 都在 `lock_for(task_id)`
（每 task 一把 RLock，L0 编排锁）下执行——api（经 asyncio.to_thread）与 HealthMonitor（后台线程）
共用同一把锁。生命周期写（session open / seal / close、decoder 启停、actor 增删）只在这里发生。

HealthMonitor「先决策后拿锁」：决策时捕获的 `Session` 作 fence（`expected=`），拿锁后注册项已不是它
就放弃，不误动被 `/start` 换代后的新 run。

跨域协调居所：允许直接 import 各服务单例来编排（软约定：跨域 import 尽量收敛于此）。
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

from app.types.alarm import ALARM_MODE_SETTLEMENT
from app.storage import runs
from app.services.inference.online.instance import inference_service
from app.services.inference.online.temporal import alarm_sink
from app.services.inference.online.topics import LATEST_TEMPORAL
from app.services.recording.instance import recording_service
from app.services.session.core import Session, SessionInfo
from app.services.session.instance import session_service
from app.services.session.topics import RENDERED
from app.services.stream.instance import stream_service
from app.services.stream.service import internal_stream_url
from app.types.exceptions import AppError, ValidationError

logger = logging.getLogger(__name__)


class RunControlService:
    """跨服务运行编排者（控制面）：对称 start_run / stop_run，共用任务级锁。"""

    def __init__(self):
        # L0 编排锁：每 task 一把 RLock（start_run 持锁内重入 stop_run 不自死锁）
        self._task_locks: Dict[int, threading.RLock] = {}
        self._task_locks_guard = threading.Lock()   # L2：只护 get-or-create

    def lock_for(self, task_id: int) -> threading.RLock:
        """该 task 的生命周期锁（get-or-create）。"""
        with self._task_locks_guard:
            lk = self._task_locks.get(task_id)
            if lk is None:
                lk = self._task_locks[task_id] = threading.RLock()
            return lk

    def start_run(
        self,
        task_id: int,
        current_step: str,
        rtsp_url: str,
        source_ip: str = "",
    ) -> Dict[str, Any]:
        """启动一次 run：幂等检查 / 重启清理 → 分配 run 目录 → 注册 session + start_workflow → 起流。

        运行键 = **`task_id`(int)**。入参均为 primitive（不接触 DB/HTTP）。全程持
        `lock_for(task_id)`，与拆除互斥。幂等命中直接返回；start_workflow 失败抛 AppError。
        """
        # 边界层解析 + 参数校验（锁外、动旧 run 之前）：字符串 current_step（DB clean_task.current_step）
        # → int step_id 一次转换；非数字 / 推理配置未定义的 step 属参数错误 → ValidationError（400）。
        try:
            step_id = int(current_step)
        except (TypeError, ValueError):
            raise ValidationError(
                f"current_step '{current_step}' 不是数字", field="current_step", value=str(current_step),
            ) from None
        # stage 由 inference 解析，是 session 不可变身份的一部分
        stage = inference_service.resolve_stage(step_id)

        with self.lock_for(task_id):
            # 2a. 幂等 / 重启清理（同 task_id 同槽位；不同 task_id 走不同键，天然并发）
            old = session_service.get(task_id)
            if old is not None:
                # decoder 记的是改写后的内部地址，入参先过同一改写再比
                cur_url = (stream_service.get_stream_info(task_id) or {}).get("url")
                # 完全相同（step / URL 均未变）才幂等返回，否则全量重建
                if old.info.run.step_id == step_id and cur_url == internal_stream_url(rtsp_url):
                    logger.info(
                        "[RunControlService] start_run idempotent: task=%s", task_id
                    )
                    return {
                        "status": "success",
                        "task_id": task_id,
                        "rtsp_url": rtsp_url,
                        "message": f"Task {task_id} already running (idempotent)",
                    }
                # 字段变化（改 step/url）→ 停旧 run，全量重建（重入 lock_for，无害）
                logger.info(
                    "[RunControlService] start_run restart: task=%s (step %s->%s)",
                    task_id, old.info.run.step_id, step_id,
                )
                self.stop_run(task_id, reason=f"restart:{task_id}")

            # 2b. 分配 run（必须在 lock_for 内、早于注册：同 step 的分配串行，run_id 才严格递增）
            try:
                run = runs.allocate(task_id, step_id)
            except OSError as e:
                raise AppError(
                    message=f"Failed to allocate run dir for task {task_id}: {e}",
                    task_id=task_id,
                    step_id=step_id,
                    source_ip=source_ip,
                ) from e

            # 2c. 注册 session。之后的 setup 步包进 try：任一步失败即回滚，session 不泄漏在注册表
            sess = session_service.open(SessionInfo(
                run=run, source_ip=source_ip, stage=stage, started_at=time.time(),
            ))
            try:
                # 2d. start_workflow（建 Actor）
                if not inference_service.start_workflow(sess):
                    raise AppError(
                        message=f"Failed to start workflow for task {task_id}",
                        task_id=task_id,
                        step_id=run.step_id,
                        source_ip=source_ip,
                    )
                logger.info("[RunControlService] workflow started: task_id=%s", task_id)

                # 2e. 起流（decoder 键 = task_id，与注册表一致；系统只用 RTSP）
                stream_service.start_stream(task_id=task_id, stream_url=rtsp_url)
                logger.info("[RunControlService] stream started: task_id=%s", task_id)
            except Exception:
                # 对称回滚（stop_run 尽力而为、永不抛）；expected=sess 身份 fence 防误清。
                logger.warning(
                    "[RunControlService] start_run failed for task=%s; rolling back", task_id
                )
                self.stop_run(task_id, reason=f"start_rollback:{task_id}", expected=sess)
                raise

            return {
                "status": "success",
                "task_id": task_id,
                "rtsp_url": rtsp_url,
                "message": f"Task {task_id} started",
            }

    def stop_run(
        self,
        task_id: int,
        reason: str,
        *,
        skip_decoder: bool = False,
        expected: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """拆除一次 run：封闸 → 停 decoder → 结算 / 清前端槽 / 交出残帧 → 关 session。

        全程持 `lock_for(task_id)`。尽力而为：每步独立 try，单步失败不中断后续；永不抛出。
        `skip_decoder=True` 用于孤儿流（decoder 已不存在）。

        `expected`（HealthMonitor 自动结束路径与 start 回滚传）：对象身份 fence。注册项已非
        `expected` 时整段放弃（不停新 run 的 decoder、不清其数据）。api 控制面持锁内决策+执行、无 ABA，不传。
        """
        with self.lock_for(task_id):
            result: Dict[str, Any] = {
                "client_id": None,   # 诊断字段（保键名兼容），语义=source_ip，取到 session 后回填
                "reason": reason,
                "decoder_stopped": False,
                "data_flushed": False,
                "client_cleaned": False,
                "errors": [],
            }

            sess = session_service.get(task_id)
            if sess is not None:
                result["client_id"] = sess.info.source_ip

            # 0. 对象身份 fence
            if expected is not None and sess is not expected:
                logger.info(
                    "[RunControlService] stop_run(reason=%r) skipped by identity fence: task=%s "
                    "(slot replaced by newer run)", reason, task_id,
                )
                result["skipped"] = True
                return result

            # 0b. 封闸：ACTIVE→DRAINING，拒一切写（decoder 帧 / 结果写回 / tick）；读与取走照常。
            if sess is not None:
                session_service.seal(sess)

            # 1. 停 decoder（owner=StreamService）
            if not skip_decoder:
                try:
                    stream_service.stop_stream(task_id)
                    result["decoder_stopped"] = True
                except Exception as e:
                    result["errors"].append(f"decoder: {e}")
                    logger.error(
                        "[RunControlService] stop decoder failed: task=%s - %s", task_id, e, exc_info=True
                    )

            # 2. 交出残余（按 owner 归位）：① inference 停 workflow 交出 settlement → alarm 上报；
            #    ② 清前端槽；③ recording 落 HLS 残段与剩余检测结果。③ 必须早于第 3 步 close
            #    （close 释放帧）。三步各自 try：结算或清槽抛错不能连带跳过 ③。
            if sess is not None:
                try:
                    settlement = inference_service.stop_workflow(sess)  # Inference owner
                    if settlement:
                        alarm_sink.persist_alarms(
                            settlement, sess=sess, mode=ALARM_MODE_SETTLEMENT, log_each=True
                        )
                except Exception as e:
                    result["errors"].append(f"settlement: {e}")
                    logger.error(
                        "[RunControlService] settlement failed: %s - %s", task_id, e, exc_info=True
                    )
                try:
                    sess.channel(LATEST_TEMPORAL).clear()   # 提前清前端槽，防 WS 读到结束后残留
                    sess.channel(RENDERED).clear()
                except Exception as e:
                    result["errors"].append(f"frontend: {e}")
                    logger.error(
                        "[RunControlService] clear frontend slots failed: %s - %s",
                        task_id, e, exc_info=True,
                    )
            try:
                if sess is not None:
                    recording_service.flush_residual(sess)                  # Recording owner
                result["data_flushed"] = True
            except Exception as e:
                result["errors"].append(f"flush: {e}")
                logger.error(
                    "[RunControlService] flush data failed: %s - %s", task_id, e, exc_info=True
                )

            # 3. 摘出注册表并释放全部数据（身份核对：注册项仍是 sess 才摘）
            if sess is not None:
                try:
                    result["client_cleaned"] = session_service.close(sess)
                except Exception as e:
                    result["errors"].append(f"session: {e}")
                    logger.error(
                        "[RunControlService] close session failed: %s - %s", task_id, e, exc_info=True
                    )

            if result["errors"]:
                logger.warning(
                    "[RunControlService] stop_run(reason=%r) completed with errors: %s - %s",
                    reason, task_id, result["errors"],
                )
            else:
                logger.info(
                    "[RunControlService] stop_run(reason=%r) completed: %s", reason, task_id
                )
            return result

    def restart_stream(self, task_id: int, stream_url: str, *, expected: Session) -> bool:
        """重连 respawn：注册项仍是 `expected` 才重启 decoder。返回是否发起了重启且成功。

        HealthMonitor 决策时捕获 `expected`，respawn 前 `/start` 可能已换代；锁内核对不过即放弃，
        不停新 run 的 decoder、不把旧 URL 的 decoder 绑到新 run 上。持锁期间注册项不变，
        stream 侧按 task_id 取到的 session 即 `expected`。
        """
        with self.lock_for(task_id):
            if session_service.get(task_id) is not expected:
                logger.info(
                    "[RunControlService] restart_stream skipped by identity fence: task=%s", task_id
                )
                return False
            return stream_service.restart_stream(task_id=task_id, stream_url=stream_url)

    def stop_orphan_decoder(self, task_id: int) -> bool:
        """停孤儿 decoder（有 decoder 无 run）。返回是否真的停了。

        HealthMonitor 在锁外判定「孤儿」，拿锁前 `/start` 可能已为该 task 注册新 run 与新 decoder；
        故锁内复查注册表，有 run 即放弃，不碰新 run 的 decoder。
        """
        with self.lock_for(task_id):
            if session_service.has(task_id):
                logger.info(
                    "[RunControlService] stop_orphan_decoder skipped: task=%s now has a run", task_id
                )
                return False
            stream_service.stop_stream(task_id)
            return True

    def shutdown(self) -> None:
        """进程停机：逐个 stop_run 在跑的 run，交出 settlement / HLS 残段 / 剩余检测结果。

        须在 `inference.stop()` 之前、recording / alarm 队列仍活着时调（`run_control.lifespan`
        嵌在 `inference.lifespan` 里层保证）。
        """
        for task_id in list(session_service.snapshot()):
            self.stop_run(task_id, reason="shutdown")
