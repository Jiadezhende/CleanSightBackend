"""离线分割手动入口 —— 独立进程、钉卡、限核、同步跑一次；另含 check / query / materials 子命令。

    nice -n 15 python -m app.services.inference.offline.cli run --task-id 100 --step-id 2 [--run-id R] [--threads 2]
    python -m app.services.inference.offline.cli check --task-id 100 --step-id 2 [--run-id R] [--dry-run]
    python -m app.services.inference.offline.cli query --task-id 100 --step-id 2 [--run-id R]
    python -m app.services.inference.offline.cli materials            # 各 stage 激活策略的物料核对，缺失退出码 1

设计：本进程与在线后端（uvicorn）、mediamtx 网关无任何代码/进程耦合——独立启动。
`run` 的隔离在**任何 torch import 之前**生效：`CUDA_VISIBLE_DEVICES` 钉成 `settings.cuda_device`（与在线
推理子进程同一张卡，不碰机器上别的卡；""=CPU）+ `torch.set_num_threads`（限核，默认 2），故必须先
`_configure_run_process()` 再 import 触发策略 torch 加载的 worker/策略模块。用不用 GPU、限多少速由策略参数定。
`run` 分割 completed 后接着跑规则判定（`compliance.checker.check_run`），写 alarms.jsonl；核对失败只记进 message，
不改分割结果的 status 与退出码。`check` 只重跑核对（`--dry-run` 逐行打印全部判定、不写盘）。
`query` 只读 temporal.jsonl，不碰 torch/worker。

step_id 恒为**数字存储键**（--step-id int）；未配置 / 无离线模型的 step 直接报错（无兜底）。
`--run-id` 缺省 = 该 step 最新可见 run；入口解析一次，之后整次运行只读写这个 run。

输出：stdout 末行恒为一行结果 JSON `{status, producer, segment_count, alarm_count, message}`（失败时 status="error"；
未核对时 alarm_count 为 null），作业服务（offline/service.py）以子进程调用时解析它。
退出码：completed / skipped / reclaimed（点名的 run 已被回收）→ 0；step 未配置 / 输入损坏 /
策略异常 / 写失败 → 非 0。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Optional, Sequence

logger = logging.getLogger("offline.cli")


def _pin_cuda_device() -> None:
    """可见卡钉成在线推理子进程那一张（`settings.cuda_device`，""=CPU）。"""
    from app.settings import settings

    os.environ["CUDA_VISIBLE_DEVICES"] = settings.cuda_device


def _configure_run_process(num_threads: int) -> None:
    """钉卡 + 限 torch 线程。**须在任何 torch import 之前调用。**"""
    _pin_cuda_device()
    try:
        import torch  # 此处才首次 import torch，CUDA_VISIBLE_DEVICES 已钉好
        torch.set_num_threads(max(1, num_threads))
    except Exception:  # torch 未安装 / 占位策略不需要 torch —— 不阻断
        pass


def _handle_run(args: argparse.Namespace) -> int:
    _configure_run_process(args.threads)
    # worker / 策略 import 放在隔离之后：策略模块的 torch import 此时才发生
    from .worker import OfflineRunSpec, OfflineWorker

    try:
        result = OfflineWorker().run(
            OfflineRunSpec(task_id=args.task_id, step_id=args.step_id, run_id=args.run_id)
        )
    except Exception as e:  # 配置/输入/策略/写失败 → 非 0
        logger.error("运行失败 task=%s step=%s: %s", args.task_id, args.step_id, e, exc_info=True)
        _print_run_result("error", None, 0, None, str(e))
        return 1
    _print_run_result(result.status, result.producer, result.segment_count, result.alarm_count, result.message)
    return 0


def _print_run_result(
    status: str, producer: Optional[str], segment_count: int, alarm_count: Optional[int], message: str
) -> None:
    # 人读与作业服务共用这一行，故不转义中文；作业服务给子进程置了 PYTHONIOENCODING=utf-8 并按 utf-8 解码。
    print(json.dumps(
        {"status": status, "producer": producer, "segment_count": segment_count, "alarm_count": alarm_count,
         "message": message},
        ensure_ascii=False,
    ))


def _handle_check(args: argparse.Namespace) -> int:
    """只重跑规则核对。末行 JSON `{status, run_id, alarm_count, message}`；`--dry-run` 先逐行打印每条判定。"""
    from dataclasses import asdict

    from .worker import OfflineRunSpec, OfflineWorker

    result = OfflineWorker().check(
        OfflineRunSpec(task_id=args.task_id, step_id=args.step_id, run_id=args.run_id), dry_run=args.dry_run,
    )
    if args.dry_run:
        for verdict in result.verdicts:
            print(json.dumps(asdict(verdict), ensure_ascii=False))
    print(json.dumps({
        "status": result.status, "run_id": result.run_id, "alarm_count": result.alarm_count, "message": result.message,
    }, ensure_ascii=False))
    return 1 if result.status == "error" else 0


def _handle_query(args: argparse.Namespace) -> int:
    """轻量查询：读 temporal.jsonl 里的 TemporalSegment 时间线打印（不碰 torch/worker）。"""
    from dataclasses import asdict

    from app.types.temporal import TemporalSegment
    from app.storage import inference as inference_store
    from app.storage import runs

    run = runs.query(args.task_id, args.step_id, args.run_id)
    facts = inference_store.read_temporal(run) if run is not None else []
    rows = [
        asdict(f)
        for f in facts
        if isinstance(f, TemporalSegment)
        and (args.producer is None or f.producer == args.producer)
    ]
    rows.sort(key=lambda r: (float(r.get("start", 0.0)), str(r.get("label", ""))))
    print(json.dumps(
        {
            "task_id": args.task_id, "step_id": args.step_id,
            "run_id": run.run_id if run is not None else None, "timeline": rows,
        },
        ensure_ascii=False, indent=2,
    ))
    return 0


def _handle_materials(args: argparse.Namespace) -> int:
    """各 stage 激活的离线策略要哪些物料、在不在；有缺失退出码 1（部署核对用）。"""
    from .segmentation.materials import check_materials
    from app.services.inference.config import load_stage_config

    statuses = check_materials(load_stage_config())
    print(json.dumps(
        [{"stage": s.stage, "producer": s.producer, "path": str(s.path), "exists": s.exists} for s in statuses],
        ensure_ascii=False, indent=2,
    ))
    return 0 if all(s.exists for s in statuses) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inference.offline.cli",
        description="离线全序列分割：读 detections.jsonl → 策略分段 → 幂等写 temporal.jsonl。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="读检测结果、跑策略、幂等写 temporal.jsonl")
    run.add_argument("--task-id", type=int, required=True, help="任务 id（存储键）")
    run.add_argument("--step-id", type=int, required=True, help="洗消步骤 id（数字存储键；须在推理配置中配了 offline）")
    run.add_argument("--run-id", type=int, default=None, help="锁定哪个 run（缺省 = 该 step 最新可见 run）")
    run.add_argument(
        "--threads", type=int, default=2, help="CPU 线程数（torch.set_num_threads，默认 2）",
    )

    check = sub.add_parser("check", help="只重跑规则核对，写 alarms.jsonl（--dry-run 不写盘、打印全部判定）")
    check.add_argument("--task-id", type=int, required=True)
    check.add_argument("--step-id", type=int, required=True)
    check.add_argument("--run-id", type=int, default=None, help="核对哪个 run（缺省 = 该 step 最新可见 run）")
    check.add_argument("--dry-run", action="store_true", help="不写盘，逐行打印每条规则的判定")

    query = sub.add_parser("query", help="查询 temporal.jsonl 里的 TemporalSegment 时间线")
    query.add_argument("--task-id", type=int, required=True)
    query.add_argument("--step-id", type=int, required=True)
    query.add_argument("--run-id", type=int, default=None, help="查哪个 run（缺省 = 该 step 最新可见 run）")
    query.add_argument("--producer", default=None, help="只查询某个 producer 产出的分段")

    sub.add_parser("materials", help="核对各 stage 激活的离线策略物料是否齐全（缺失退出码 1）")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "run":
        return _handle_run(args)
    if args.command == "check":
        return _handle_check(args)
    if args.command == "query":
        return _handle_query(args)
    if args.command == "materials":
        return _handle_materials(args)
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
