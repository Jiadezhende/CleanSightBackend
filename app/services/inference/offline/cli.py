"""离线链路唯一手动入口 —— 独立进程、默认 CPU、同步跑一次。

    CUDA_VISIBLE_DEVICES="" nice -n 15 \\
        python -m app.services.inference.offline.cli infer  --task-id 100 --step-id 2 [--segmenter PATH]
        python -m app.services.inference.offline.cli export --task-id 100 --step-id 2 --segmenter PATH
        python -m app.services.inference.offline.cli query  --task-id 100 --step-id 2
        python -m app.services.inference.offline.cli diagnose | contract

本模块只做**参数与进程**：argparse、设备隔离、退出码、缓存 gc 触发。编排（造 Segmenter、
校验事实、写 FactLedger）在 `runner.py`。两者曾合并过，实测合并后本文件 415 行——超过
「薄到可以并进 CLI」的判据，故拆开；各自都在 250 行以内。

`export` 与 `infer` 走**同一个 Segmenter 类**：`--segmenter` 指哪个类，导出的就是那个
模型推理时实际吃的字节。故训练样例与线上输入不可能漂——不是靠纪律，是只有一条路径。

设计：本进程与在线后端（uvicorn）、mediamtx 网关无任何代码/进程耦合——独立启动，不抢在线
GPU/核。设备隔离在**任何 torch import 之前**生效，故所有会触发 torch 的 import 一律放在
`_isolate()` 之后。`query` / `diagnose` / `contract` 只读产物，不碰 torch。

step_id 恒为**数字存储键**（--step-id int）；未配数字（如 -1）经 config.resolve_stage 回退到
MOCK stage 配置，存储路径仍用原数字。

退出码：completed / skipped → 0；配置错误 / 输入损坏 / 策略异常 / 写失败 → 非 0。
不做排队/并发/自动触发；只对已封口（step 已停写）的数据手动运行。
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("offline.cli")


def _isolate(device: str, num_threads: int) -> None:
    """设备隔离。**须在任何 torch import 之前调用。**

    device=cpu（默认）：置空 CUDA_VISIBLE_DEVICES 禁 GPU + 限 torch 线程，不抢在线资源。
    device=cuda：不置，交给 torch 正常发现 GPU（批量重抽视觉特征时需要）。
    """
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    try:
        import torch  # 此处才首次 import torch，隔离已生效
        torch.set_num_threads(max(1, num_threads))
    except Exception:  # torch 未安装 / 本次策略不需要 —— 不阻断
        pass


# ==================== 子命令 ====================


def _cmd_infer(args: argparse.Namespace) -> int:
    _isolate(args.device, args.threads)
    from app.services.inference.offline import blocks
    from app.services.inference.offline.runner import run_infer

    blocks.sweep_cache(args.cache_ttl_days)
    try:
        result = run_infer(args)
    except Exception as e:  # 配置/输入/策略/写失败 → 非 0
        logger.error("运行失败 task=%s step=%s: %s", args.task_id, args.step_id, e, exc_info=True)
        print(f"error task={args.task_id} step={args.step_id}: {e}")
        return 1
    line = f"{result.status} producer={result.producer} segment_count={result.segment_count}"
    if result.message:
        line += f" | {result.message}"
    print(line)
    return 0  # completed / skipped 均为 0


def _cmd_export(args: argparse.Namespace) -> int:
    _isolate(args.device, args.threads)
    from app.services.inference.offline import blocks
    from app.services.inference.offline.runner import run_export

    blocks.sweep_cache(args.cache_ttl_days)
    try:
        result = run_export(args)
    except Exception as e:
        logger.error("导出失败 task=%s step=%s: %s", args.task_id, args.step_id, e, exc_info=True)
        print(f"error task={args.task_id} step={args.step_id}: {e}")
        return 1
    line = f"{result.status} frames={result.segment_count}"
    if result.message:
        line += f" out={result.message}"
    print(line)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    """轻量查询：读 FactLedger 里的 SegmentFact 时间线打印（不碰 torch）。"""
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


def _cmd_diagnose(args: argparse.Namespace) -> int:
    """扫导出产物，报无效（恒定/重复）特征列。不碰 torch/backbone。"""
    from app.services.inference.offline.blocks import cache
    from app.services.inference.offline.diagnose import format_report, scan_columns

    root = Path(args.export_root) if args.export_root else cache.cache_root()
    print(format_report(scan_columns(root, tag_filter=args.tag)))
    return 0


def _cmd_contract(args: argparse.Namespace) -> int:
    """先验契约检查：特征契约声明的目标类别 vs 检测器实际产出的类别名。"""
    from app.services.inference.offline.diagnose import check_object_contract

    module_path, _, attr = args.objects.rpartition(".")
    objects = getattr(importlib.import_module(module_path), attr)
    never_produced, never_consumed = check_object_contract(
        objects, [Path(p) for p in args.checkpoints]
    )
    print(f"契约声明 {len(objects)} 类；检测器 checkpoint {len(args.checkpoints)} 个")
    if never_produced:
        print(f"\n✗ 契约声明但无检测器产出（这些特征列必然恒零，应从契约移除）: {len(never_produced)}")
        for n in never_produced:
            print(f"    {n}")
    else:
        print("\n✓ 契约里没有检测器不产出的类别")
    if never_consumed:
        print(f"\n⚠ 检测器会产出但契约未消费（白丢的检测信号）: {len(never_consumed)}")
        for n in never_consumed:
            print(f"    {n}")
    return 1 if never_produced else 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", type=int, required=True, help="任务 id（存储键）")
    parser.add_argument("--step-id", type=int, required=True, help="洗消步骤 id（数字存储键；未配回退 MOCK）")
    parser.add_argument(
        "--segmenter", default=None,
        help="覆盖 stage.offline.class 的 Segmenter 全限定路径（开发期对比不同模型）",
    )
    parser.add_argument(
        "--device", default="cpu", choices=("cpu", "cuda"),
        help="默认 cpu（禁 GPU，不抢在线资源）",
    )
    parser.add_argument(
        "--threads", type=int, default=2, help="CPU 线程数（torch.set_num_threads，默认 2）",
    )
    parser.add_argument(
        "--cache-ttl-days", type=int, default=30,
        help="离线缓存保留天数（每次执行开头自动清理；默认 30，刻意长于 raw 段的 7 天 TTL）",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inference.offline.cli",
        description="离线链路：取特征块 → Segmenter → 幂等写 FactLedger / 导出训练样例。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    infer = sub.add_parser("infer", help="跑策略并幂等写 FactLedger")
    _add_common(infer)

    export = sub.add_parser("export", help="产训练样例（与推理同一个 Segmenter，不前向）")
    _add_common(export)
    export.add_argument("--out-dir", default=None, help="产物目录；缺省落 {offline_base_dir}/.cache/{task}/{step}/")

    query = sub.add_parser("query", help="查询 FactLedger 里的 SegmentFact 时间线")
    query.add_argument("--task-id", type=int, required=True)
    query.add_argument("--step-id", type=int, required=True)
    query.add_argument("--source", default=None, help="只查询某个 SegmentFact source")

    diag = sub.add_parser(
        "diagnose", help="扫导出产物找无效特征列（恒定 / 重复）；≥2 个 step 才能判结构性",
    )
    diag.add_argument("--export-root", default=None, help="产物根目录；缺省用 .cache")
    diag.add_argument("--tag", default=None, help="只看某个 Segmenter")

    con = sub.add_parser("contract", help="先验契约检查：特征契约的目标类别 vs 检测器实际类别名")
    con.add_argument(
        "--objects", default="app.services.inference.offline.blocks.bbox.OBJECTS",
        help="目标类别清单的全限定路径",
    )
    con.add_argument("--checkpoints", nargs="+", required=True, help="部署中的检测器权重路径")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers = {
        "infer": _cmd_infer,
        "export": _cmd_export,
        "query": _cmd_query,
        "diagnose": _cmd_diagnose,
        "contract": _cmd_contract,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"未知命令: {args.command}")
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
