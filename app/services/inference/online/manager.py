"""推理管理器 - 核心实现

架构特点：
1. 推理与可视化解耦：推理线程只负责推理，可视化独立定时拉取
2. 时序分析独立：ClientTemporalActor 持有 Operator 流算子（per-client），2Hz tick
3. 三池独立时钟：推理、时序分析、可视化各自独立节奏，不通过队列串联
4. 双写 + 原子快照：推理结果同时写入 slide_window（历史）和 latest_detection（最新快照）

数据流：
InferenceLoop → cq.push_detection() + cq.set_latest_detection()  [双写]
TemporalActor (2Hz)  → cq.get_slide_window() → operator.analyze() → operator.judge() → cq.set_latest_temporal()
VisualizationWorker (~15Hz) → cq.get_latest_detection() + get_latest_frame() + get_latest_temporal() → render → cq
"""

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.domain.alarm import ALARM_MODE_SETTLEMENT, Alarm
from app.services.client import ClientQueues, client_manager
from app.services.inference.config import FALLBACK_STAGE
from app.utils.exceptions import ValidationError
from .temporal import alarm_sink
from .temporal.actor import ClientTemporalActor

# 两个只在 `_build_components()` 里实例化的重组件走 TYPE_CHECKING + 函数体内导入
# （规范 §2 通路 2）：`visualization.pool` → worker → visualizer 顶层 `import cv2`，
# 写在模块级会让 `import app.main`（经 run_control → instance）一律拉起 OpenCV。
if TYPE_CHECKING:
    from .detection.service import DetectionService
    from .visualization.pool import VisualizationWorkerPool

logger = logging.getLogger(__name__)


class InferenceManager:
    """推理管理器

    集成三个独立时钟的 Worker 池：
    - DetectionService（推理，~30 FPS）
    - ClientTemporalActor（时序分析，2 Hz，per-client）
    - VisualizationWorkerPool（可视化，轮询 raw_fps~30 Hz 过采样，出帧随 inference_fps~15 FPS 去重）

    三池通过 ClientQueues 上的原子槽位通信，不通过队列串联。
    """

    def __init__(self):
        """只做赋值与建空容器，**不产生任何副作用**（不读 settings、不加载 stage 配置、
        不建 worker 池）。重活全在 `start()`（见 `_build_components`）。

        原因：本类的全局单例在 `instance.py` 里是模块级构造的，构造期加载 stage 配置会经
        `stage_factory` 的 importlib 把全部 impl 与 torch 在 **import 期**拉起——凡 import
        到本包的人（含只想跑一个纯函数单测的）都得付这笔钱。
        """
        self._stop_event = threading.Event()

        # stage 配置（延迟初始化）
        self._stage_configs: Optional[Dict[str, Dict[str, Any]]] = None
        self._model_worker_service: Optional["DetectionService"] = None

        # per-client ClientTemporalActor 注册表。
        # 注：start/stop_workflow 的互斥由 RunController 的 lock_for(task_id) per-task 锁承接
        # （T3 已落地），本类不再自持 _client_lifecycle_lock。
        self._actors: Dict[int, ClientTemporalActor] = {}

        # 活体组件在 start() 里建（None = 尚未 start）
        self.visualization_pool: Optional["VisualizationWorkerPool"] = None

        # 注：InferenceManager 不再持 persistence_manager 引用（不驱动其生命周期、不做拆除期持久化）。
        # 告警落库/HLS flush 归 PersistenceManager，由 RunController 编排；进程停机残余结算走惰性 import。
        logger.debug("[InferenceManager] Initialization completed")

    def _build_components(self):
        """建重组件：可视化池、DetectionService。

        由 `start()` 调用，幂等（重复调用不重建）。放这儿而不是 `__init__` 的理由见后者 docstring。
        """
        if self._model_worker_service is not None:
            return

        from app.settings import settings

        # 可视化 worker 是"采样后 inference 流"的消费者：渲染按 inference.ts 去重，故每秒吐出的
        # 不同画面数恒 = 检测采样率（inference_fps）。但轮询率取 raw_fps（源视频帧率，2× 过采样）：
        # poll 率 == inference_fps 时两个同频时钟拍频，部分 tick 读到旧快照 → 恒报 supply-bound、
        # 抓帧有 33~66ms 抖动；抬到 raw_fps 后每帧新推理都能在一个 tick 内被抓到（空转 tick 仅读单槽+
        # 比 ts，~µs 级，不增推理量）。raw_fps 是已有的跨模块真源，无需新旋钮。
        # 注：HLS processed 打标另由 eff_fps 从 ts 反推、模型输入另由 model_input_fps 契约重采样，均不借本值。
        from .visualization.pool import VisualizationWorkerPool

        self.visualization_pool = VisualizationWorkerPool(
            target_fps=settings.raw_fps,          # 轮询率：源视频帧率，对 inference 流 2× 过采样
            output_fps=settings.inference_fps,    # 期望出帧率：吞吐告警判速率亏空的基准（与轮询率解耦）
            stage_configs=None,
        )

        # 注：L1 检测结果落盘不在本服务——写回口把 FrameDetection 放进 cq 的落盘缓冲，由
        # recording 的 sweeper 拉走写 `{task}/{step}/inference/detections.jsonl`。本 manager
        # 因此不持有任何 store、不管 supersede（recording 首写自清）、不管 flush。
        self._model_worker_service = self._create_async_model_worker_service()

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
                    # 启动不变式：兜底 stage 必须 active（有 detector）。
                    # resolve_stage 把 detector 加载失败的 stage 路由到它，而 dispatcher 只提交
                    # active stage 的帧——若它被配掉 detector，启动**仍会成功**（上面只 INFO 一行
                    # Skipped），但此后兜底的 run 都会被取帧后无人消费，静默 0 推理。
                    # 现网靠「MOCK 恰好配了 detector」这个巧合幸免，此处把巧合提成显式契约。
                    if FALLBACK_STAGE not in stage_configs:
                        raise ValueError(
                            f"兜底 stage '{FALLBACK_STAGE}' 无 detector（未 active）——"
                            f"兜底的 run 会被静默黑洞：取帧后无 stage 消费、0 推理且无告警。"
                            f"请在 inference_config.yaml 为 '{FALLBACK_STAGE}' 配至少一个 detector。"
                            f"（active={list(stage_configs.keys())}, skipped={skipped_stages}）"
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
        from .detection.service import DetectionService

        return DetectionService(
            stage_configs=self._get_stage_configs(),
            max_batch_per_stage=8,
        )

    # ========== 公共 API ==========

    def resolve_stage(self, step_id: Any) -> str:
        """step_id 主键直接作 stage（恒等路由，无映射表）。

        - YAML 未配该 step → `ValidationError`（参数错误，上游不该下发）；
        - 配了但 detector 全部加载失败（stage 未 active）→ 回退 MOCK 透传（推理失败兜底，不黑屏）。

        公有：供 RunController 在建 CQ 前解析 stage（stage 是 CQ 不可变身份的一部分）。
        """
        from app.services.inference.config import load_stage_config

        step_key = str(step_id)
        if step_key in self._get_stage_configs():
            return step_key
        if step_key not in load_stage_config().list_stages():
            raise ValidationError(
                f"step_id '{step_id}' 未在推理配置中定义", field="current_step", value=step_key,
            )
        logger.warning(
            "[InferenceManager] stage '%s' 无可用 detector（加载失败），路由到 %s stage",
            step_key, FALLBACK_STAGE,
        )
        return FALLBACK_STAGE

    def start_workflow(self, cq: ClientQueues) -> bool:
        """起该 run 的推理 workflow：建并启 actor（存储侧无起始钩子）。

        入参是 RunController 已建好并**已注册**（client_manager.set）的不可变身份 CQ
        （一 CQ == 一 run）。调用方已持 lock_for(cq.task_id)，与 stop_workflow 互斥；重启路径下
        RunController 先 stop_workflow 拆旧，故此处 _actors 槽已空。CQ 的 set/remove 均归
        RunController（与 stop_run 对称），本方法不再碰注册表。stage 由 cq 派生（构造时经
        resolve_stage 定死）。
        """
        task_id = cq.task_id
        # 防御：残留旧 actor（正常路径 stop_workflow 已 pop，不应命中）——信号停、丢弃、不结算。
        stale = self._actors.pop(task_id, None)
        if stale is not None:
            logger.warning(
                "[InferenceManager] stale actor for task=%s at start_workflow; dropping", task_id
            )
            stale.signal_stop()

        # 注：起始**不再截断存储分区**。同 (task,step) 重启的 supersede 归 recording 的
        # 懒惰首写自清（本代次第一批检测结果真正落盘时才 `inference.delete`），与 HLS 同款——
        # 新 run 若一帧检测结果都没写出来，上一代的产物原样保留、离线还能跑。

        # 按 stage 实例化流算子 Operator + actor（绑定该 CQ）
        stage = cq.stage
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
                task_id=task_id,
                cq=cq,
                stage=stage,
                operators=operators,
            )
            actor.start()
            self._actors[task_id] = actor
            logger.info(
                "[InferenceManager] TemporalActor created for task=%s (stage=%s, operators=%d)",
                task_id, stage, len(operators),
            )
        else:
            logger.debug(
                "[InferenceManager] No operator specs for stage %s, skipping TemporalActor",
                stage,
            )

        return True

    def stop_workflow(self, cq: ClientQueues) -> List[Alarm]:
        """停该 run 的推理 workflow：停 actor（收结算），返回 settlement 列表。

        单一 per-run 拆除口——一把停掉本 run 的全部 inference 自有组件，**不持久化**（settlement
        交给 RunController 转 PersistenceManager；HLS 残段 / 剩余检测结果归 recording，告警落库归
        persistence，前端槽清零亦由 RunController 做）。调用方（RunController.stop_run）已持
        lock_for(cq.task_id)，与 start_workflow 互斥。无 actor 返 []；别名已由 actor 烧进 alarm.stage。
        """
        task_id = cq.task_id
        logger.info("[InferenceManager] Stopping workflow: task=%s", task_id)

        settlement: List[Alarm] = []
        actor = self._actors.pop(task_id, None)
        if actor is not None:
            try:
                settlement = actor.finalize_and_stop()
            except Exception as e:
                logger.warning(
                    "[InferenceManager] finalize actor failed for task=%s: %s", task_id, e
                )

        # 注：这里**不收尾检测结果**。cq 落盘缓冲里剩下的那点由 RunController 紧接着调的
        # `recording.flush_residual(cq)` 一并交出（它在本方法之后、cq.close() 之前）。
        logger.info("[InferenceManager] Workflow stopped: task=%s", task_id)
        return settlement

    def status(self) -> Dict[str, Any]:
        clients = client_manager.snapshot()
        stats = {task_id: cq.get_queue_depths() for task_id, cq in clients.items()}
        return {"clients": len(clients), "queues": stats}

    # ========== 启动/停止 ==========

    def start(self):
        logger.info("[InferenceManager] 启动中...")

        # 重组件在此建（构造期零副作用，见 __init__ docstring）
        self._build_components()

        self._model_worker_service.start()
        self.visualization_pool.stage_configs = self._get_stage_configs()
        self.visualization_pool.start()
        # 注：persistence 生命周期已上移 lifespan（persistence.lifespan 嵌套于 inference.lifespan 外层），
        # 不再由本类驱动 start/stop——inference 不拥有平级服务的生命周期。

        # 初始化全局映射（均由 YAML 驱动）：
        #   task_name → AlarmMetric（实时信号指标）
        #   stage 主键(step_id) → alias（写告警 step_name + 可视化叠字）
        from app.services.inference.stage_factory import StageFactory
        from app.services.inference.config import load_stage_config
        from .naming import _set_task_metric_map, _set_stage_alias_map
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
        # alarm.stage，与 actor 实时路径同款 sink 调用）——此时 persistence 仍在跑
        # （persistence.lifespan 于 inference.lifespan 外层，停在 inference 之后）。
        for task_id, actor in actors:
            try:
                settlement = actor.finalize_and_stop()
                if settlement:
                    cq = client_manager.get(task_id)
                    if cq:
                        alarm_sink.persist_alarms(
                            settlement, cq=cq, mode=ALARM_MODE_SETTLEMENT
                        )
            except Exception as e:
                logger.warning(
                    "[InferenceManager] Settlement alarms on stop failed for task=%s: %s",
                    task_id, e,
                )

        # 注：停机时不再 flush 检测结果——落盘缓冲在 cq 上，recording.lifespan 嵌在 inference 外层
        # （main.py），它的 sweeper 与队列此刻还活着。但 recording.stop 先停 sweeper 再抽队列，
        # 进程直接停机（非 stop_run）时 cq 里最后不到 1 s 的检测结果能否被拉走取决于时序——已接受，
        # 与 HLS 残段同口径。
        # 组件建于 start()，未 start 过就 stop（异常路径 / 测试）时为 None，跳过即可。
        if self.visualization_pool is not None:
            self.visualization_pool.stop()
        # 注：persistence.stop() 已上移 persistence.lifespan（停在 inference 之后，抽干队列）。

        logger.info("[InferenceManager] Stopped")
