"""离线分割手动入口 —— 独立进程、CPU-only、限核、同步跑一次；另含 query 查询子命令。

    CUDA_VISIBLE_DEVICES="" nice -n 15 \\
        python -m app.services.inference.offline.cli run --task-id 100 --step-id 2 [--strategy PATH]
    python -m app.services.inference.offline.cli query --task-id 100 --step-id 2

设计：本进程与在线后端（uvicorn）、mediamtx 网关无任何代码/进程耦合——独立启动，不抢在线 GPU/核。
`run` 的 CPU 隔离在**任何 torch import 之前**生效：置 `CUDA_VISIBLE_DEVICES=""`（禁 GPU）+
`torch.set_num_threads`（限核，默认 2），故必须先 `_isolate_cpu()` 再 import 触发策略 torch 加载的
runner/策略模块。`query` 只读 FactLedger，不碰 torch/runner。

内存两层防护（输入规模由外部切 step 决定，本进程无从预设上限）：
    ① Runner 准入闸（精确）：策略估算峰值内存，超预算 → `skipped`，见 runner.py；
    ② `RLIMIT_AS`（量级兜底，Linux only）：见 `_limit_address_space`。
预算取 `settings.process_memory_budget_mb`，`--memory-budget-mb` 可覆盖单次运行。

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


def _limit_address_space(budget_mb: int) -> None:
    """给**本进程**设一道地址空间硬上限，作 OOM 的**量级兜底**（Linux only）。

    ⚠️ 只在真正作为独立进程入口跑时调用（`__main__` → `main(apply_process_limits=True)`）。
    `cli.main()` 也会被测试在 pytest 进程内直接调用，那里改 rlimit 会把整个 pytest 进程一起限住。

    精确的那一层是 Runner 的准入闸（策略报成本、超预算返 skipped）；这里只兜"估算失准/
    估算器没覆盖到的路径"，让进程自己带着 MemoryError 死掉，而不是让 OOM killer 去挑
    RSS 最大的进程杀（同机跑着在线 uvicorn 时，被杀的很可能是它）。

    ⚠️ RLIMIT_AS 限的是**虚拟地址空间不是常驻内存**，torch/BLAS 会预留远超实际驻留的地址空间，
    按预算原值设会误杀正常任务。故这里放到 2× 预算（且至少 +2 GB）—— 只挡量级失控，不做精确闸。
    Windows 无 `resource` 模块 → 只有 Runner 那一层，如实记录不假装跨平台。
    """
    try:
        import resource
    except ImportError:
        logger.info("本平台无 RLIMIT_AS（非 Linux），内存兜底只有 Runner 准入闸这一层")
        return
    limit = max(budget_mb * 2, budget_mb + 2048) * 1024 * 1024
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        logger.info("RLIMIT_AS 兜底已设为 %d MB（预算 %d MB）", limit // (1024 * 1024), budget_mb)
    except Exception as e:  # 设不上不阻断：Runner 准入闸仍在
        logger.warning("RLIMIT_AS 设置失败（继续执行，仅剩 Runner 准入闸）: %s", e)


def _run(args: argparse.Namespace, apply_process_limits: bool = False) -> int:
    _isolate_cpu(args.threads)
    # settings 是纯配置读取（无 torch），放在 runner import 之前拿预算给 RLIMIT_AS 用
    from app.settings import settings
    budget_mb = int(args.memory_budget_mb if args.memory_budget_mb is not None
                    else settings.process_memory_budget_mb)
    if apply_process_limits:
        _limit_address_space(budget_mb)

    # runner / 策略 import 放在 CPU 隔离之后：策略模块的 torch import 此时才发生
    from app.services.inference.offline.runner import OfflineRunner, OfflineRunSpec

    spec = OfflineRunSpec(task_id=args.task_id, step_id=args.step_id, strategy=args.strategy)
    try:
        result = OfflineRunner(memory_budget_mb=budget_mb).run(spec)
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
    from app.services.inference.types import SegmentFact
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


def main(argv: Optional[Sequence[str]] = None, *, apply_process_limits: bool = False) -> int:
    """CLI 入口。

    apply_process_limits: 是否给**当前进程**施加 rlimit（RLIMIT_AS 内存兜底）。
        只有作为独立进程跑时才该为 True（见 `__main__`）——测试会在 pytest 进程内直接调
        `main()`，那里改 rlimit 会把整个 pytest 进程限住，故默认 False。
    """
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
    run.add_argument(
        "--memory-budget-mb", type=int, default=None,
        help="本次运行的内存预算（MB），覆盖 settings.process_memory_budget_mb；"
             "超预算的输入返回 skipped（退出码仍为 0）",
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
        return _run(args, apply_process_limits=apply_process_limits)
    if args.command == "query":
        return _query(args)
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main(apply_process_limits=True))
