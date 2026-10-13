"""渲染子进程 entrypoint：从共享内存槽读原始帧 → FixedVisualizer 渲染 → 结果写回同一槽 → 答复。

multiprocessing spawn target（见 render_proxy.RemoteRenderProxy）。本模块与它的 import 链**不得拉起
torch / ultralytics**：子进程只需要 cv2 + PIL，多一个 torch 就是每个子进程几百 MB 常驻。

进程边界只过纯数据：
  req  (main→child):  (req_id, slot, shape, specs: List[RenderSpec], stage: str, events: List[str]) | None
  resp (child→main):  (req_id, out_shape, render_seconds, ok)
渲染失败时不写槽（槽里仍是原始帧），答复 ok=False——主进程照样读槽，即「未标注原帧」兜底。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

logger = logging.getLogger(__name__)


def run_renderer(req_q, resp_q, ready_ev, ring_spec) -> None:
    """渲染子进程主函数。"""
    if threading.current_thread() is threading.main_thread():
        # Ctrl-C 广播给整个进程组；关停统一由父进程 terminate 驱动（同推理子进程的做法）
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [render-child] %(name)s: %(message)s",
    )
    from app.services.utils.frame_ring import SharedFrameRingView
    from .visualizer import FixedVisualizer

    view = SharedFrameRingView(ring_spec)
    vis = FixedVisualizer()
    ready_ev.set()
    logger.info("启动 pid=%s", os.getpid())
    try:
        serve(req_q, resp_q, view, vis)
    finally:
        view.close()
    logger.info("退出 pid=%s", os.getpid())


def serve(req_q, resp_q, view, vis) -> None:
    """请求循环（与进程装配分开，便于在测试线程里直接驱动）。"""
    while True:
        try:
            item = req_q.get()
        except (EOFError, OSError):
            break
        if item is None:
            break
        req_id, slot, shape, specs, stage, events = item
        t0 = time.perf_counter()
        ok = True
        out_shape = shape
        try:
            frame = view.read(slot, shape)
            annotated = vis.render(frame=frame, vis_data_list=specs, stage=stage, temporal_events=events)
            view.write(slot, annotated)
            out_shape = annotated.shape
        except Exception as e:
            ok = False
            logger.error("req_id=%s 渲染失败: %s", req_id, e, exc_info=True)
        resp_q.put((req_id, out_shape, time.perf_counter() - t0, ok))
