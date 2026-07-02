"""推理管理器 - 核心实现

架构特点：
1. 推理与可视化解耦：推理线程只负责推理，可视化独立定时拉取
2. 时序分析独立：ClientTemporalActor 持有 Operator 流算子（per-client），1Hz tick
3. 三池独立时钟：推理、时序分析、可视化各自独立节奏，不通过队列串联
4. 双写 + 原子快照：推理结果同时写入 slide_window（历史）和 latest_inference（最新快照）

数据流：
InferenceLoop → cq.push_detection() + cq.set_latest_inference()  [双写]
TemporalActor (1Hz)  → cq.get_slide_window() → operator.analyze() → operator.judge() → cq.set_latest_temporal()
VisualizationWorker (~15Hz) → cq.get_latest_inference() + get_latest_frame() + get_latest_temporal() → render → cq
"""

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

logger = logging.getLogger(__name__)

from app.domain.alarm import Alarm
from app.domain.frame import Frame
from app.domain.task import CleaningTask
from app.services.client import ClientQueues, client_manager
from app.services.inference.detection.service import ModelWorkerService
from app.services.inference.temporal.actor import ClientTemporalActor
from app.services.inference.visualization.pool import VisualizationWorkerPool


class InferenceManager:
    """推理管理器

    集成三个独立时钟的 Worker 池：
    - ModelWorkerService（推理，~30 FPS）
    - ClientTemporalActor（时序分析，1 Hz，per-client）
    - VisualizationWorkerPool（可视化，~15 FPS）

    三池通过 ClientQueues 上的原子槽位通信，不通过队列串联。
    """

    def __init__(
        self,
        rt_fps: int = 30,
        ca_segment_seconds: int = 10,
        db_dir: Optional[str] = None,
    ):
        # 队列参数
        self._ca_segment_len = max(10, int(rt_fps * ca_segment_seconds))

        # 持久化存储根目录：默认读 settings 单一真源（与 persistence/traceback 同源），
        # 仅显式传 db_dir 时覆盖（测试/特殊场景）。不再 __file__ 自数层级重算。
        from app.settings import settings
        self._db_dir = Path(db_dir) if db_dir else settings.storage_base_dir
        self._db_dir.mkdir(parents=True, exist_ok=True)

        self._stop_event = threading.Event()

        # stage 配置（延迟初始化）
        self._stage_configs: Optional[Dict[str, Dict[str, Any]]] = None
        self._model_worker_service: Optional[ModelWorkerService] = None

        # 客户端生命周期事务锁（set_task / remove_client 互斥）。
        # 这是控制面锁，只串行稀疏的启/切/停转换（每会话几次、毫秒级），不在帧推理热路径上；
        # 故用单把 manager 级锁，而非 per-client 锁——后者会引入锁字典的内存泄漏与回收 race，
        # 且为几乎不存在的"跨 client 同刻启停"并行度买单。详见 docs/update/20260626_THREAD_INSTANCE_LIFECYCLE_AUDIT.md。
        self._client_lifecycle_lock = threading.Lock()

        # per-client ClientTemporalActor 注册表
        self._actors: Dict[str, ClientTemporalActor] = {}

        # 可视化拉取率 = settings.inference_fps（单一真源）：与推理限流、HLS processed
        # 打标三处对齐，避免 processed 段实际产出率 ≠ 打标率导致回放偏快。
        self.visualization_pool = VisualizationWorkerPool(
            target_fps=settings.inference_fps,
            stage_configs=None,
        )

        # L2 特征落盘（常开，与 HLS 同款 {task_id}/{step_id}/ 工作目录）。
        # FeatureStore 注入推理服务，由推理写回处按帧追加，生命周期随在线 run（open_fresh/close/flush）。
        # 注：FactLedger（事实账本）是**离线异步写**的 store，生命周期归离线 runner，不由在线 manager
        # 调度——故此处不持有、不 open_fresh/close/flush。待离线流水线建起时由其自行 new + 驱动
        # （同一 storage_base_dir）。类/契约见 feature/store.py，休眠预留。
        from app.services.inference.feature.store import FeatureStore
        self.feature_store = FeatureStore(self._db_dir)

        self._model_worker_service = self._create_async_model_worker_service()

        # persistence 自己从 settings.storage_base_dir 读存储根（与此处 _db_dir 同源），
        # 不再反向 push db_dir 进其私有 hls_pool.strategy —— 消除跨服务穿透。
        from app.services.persistence import persistence_manager as _persistence_manager
        self.persistence_manager = _persistence_manager

        logger.debug("[InferenceManager] Initialization completed")

    def _get_stage_configs(self) -> Dict[str, Dict[str, Any]]:
        """延迟初始化 stage 配置。

        返回结构：
        {
            "1": {
                "models": [BubbleDetector, BendingDetector],   # List[Detector]（流源），共享
                "operator_specs": [(BubbleOperator, {...}), ...],  # 流算子，按 Client 实例化
                "batch_size": 4,
            }
        }
        """
        if self._stage_configs is None:
            try:
                from app.services.inference.stage_factory import StageFactory
                from app.services.inference.config import load_stage_config

                config = load_stage_config()
                factory = StageFactory(config)

                stage_configs = {}
                skipped_stages = []
                for stage_name in config.list_stages():
                    detectors = factory.create_detectors_for_stage(stage_name)
                    operator_specs = factory.create_operators_for_stage(stage_name)

                    if detectors:
                        stage_configs[stage_name] = {
                            "models": detectors,
                            "operator_specs": operator_specs,
                            "batch_size": config.batch_size,
                        }
                    else:
                        skipped_stages.append(stage_name)

                if stage_configs:
                    logger.info(
                        "[InferenceManager] Loaded %d stages (active): %s",
                        len(stage_configs), list(stage_configs.keys())
                    )
                    if skipped_stages:
                        logger.info(
                            "[InferenceManager] Skipped %d stages (no detectors): %s",
                            len(skipped_stages), skipped_stages
                        )
                    self._stage_configs = stage_configs
                else:
                    raise ValueError(
                        "No valid stages found in configuration file. "
                        "Please ensure inference_config.yaml contains at least one stage with valid models."
                    )
            except Exception as e:
                logger.error("[InferenceManager] Failed to load config: %s", e, exc_info=True)
                raise RuntimeError(
                    f"Failed to load inference configuration: {e}. "
                    "Please check inference_config.yaml and ensure it is properly configured."
                ) from e

        return self._stage_configs

    def _create_async_model_worker_service(self):
        from app.services.inference.detection.service import ModelWorkerService

        return ModelWorkerService(
            stage_configs=self._get_stage_configs(),
            max_batch_per_stage=8,
            use_cuda_stream=True,
            feature_store=self.feature_store,
        )

    # ========== 公共 API ==========

    def get_result(self, client_id: str) -> Optional[Frame]:
        """返回最新处理帧（domain Frame）。

        编码为 WS 载荷（JPEG base64）是边界职责，在 routers/ai.py 完成；
        core 服务只交付 domain 对象。
        """
        if not client_manager.has_client(client_id):
            return None
        cq = client_manager.get(client_id)
        if not cq:
            return None
        return cq.get_latest_result()

    def _resolve_stage(self, current_step: Any) -> str:
        """step_id 主键直接作 stage（恒等路由，无映射表）；未知/未配回退 MOCK 透传。"""
        step_key = str(current_step)
        if step_key in self._get_stage_configs():
            return step_key
        logger.warning(
            "[InferenceManager] 未知的 current_step '%s'，路由到 MOCK stage", current_step
        )
        return "MOCK"

    def set_task(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """建立一次 run：构造不可变身份的**新** CQ + ClientTemporalActor，整体换槽。

        一 CQ == 一 run：不在旧 CQ 上原地改身份，而是建新 CQ 换 registry 槽位。
        旧 run 的 actor 先 finalize（settlement 落到旧 CQ，其不可变身份即旧 run，归属天然正确），
        故不再需要"先停旧 actor 再切字段"的排序不变式。
        全程在 _client_lifecycle_lock 下，与 remove_client 互斥。
        task=None：仅停旧 actor、不建新 run（历史语义，生产链路不再走）。
        """
        with self._client_lifecycle_lock:
            return self._set_task_locked(client_id, task)

    def _set_task_locked(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """set_task 的加锁实现，调用方须已持有 _client_lifecycle_lock。"""
        # 1. 停旧 actor：settlement 落到旧 CQ（其不可变身份即旧 run，归属正确）。
        #    重启路径下 RunController 已先 stop_run 拆掉旧 run，此处多为 no-op（防御保留）。
        old_cq = client_manager.get(client_id)
        old_actor = self._actors.pop(client_id, None)
        if old_actor is not None:
            try:
                settlement = old_actor.finalize_and_stop()
                if settlement and old_cq is not None:
                    self._persist_settlement_alarms(client_id, old_cq, settlement)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms on task switch failed for %s: %s",
                    client_id, e,
                )

        if task is None:
            return True  # 历史语义：清任务 = 无新 run，不建 CQ/actor

        # 2. 解析 stage（step_id 主键，未配回退 MOCK）
        stage = self._resolve_stage(task.current_step)

        # 3. 建**新**不可变 CQ，整体换槽（配置走单一出口，早于起流，消除 dead-kwargs）
        from app.services.client.config import get_client_config
        new_cq = ClientQueues(
            client_id, task=task, stage=stage, **get_client_config().cq_kwargs()
        )
        client_manager.set(client_id, new_cq)

        # 4. 新 run 起始截断存储分区（重启 supersede，避免同 (task,step) 新旧混写）
        if new_cq.step_id is not None:
            try:
                self.feature_store.open_fresh(task.task_id, new_cq.step_id, owner=new_cq)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] open_fresh storage failed for %s: %s", client_id, e
                )

        # 5. 按 stage 实例化流算子 Operator + actor（绑定新 CQ）
        stage_cfg = self._get_stage_configs().get(stage, {})
        specs = stage_cfg.get("operator_specs", [])

        if specs:
            operators = [cls(**kwargs) for cls, kwargs in specs]
            # 按感受野配置每条流的缓冲长度：max(底线, 订阅该流的算子最大 window_seconds)。
            # 算子在 analyze 内自行 _clip 到各自感受野；底线保证 signals_10s 仍见 10s。
            stream_windows: Dict[str, float] = {}
            for op in operators:
                for src in op.subscribes:
                    stream_windows[src] = max(
                        stream_windows.get(src, 0.0), op.window_seconds
                    )
            new_cq.set_stream_windows(stream_windows)

            actor = ClientTemporalActor(
                client_id=client_id,
                cq=new_cq,
                stage=stage,
                operators=operators,
            )
            actor.start()
            self._actors[client_id] = actor
            logger.info(
                "[InferenceManager] TemporalActor created for %s (stage=%s, operators=%d)",
                client_id, stage, len(operators),
            )
        else:
            logger.debug(
                "[InferenceManager] No operator specs for stage %s, skipping TemporalActor",
                stage,
            )

        return True

    def get_task(self, client_id: str) -> Optional[CleaningTask]:
        cq = client_manager.get(client_id)
        return cq.get_task() if cq else None

    def remove_client(self, client_id: str) -> None:
        """移除客户端的推理资源。

        1. 通过 actor.finalize_and_stop() 收集结算告警并持久化
        2. 落盘残余 HLS 段
        3. 清理编码缓存

        整个流程在 _client_lifecycle_lock 下执行，与 set_task 互斥。
        """
        with self._client_lifecycle_lock:
            self._remove_client_locked(client_id)

    def _remove_client_locked(self, client_id: str) -> None:
        """remove_client 的加锁实现，调用方须已持有 _client_lifecycle_lock。"""
        logger.info("[InferenceManager] Removing inference resources: %s", client_id)

        cq = (
            client_manager.get(client_id)
            if client_manager.has_client(client_id)
            else None
        )

        # Actor 持有正确的 _sm，finalize_and_stop() 调用 analyzer.finalize()
        actor = self._actors.pop(client_id, None)
        if actor is not None:
            try:
                settlement_alarms = actor.finalize_and_stop()
                if settlement_alarms and cq:
                    self._persist_settlement_alarms(client_id, cq, settlement_alarms)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms failed for %s: %s", client_id, e
                )

        # Actor 停止后立即清空前端可见槽位，防止 WebSocket 读到任务结束后的残留状态。
        # cq.clear() 会在 Step3（client_manager.remove_client）再次执行，此处只做提前清零。
        if cq:
            cq.set_latest_temporal([])
            cq.set_latest_rendered(None)

        if cq:
            try:
                self._flush_all_remaining_segments(client_id, cq)
                logger.info("[InferenceManager] Segments WroteBack: %s", client_id)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Failed to write back segments: %s - %s", client_id, e
                )
            # FeatureStore flush/close（best-effort；此时 cq 仍在，(task_id, step_id) 可解析）
            try:
                task_id = cq.get_task_id()
                step_id = cq.get_step_id()
                if task_id is not None and step_id is not None:
                    self.feature_store.close(task_id, step_id, owner=cq)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Failed to close feature store: %s - %s", client_id, e
                )
        else:
            logger.warning(
                "[InferenceManager] Client not found in ClientManager, skipping writeback: %s",
                client_id,
            )

        logger.info("[InferenceManager] Inference resources removed: %s", client_id)

    def status(self) -> Dict[str, Any]:
        clients = client_manager.snapshot()
        stats = {client_id: cq.to_status_dict() for client_id, cq in clients.items()}
        return {"clients": len(clients), "queues": stats}

    # ========== 内部辅助方法 ==========

    def _persist_settlement_alarms(
        self, client_id: str, cq: ClientQueues, alarms: List[Alarm]
    ) -> None:
        """持久化结算告警（由 actor.finalize_and_stop() 收集后调用）。"""
        from app.services.inference.naming import get_stage_alias
        from app.services.inference.temporal.alarm_sink import persist_alarms

        # 可读性出口：告警 step_name/stage 用别名（主键是 step_id，对人不可读）
        persist_alarms(
            alarms,
            cq=cq,
            client_id=client_id,
            stage_name=get_stage_alias(cq.get_stage()),
            mode="SETTLEMENT",
            persistence_manager=self.persistence_manager,
            log_each=True,
        )

    def _flush_all_remaining_segments(
        self, client_id: str, client_queues: ClientQueues
    ) -> None:
        try:
            task_id = client_queues.get_task_id()
            if task_id is None:
                logger.warning(
                    "[InferenceManager] task_id is None for %s, skipping flush", client_id
                )
                return

            step_id = client_queues.get_step_id()
            if step_id is None:
                logger.error(
                    "[InferenceManager] invalid current_step for client=%s task_id=%s, skip flush",
                    client_id, task_id,
                )
                return

            seg_len = client_queues.ca_segment_len
            raw_frames = client_queues.drain_ca_raw()
            processed_frames = client_queues.drain_ca_processed()

            for i in range(0, len(raw_frames), seg_len):
                chunk = raw_frames[i : i + seg_len]
                if chunk:
                    self.persistence_manager.persist_hls_segment(
                        task_id=task_id,
                        step_id=step_id,
                        segment_type="raw",
                        frames=chunk,
                    )

            for i in range(0, len(processed_frames), seg_len):
                chunk = processed_frames[i : i + seg_len]
                if chunk:
                    self.persistence_manager.persist_hls_segment(
                        task_id=task_id,
                        step_id=step_id,
                        segment_type="processed",
                        frames=chunk,
                    )

        except Exception as e:
            logger.error("_flush_all_remaining_segments error for %s: %s", client_id, e, exc_info=True)

    # ========== 启动/停止 ==========

    def start(self):
        logger.info("[InferenceManager] 启动中...")

        if self._model_worker_service:
            self._model_worker_service.start()

        if self._model_worker_service:
            self.visualization_pool.stage_configs = self._get_stage_configs()

        self.visualization_pool.start()
        self.persistence_manager.start()

        # 初始化全局映射（均由 YAML 驱动）：
        #   task_name → AlarmMetric（实时信号指标）
        #   stage 主键(step_id) → alias（写告警 step_name + 可视化叠字）
        from app.services.inference.stage_factory import StageFactory
        from app.services.inference.config import load_stage_config
        from app.services.inference.naming import _set_task_metric_map, _set_stage_alias_map
        _factory = StageFactory(load_stage_config())
        _set_task_metric_map(_factory.build_task_metric_map())
        _set_stage_alias_map(_factory.build_stage_alias_map())

        logger.info("[InferenceManager] Started")

    def stop(self):
        self._stop_event.set()

        if self._model_worker_service:
            self._model_worker_service.stop()

        # 停止所有 actor，等待线程退出后再停止下游服务
        actors = list(self._actors.items())   # [(client_id, actor), ...]
        self._actors.clear()

        # Phase 1: 并行发出停止信号（非阻塞）
        for _, actor in actors:
            actor.signal_stop()

        # Phase 2: 逐个 join，收集并持久化结算告警
        for client_id, actor in actors:
            try:
                settlement = actor.finalize_and_stop()
                if settlement:
                    cq = client_manager.get(client_id)
                    if cq:
                        self._persist_settlement_alarms(client_id, cq, settlement)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms on stop failed for %s: %s",
                    client_id, e,
                )

        # FeatureStore 全量 flush：停机时仍有活跃客户端时，逐客户端 close 不会触发，
        # 残余缓冲（每 (task,step) 最多 batch_size-1 行）需在此 best-effort 落盘，
        # 否则 offline 链路读到的特征尾部会被静默截断。
        try:
            self.feature_store.flush()
        except Exception as e:
            logger.warning("[InferenceManager] flush feature store on stop failed: %s", e)

        self.visualization_pool.stop()
        self.persistence_manager.stop(timeout=10.0)

        logger.info("[InferenceManager] Stopped")
