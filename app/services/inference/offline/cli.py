"""离线分割手动入口 —— 独立进程、CPU-only、限核、同步跑一次；另含 query 查询子命令。

    CUDA_VISIBLE_DEVICES="" nice -n 15 \\
        python -m app.services.inference.offline.cli run --task-id 100 --step-id 2 [--strategy PATH]
    python -m app.services.inference.offline.cli query --task-id 100 --step-id 2

设计：本进程与在线后端（uvicorn）、mediamtx 网关无任何代码/进程耦合——独立启动，不抢在线 GPU/核。
`run` 的 CPU 隔离在**任何 torch import 之前**生效：置 `CUDA_VISIBLE_DEVICES=""`（禁 GPU）+
`torch.set_num_threads`（限核，默认 2），故必须先 `_isolate_cpu()` 再 import 触发策略 torch 加载的
runner/策略模块。`query` 只读 FactLedger，不碰 torch/runner。

step_id 恒为**数字存储键**（--step-id int）；未配数字（如 -1）经 config.resolve_stage 回退到
MOCK stage 配置，存储路径仍用原数字（见 runner.py）。

退出码：completed / skipped → 0；配置错误 / 输入损坏 / 策略异常 / 写失败 → 非 0。
一期不做排队/并发/自动触发；只对已封口（step 已停写）的数据手动运行。
"""

from __future__ import annotations

import argparse
import json
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


def _run(args: argparse.Namespace) -> int:
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


def _query(args: argparse.Namespace) -> int:
    """轻量查询：读 FactLedger 里的 SegmentFact 时间线打印（不碰 torch/runner）。"""
    from app.services.inference.feature.store import FactLedger
    from app.services.inference.models import SegmentFact
    from app.settings import settings

    ledger = FactLedger(settings.storage_base_dir)
    rows = [
        f.to_json()
        for f in ledger.load(args.task_id, args.step_id)
        if isinstance(f, SegmentFact) and (args.source is None or f.source == args.source)
    ]
    rows.sort(key=lambda r: (float(r.get("start", 0.0)), str(r.get("label", ""))))
    print(json.dumps(
        {"task_id": args.task_id, "step_id": args.step_id, "timeline": rows},
        ensure_ascii=False, indent=2,
    ))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inference.offline.cli",
        description="离线全序列分割：读 FeatureStore 特征 → 策略分段 → 幂等写 FactLedger。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="读特征、跑策略、幂等写 FactLedger")
    run.add_argument("--task-id", type=int, required=True, help="任务 id（存储键）")
    run.add_argument("--step-id", type=int, required=True, help="洗消步骤 id（数字存储键；未配回退 MOCK）")
    run.add_argument(
        "--strategy", default=None,
        help="覆盖 stage.offline.class 的策略全限定路径（开发期对比不同策略）",
    )
    run.add_argument(
        "--threads", type=int, default=2, help="CPU 线程数（torch.set_num_threads，默认 2）",
    )

    query = sub.add_parser("query", help="查询 FactLedger 里的 SegmentFact 时间线")
    query.add_argument("--task-id", type=int, required=True)
    query.add_argument("--step-id", type=int, required=True)
    query.add_argument("--source", default=None, help="只查询某个 SegmentFact source")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "run":
        return _run(args)
    if args.command == "query":
        return _query(args)
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
