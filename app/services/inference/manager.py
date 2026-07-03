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

from app.domain.alarm import ALARM_MODE_SETTLEMENT, Alarm
from app.domain.frame import Frame
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

        # per-client ClientTemporalActor 注册表。
        # 注：start/stop_workflow 的互斥由 RunController 的 lock_for(client_id) per-client 锁承接
        # （T3 已落地），本类不再自持 _client_lifecycle_lock。
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

        # 注：InferenceManager 不再持 persistence_manager 引用（不驱动其生命周期、不做拆除期持久化）。
        # 告警落库/HLS flush 归 PersistenceManager，由 RunController 编排；进程停机残余结算走惰性 import。
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

    def resolve_stage(self, current_step: Any) -> str:
        """step_id 主键直接作 stage（恒等路由，无映射表）；未知/未配回退 MOCK 透传。

        公有：供 RunController 在建 CQ 前解析 stage（stage 是 CQ 不可变身份的一部分）。
        """
        step_key = str(current_step)
        if step_key in self._get_stage_configs():
            return step_key
        logger.warning(
            "[InferenceManager] 未知的 current_step '%s'，路由到 MOCK stage", current_step
        )
        return "MOCK"

    def start_workflow(self, cq: ClientQueues) -> bool:
        """起该 run 的推理 workflow：换槽注册 CQ、open_fresh 特征分区、建并启 actor。

        入参是 RunController 已建好的不可变身份 CQ（一 CQ == 一 run）。调用方已持
        lock_for(cq.run_key)，与 stop_workflow 互斥；重启路径下 RunController 先 stop_workflow
        拆旧，故此处 _actors 槽已空。stage 由 cq 派生（构造时经 resolve_stage 定死）。
        """
        run_key = cq.run_key
        # 防御：残留旧 actor（正常路径 stop_workflow 已 pop，不应命中）——信号停、丢弃、不结算。
        stale = self._actors.pop(run_key, None)
        if stale is not None:
            logger.warning(
                "[InferenceManager] stale actor for %s at start_workflow; dropping", run_key
            )
            stale.signal_stop()

        # 1. 换槽注册（COW 发布新 CQ）
        client_manager.set(run_key, cq)

        # 2. 新 run 起始截断存储分区（重启 supersede，避免同 (task,step) 新旧混写）
        if cq.step_id is not None:
            try:
                self.feature_store.open_fresh(cq.task_id, cq.step_id, owner=cq)
            except Exception as e:
                logger.warning(
                    "[InferenceManager] open_fresh storage failed for %s: %s", run_key, e
                )

        # 3. 按 stage 实例化流算子 Operator + actor（绑定该 CQ）
        stage = cq.get_stage()
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
            cq.set_stream_windows(stream_windows)

            actor = ClientTemporalActor(
                run_key=run_key,
                cq=cq,
                stage=stage,
                operators=operators,
            )
            actor.start()
            self._actors[run_key] = actor
            logger.info(
                "[InferenceManager] TemporalActor created for %s (stage=%s, operators=%d)",
                run_key, stage, len(operators),
            )
        else:
            logger.debug(
                "[InferenceManager] No operator specs for stage %s, skipping TemporalActor",
                stage,
            )

        return True

    def stop_workflow(self, cq: ClientQueues) -> List[Alarm]:
        """停该 run 的推理 workflow：停 actor（收结算）+ 关 feature 分区，返回 settlement 列表。

        单一 per-run 拆除口——一把停掉本 run 的全部 inference 自有组件，**不持久化**（settlement
        交给 RunController 转 PersistenceManager；HLS flush / 告警落库均归 persistence owner，
        前端槽清零亦由 RunController 做）。调用方（RunController.stop_run）已持 lock_for(cq.run_key)，
        与 start_workflow 互斥。无 actor 返 []；feature close best-effort；别名已由 actor 烧进 alarm.stage。
        """
        run_key = cq.run_key
        logger.info("[InferenceManager] Stopping workflow: %s", run_key)

        settlement: List[Alarm] = []
        actor = self._actors.pop(run_key, None)
        if actor is not None:
            try:
                settlement = actor.finalize_and_stop()
            except Exception as e:
                logger.warning(
                    "[InferenceManager] finalize actor failed for %s: %s", run_key, e
                )

        # 关闭本 run 的 FeatureStore 分区（inference 自有组件；best-effort，此时 cq 仍在）
        try:
            task_id = cq.get_task_id()
            step_id = cq.get_step_id()
            if task_id is not None and step_id is not None:
                self.feature_store.close(task_id, step_id, owner=cq)
        except Exception as e:
            logger.warning(
                "[InferenceManager] Failed to close feature store: %s - %s", run_key, e
            )

        logger.info("[InferenceManager] Workflow stopped: %s", run_key)
        return settlement

    def status(self) -> Dict[str, Any]:
        clients = client_manager.snapshot()
        stats = {client_id: cq.to_status_dict() for client_id, cq in clients.items()}
        return {"clients": len(clients), "queues": stats}

    # ========== 启动/停止 ==========

    def start(self):
        logger.info("[InferenceManager] 启动中...")

        if self._model_worker_service:
            self._model_worker_service.start()

        if self._model_worker_service:
            self.visualization_pool.stage_configs = self._get_stage_configs()

        self.visualization_pool.start()
        # 注：persistence 生命周期已上移 lifespan（persistence.lifespan 嵌套于 ai.lifespan 外层），
        # 不再由本类驱动 start/stop——inference 不拥有平级服务的生命周期。

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

        # Phase 2: 逐个 join，收集结算告警并经 persistence sink 落库。
        # 进程停机路径（非 per-run 拆除）：actor 产出的 settlement 用 persistence 落库（别名已烧进
        # alarm.stage）。惰性 import 持久化单例（与 actor 实时路径同款 sink 调用）——此时 persistence
        # 仍在跑（persistence.lifespan 于 ai.lifespan 外层，停在 inference 之后）。
        from app.services.persistence import persistence_manager
        for client_id, actor in actors:
            try:
                settlement = actor.finalize_and_stop()
                if settlement:
                    cq = client_manager.get(client_id)
                    if cq:
                        persistence_manager.persist_alarms(
                            settlement,
                            cq=cq,
                            client_id=client_id,
                            mode=ALARM_MODE_SETTLEMENT,
                        )
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
        # 注：persistence.stop() 已上移 persistence.lifespan（停在 inference 之后，抽干队列）。

        logger.info("[InferenceManager] Stopped")
