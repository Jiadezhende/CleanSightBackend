"""多模型推理 Worker Pool（同一 stage 下多模型串行）+ 推理子进程 entrypoint。

完全解耦版本：使用 InferenceWorkflow 基类，不依赖 pipeline_base。

关键特性：
- 每个 stage 配置多个模型（基于 InferenceWorkflow）
- 逐模型串行推理（ultralytics predict 内部同步阻塞，自定义 CUDA Stream 对其零并行
  收益、反而每次前向掺入跨流全设备同步，已拆除）
- 调用 InferenceWorkflow.infer_batch 接口

进程边界：本模块底部的 `run_infer_worker` 是 multiprocessing spawn target（子进程独占 GIL 跑
GPU 前向，见 infer_proxy.RemoteInferProxy）。**故本模块顶层不 import torch**——torch on-CPU
本身无害，但 spawn 子进程重 import 本模块解析 target 时会跑顶层代码，顶层若 import torch 会早于
run_infer_worker 钉 CUDA_VISIBLE_DEVICES；torch/ultralytics 一律钉设备后在函数内惰性 import。
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Dict, List, Sequence

import numpy as np

from app.services.inference.detection.detector import Detector
from app.domain.detection import FrameDetections
from app.services.inference.models import DetectionTask, FrameInference

logger = logging.getLogger(__name__)


class MultiModelWorkerPool:
    """多模型推理 Worker Pool（同一 stage 下多模型串行）。

    完全解耦版本：
    - 使用 InferenceWorkflow 基类（不依赖 pipeline_base）
    - 逐模型串行推理（无 CUDA Stream：对同步阻塞的 predict 无并行收益）
"""

    def __init__(
        self,
        stage: str,
        models: Sequence[Detector],
    ):
        """
        Args:
            stage: Stage 名称（LEAK/CLEAN）
            models: 该 stage 对应的模型列表（基于 InferenceWorkflow）
        """
        self.stage = stage
        self.models = list(models)

        logger.info(
            f"MultiModelWorkerPool initialized: stage={stage}, models={len(self.models)}"
        )

    def infer_batch(self, batch: List[DetectionTask]) -> List[FrameInference]:
        """批量推理并组装 FrameInference（**进程内**路径，供单测/回退；生产走进程隔离）。

        生产热路径已拆进程：子进程只调 `_infer_models`（纯数据、无 cq），主进程 collector
        据 pending 记录重组 FrameInference。本方法保留同一组装语义（cq 透传 + 帧分辨率盖章 +
        ts 锚点穿透），锁死 ts-anchor 不变式的单测仍走它；此路径不发 Prometheus 埋点。

        Returns:
            推理结果列表（FrameInference.detections = {detector_name: FrameDetections}）
        """
        if not batch:
            return []

        n = len(batch)
        frames = [req.frame for req in batch]
        # 帧捕获时间戳（真值锚点，源自 Frame.timestamp）：穿透到 detector，
        # 令每帧 FrameDetections.timestamp == FrameInference.timestamp，供下游多流对齐
        timestamps = [req.timestamp for req in batch]

        # 逐模型串行推理（每模型对整批帧跑一次 infer_batch）
        model_results = self._infer_models(frames, timestamps)

        # 构造输出：将每帧的 model_results 关联到对应的客户端
        # model_results[i] = {task_name: FrameDetections}
        results: List[FrameInference] = []
        for i, req in enumerate(batch):
            per_frame_results = model_results[i] if i < len(model_results) else {}

            result = FrameInference(
                task_id=req.task_id,
                stage=req.stage,
                timestamp=req.timestamp,
                detections=per_frame_results,
                cq=req.cq,  # 透传捕获句柄，写回凭它投递
                # 帧分辨率从原始帧盖章：fan-out 前的每帧常量，帧此后即销毁（frame.shape = H, W, C）
                frame_width=int(req.frame.shape[1]),
                frame_height=int(req.frame.shape[0]),
            )
            results.append(result)

        return results

    def _infer_models(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[Dict[str, FrameDetections]]:
        """逐模型串行推理：每个模型对整批帧跑一次，结果按帧索引合并。

        **纯数据进出、无 cq、无 Prometheus 副作用**——这是进程边界的天然切口：子进程调它、
        回传 `merged`，主进程 collector 直接据 merged 里的 `FrameDetections` 发埋点（跨进程
        registry 无效，故埋点上移主进程）。单模型失败就地降级为 success=False（不上抛，batch
        跨多 run）。

        观测量复用 `FrameDetections`（不另立 stats 通道）——其 metadata 文档语义即「模型名称、
        推理时间等」：成功帧记 `metadata["infer_ms"]`=该模型整批 wall（parent 除以帧数得每帧均值），
        失败帧另记 `metadata["error_type"]`（=异常类名，供 Prometheus 低基数 label）。失败信号本就
        由 `FrameDetections.success` 表达，无需再存一份。

        Returns:
            merged[i] = {detector_name: FrameDetections}（与 frames 一一对应）。
        """
        n = len(frames)
        merged: List[Dict[str, FrameDetections]] = [{} for _ in range(n)]
        if n == 0:
            return merged

        for model in self.models:
            if not model.enabled:
                continue

            start_time = time.time()
            try:
                # 调用 InferenceTask.infer_batch （已经返回 FrameDetections 格式）
                batch_res = model.infer_batch(frames, timestamps)
                elapsed_ms = (time.time() - start_time) * 1000
                for i in range(min(len(batch_res), n)):
                    fd = batch_res[i]
                    fd.metadata["infer_ms"] = elapsed_ms  # 该模型整批 wall（观测复用 FrameDetections）
                    merged[i][model.name] = fd

            except Exception as e:
                logger.error("[MultiModelWorkerPool] %s infer_batch error: %s", model.name, e, exc_info=True)

                elapsed_ms = (time.time() - start_time) * 1000
                for i in range(n):
                    merged[i][model.name] = FrameDetections(
                        detections=[],
                        metadata={"error": str(e), "error_type": type(e).__name__, "infer_ms": elapsed_ms},
                        timestamp=timestamps[i],
                        success=False,
                        error=str(e)
                    )

        return merged

    def warmup(self, batch_size: int = 1) -> None:
        """模型预热：执行dummy推理以消除冷启动延迟。

        Args:
            batch_size: 预热批次大小，默认1（建议与实际batch_size一致）

        工作原理：
        1. 生成dummy输入（与真实输入shape一致）
        2. 执行一次完整的并行推理流程
        3. 触发模型加载、CUDA内核编译、显存分配
        4. 丢弃预热结果
        """
        import time

        logger.info(f"Starting model warmup for stage {self.stage}...")

        # 生成dummy输入（640x480 BGR图像）
        dummy_frames = [
            np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(batch_size)
        ]
        dummy_timestamps = [0.0] * batch_size

        start_time = time.time()

        try:
            # 执行一次完整推理（触发模型加载和CUDA初始化）；warmup 丢弃结果
            _results = self._infer_models(dummy_frames, dummy_timestamps)

            elapsed = time.time() - start_time

            logger.info(
                f"Model warmup completed for stage {self.stage}: "
                f"elapsed={elapsed*1000:.1f}ms, models={len(self.models)}"
            )

            # 清理显存缓存（可选，避免预热占用过多显存）。torch 惰性 import：保持本模块
            # 顶层 torch-free（见模块 docstring 的 spawn 导入序契约）。
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

        except Exception as e:
            logger.error(
                f"Model warmup failed for stage {self.stage}: {e}", exc_info=True
            )


# ═══════════════════════ 推理子进程 entrypoint ═══════════════════════
#
# multiprocessing spawn target：子进程独占 GIL 跑 YOLO GPU 前向（根因见
# docs/update/20260726_INFER_LAUNCH_BOUND_DIAGNOSIS.md：kernel 发射线程在主进程被
# viz/temporal/HLS/dispatcher 抢 GIL 饿到，GPU 时钟满却 SM 空转、前向读数被吹大）。
# 只负责「收批帧 → _infer_models → 回结果」，**不碰 cq / 写回 / FeatureStore**——那些
# 留主进程按 req_id 关联（见 infer_proxy.RemoteInferProxy）。
#
# 进程边界只过纯数据（均 picklable）：
#   req  (main→child):  (req_id:int, stage:str, frames:List[np.ndarray], timestamps:List[float])
#   resp (child→main):  (req_id:int, merged:List[Dict[str,FrameDetections]])
#   观测量（infer_ms / error_type）复用 FrameDetections.metadata，不另立 stats 通道。
#   控制哨兵：req 端收到 None → 优雅退出循环。
#
# 导入序契约见模块 docstring：本模块顶层不 import torch；torch/ultralytics 一律在下方钉
# CUDA_VISIBLE_DEVICES 之后惰性 import（镜像 offline/cli.py 的隔离序，方向相反——那边置空
# 强制 CPU，这边钉到目标卡）。


def run_infer_worker(req_q, resp_q, ready_ev, active_stages: List[str], cuda_device: str = "0") -> None:
    """子进程主函数：建各 stage pool → warmup → 循环消费 req_q。

    Args:
        req_q: 请求队列（main→child），元素 (req_id, stage, frames, timestamps) 或 None（退出哨兵）。
        resp_q: 响应队列（child→main），元素 (req_id, merged)。
        ready_ev: multiprocessing.Event，pool 建好 + warmup 尝试后置位（就绪屏障）。
        active_stages: 需建 pool 的 stage 主键列表（= 主进程已筛出有 detector 的 stage）。
        cuda_device: 钉给 CUDA_VISIBLE_DEVICES 的值（"0"/"1"…；""=CPU，仅测试）。
    """
    # ── 0. 忽略 SIGINT：Ctrl-C 会广播给整个进程组（含本子进程），若不忽略会在阻塞的
    # req_q.get() 处抛 KeyboardInterrupt 打出丑陋 traceback。关停统一由父进程处理——
    # 父进程收 SIGINT → 走 lifespan → proxy.stop() → _kill_child 的 terminate()（SIGTERM，
    # 本进程仍走默认处理即时退出）驱动本子进程收摊。这是 multiprocessing 池的标准做法。
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # ── 1. 环境隔离：先钉 CUDA 设备，再触发任何 torch/ultralytics 装载 ──
    os.environ["CUDA_VISIBLE_DEVICES"] = cuda_device

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [infer-child] %(name)s: %(message)s",
    )
    # 进程身份前缀由上面的 log format 统一注入（`[infer-child]`），消息里不再重复。
    logger.info("启动 pid=%s stages=%s cuda=%r", os.getpid(), active_stages, cuda_device)

    # ── 2. 建各 stage pool（复用主进程同一 StageFactory 代码路径，从 YAML 自建、零新推理逻辑）──
    from app.services.inference.config import load_stage_config
    from app.services.inference.stage_factory import StageFactory

    config = load_stage_config()
    factory = StageFactory(config)
    pools: Dict[str, "MultiModelWorkerPool"] = {}
    for stage in active_stages:
        detectors = factory.create_detectors_for_stage(stage)
        if detectors:
            pools[stage] = MultiModelWorkerPool(stage=stage, models=detectors)
        else:
            logger.warning("stage %s 无 detector，跳过", stage)

    # ── 3. warmup（模型加载 + CUDA init 均在此发生；失败不致命，首帧会重试降级）──
    batch_size = config.batch_size
    for stage, pool in pools.items():
        try:
            pool.warmup(batch_size=batch_size)
        except Exception as e:  # pragma: no cover - warmup 内部已兜底，此为双保险
            logger.error("stage %s warmup 失败: %s", stage, e, exc_info=True)

    ready_ev.set()  # 就绪屏障：pool 建好 + warmup 尝试完成
    logger.info("ready，进入推理循环")

    # ── 4. 主循环：FIFO 消费 req_q（单进程串行，天然保序）──
    while True:
        try:
            item = req_q.get()
        except (EOFError, OSError):
            # 父进程关闭队列/退出 → 收摊
            logger.info("req_q 关闭，退出")
            break

        if item is None:
            logger.info("收到退出哨兵，退出")
            break

        req_id, stage, frames, timestamps = item
        pool = pools.get(stage)
        if pool is None:
            # 未知 stage：回空结果让主进程 pop pending、不泄漏在途槽
            resp_q.put((req_id, [{} for _ in frames]))
            continue

        try:
            merged = pool._infer_models(frames, timestamps)
            resp_q.put((req_id, merged))
        except Exception as e:  # pragma: no cover - _infer_models 内部逐模型兜底，此为双保险
            logger.error("req_id=%s 推理异常: %s", req_id, e, exc_info=True)
            # 仍须应答让主进程 pop pending 不泄漏（_infer_models 逐模型已兜底，此路径实际到不了）
            resp_q.put((req_id, [{} for _ in frames]))

    logger.info("推理子进程退出 pid=%s", os.getpid())
