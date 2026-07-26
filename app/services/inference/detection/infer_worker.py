"""推理子进程 entrypoint（multiprocessing spawn target）：独占 GIL 跑 YOLO GPU 前向。

根因见 docs/update/20260726_INFER_LAUNCH_BOUND_DIAGNOSIS.md：kernel 发射线程在主进程被
viz/temporal/HLS/dispatcher 抢 GIL 饿到，GPU 时钟满却 SM 空转、前向读数被吹大。把 GPU 前向
拆进独立进程独占一把 GIL 即根治。本模块只负责「收批帧 → _infer_models → 回结果」，**不碰
cq / 写回 / FeatureStore**——那些留主进程按 req_id 关联（见 remote_infer.RemoteInferProxy）。

进程边界只过纯数据（均 picklable）：
  req  (main→child):  (req_id:int, stage:str, frames:List[np.ndarray], timestamps:List[float])
  resp (child→main):  (req_id:int, merged:List[Dict[str,FrameDetections]])
  观测量（infer_ms / error_type）复用 FrameDetections.metadata，不另立 stats 通道。
  控制哨兵：req 端收到 None → 优雅退出循环。

导入序契约：本模块**顶层不 import torch / pool**（pool.py 顶层 import torch 会触发 CUDA
运行时装载）。`run_infer_worker` 先按参数钉 CUDA_VISIBLE_DEVICES，**再**在函数内 import——
镜像 offline/cli.py 的隔离序，方向相反（那边置空强制 CPU，这边钉到目标卡）。
"""

from __future__ import annotations

import logging
import os
import signal
from typing import List


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
    logger = logging.getLogger(__name__)
    logger.info("启动 pid=%s stages=%s cuda=%r", os.getpid(), active_stages, cuda_device)

    # ── 2. 建各 stage pool（复用主进程同一 StageFactory 代码路径，从 YAML 自建、零新推理逻辑）──
    from app.services.inference.config import load_stage_config
    from app.services.inference.stage_factory import StageFactory
    from app.services.inference.detection.pool import MultiModelWorkerPool

    config = load_stage_config()
    factory = StageFactory(config)
    pools = {}
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

    logger.info("[infer-child] 推理子进程退出 pid=%s", os.getpid())
