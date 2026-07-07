"""离线分割手动入口 —— 独立进程、CPU-only、限核、同步跑一次。

    CUDA_VISIBLE_DEVICES="" nice -n 15 \\
        python -m app.services.inference.offline.cli --task-id 100 --step-id 2 [--strategy PATH]

设计：本进程与在线后端（uvicorn）、mediamtx 网关无任何代码/进程耦合——独立启动，不抢在线 GPU/核。
CPU 隔离在**任何 torch import 之前**生效：置 `CUDA_VISIBLE_DEVICES=""`（禁 GPU）+ `torch.set_num_threads`
（限核，默认 2），故必须先 `_isolate_cpu()` 再 import 触发策略 torch 加载的 runner/策略模块。

退出码：completed / skipped → 0；配置错误 / 输入损坏 / 策略异常 / 写失败 → 非 0。
一期不做排队/并发/自动触发（见下一期"调度层"）；只对已封口（step 已停写）的数据手动运行。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Sequence

logger = logging.getLogger("offline.cli")


def _isolate_cpu(num_threads: int) -> None:
    """CPU 隔离：禁 GPU + 限 torch 线程。**须在任何 torch import 之前调用。**"""
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    try:
        import torch  # 此处才首次 import torch，已置 CUDA_VISIBLE_DEVICES → 看不到 GPU
        torch.set_num_threads(max(1, num_threads))
    except Exception:  # torch 未安装 / 占位策略不需要 torch —— 不阻断
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inference.offline.cli",
        description="离线全序列分割：读 FeatureStore 特征 → 策略分段 → 幂等写 FactLedger。",
    )
    parser.add_argument("--task-id", type=int, required=True, help="任务 id（存储键）")
    parser.add_argument("--step-id", type=int, required=True, help="洗消步骤 id（= stage 主键）")
    parser.add_argument(
        "--strategy", default=None,
        help="覆盖 stage.offline.class 的策略全限定路径（开发期对比不同策略）",
    )
    parser.add_argument(
        "--threads", type=int, default=2, help="CPU 线程数（torch.set_num_threads，默认 2）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _isolate_cpu(args.threads)

    # runner / 策略 import 放在 CPU 隔离之后：策略模块的 torch import 此时才发生
    from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec

    spec = OfflineRunSpec(task_id=args.task_id, step_id=args.step_id, strategy=args.strategy)
    try:
        result = OfflineRunner().run(spec)
    except Exception as e:  # 配置/输入/策略/写失败 → 非 0
        logger.error("运行失败 task=%s step=%s: %s", args.task_id, args.step_id, e, exc_info=True)
        print(f"error task={args.task_id} step={args.step_id}: {e}")
        return 1

    line = f"{result.status} producer={result.producer} segment_count={result.segment_count}"
    if result.message:
        line += f" | {result.message}"
    print(line)
    return 0  # completed / skipped 均为 0


if __name__ == "__main__":
    sys.exit(main())
