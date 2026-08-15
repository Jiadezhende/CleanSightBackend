"""离线特征导出手动入口 —— 独立进程、默认 CPU、同步跑一次。

    python -m app.services.inference.offline.export.cli \\
        --task-id 100 --step-id 2 \\
        --recipe app.services.inference.offline.impl.clean.export_r0 \\
        [--backbone yolo] [--device cpu|cuda] [--out-dir PATH] [--threads 2]

设备隔离与离线分割 CLI（../cli.py）的取向一致但**可配**：那边硬置 `CUDA_VISIBLE_DEVICES=""`
永不碰 GPU；这边默认同样禁 GPU 不抢在线资源，但 `--device cuda` 时不置——批量重抽视觉特征
时需要。隔离仍须在**任何 torch import 之前**生效，故 runner 的 import 一律放在 `_isolate()` 之后。

recipe 用**全限定路径**（与分割 CLI 的 `--strategy` 同款），框架因此零业务知识。
退出码：completed / skipped → 0；配置错误 / 输入损坏 / recipe 异常 / 写失败 → 非 0。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("offline.export.cli")


def _isolate(device: str, num_threads: int) -> None:
    """设备隔离。**须在任何 torch import 之前调用。**

    device=cpu（默认）：置空 CUDA_VISIBLE_DEVICES 禁 GPU + 限 torch 线程，不抢在线资源。
    device=cuda：不置，交给 torch 正常发现 GPU。
    """
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    try:
        import torch  # 此处才首次 import torch，隔离已生效
        torch.set_num_threads(max(1, num_threads))
    except Exception:  # torch 未安装 / 本次 recipe 不需要 —— 不阻断
        pass


def _run(args: argparse.Namespace) -> int:
    _isolate(args.device, args.threads)
    # runner / recipe import 放在隔离之后：recipe 模块的 torch import 此时才发生
    from app.services.inference.offline.export.models import ExportSpec
    from app.services.inference.offline.export.runner import ExportRunner

    spec = ExportSpec(
        task_id=args.task_id,
        step_id=args.step_id,
        recipe=args.recipe,
        backbone=args.backbone,
        device=args.device,
        out_dir=args.out_dir,
    )
    try:
        result = ExportRunner().run(spec)
    except Exception as e:  # 配置/输入/recipe/写失败 → 非 0
        logger.error(
            "导出失败 task=%s step=%s recipe=%s: %s",
            args.task_id, args.step_id, args.recipe, e, exc_info=True,
        )
        print(f"error task={args.task_id} step={args.step_id}: {e}")
        return 1

    line = (
        f"{result.status} recipe={result.recipe} "
        f"frames={result.frame_count} dim={result.feature_dim}"
    )
    if result.out_dir:
        line += f" out={result.out_dir}"
    if result.message:
        line += f" | {result.message}"
    print(line)
    return 0  # completed / skipped 均为 0


def _diagnose(args: argparse.Namespace) -> int:
    """扫导出产物，报无效（恒定/重复）特征列。不碰 torch/backbone。"""
    from app.services.inference.offline.export.diagnose import format_report, scan_columns
    from app.services.inference.offline.export.runner import export_root

    root = Path(args.export_root) if args.export_root else export_root()
    print(format_report(scan_columns(root, tag_filter=args.tag)))
    return 0


def _contract(args: argparse.Namespace) -> int:
    """先验契约检查：特征契约声明的目标类别 vs 检测器实际产出的类别名。"""
    from app.services.inference.offline.export.diagnose import check_object_contract

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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inference.offline.export.cli",
        description="离线特征导出：读 FeatureStore 特征 → recipe 转换 → 落模型输入样例。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一次导出")
    run.add_argument("--task-id", type=int, required=True, help="任务 id（存储键）")
    run.add_argument("--step-id", type=int, required=True, help="洗消步骤 id（存储键）")
    run.add_argument(
        "--recipe", required=True,
        help="recipe 函数全限定路径，如 app.services.inference.offline.impl.clean.export_r0",
    )
    run.add_argument(
        "--backbone", default=None,
        help="视觉 backbone 身份；不消费像素的 recipe（如 R0）不传",
    )
    run.add_argument(
        "--device", default="cpu", choices=("cpu", "cuda"),
        help="默认 cpu（禁 GPU，不抢在线资源）",
    )
    run.add_argument(
        "--out-dir", default=None,
        help="产物目录；缺省落 {storage_base_dir}/.offline_exports/{task}/{step}/{recipe}@{backbone}/",
    )
    run.add_argument(
        "--threads", type=int, default=2, help="CPU 线程数（torch.set_num_threads，默认 2）",
    )

    diag = sub.add_parser(
        "diagnose", help="扫导出产物找无效特征列（恒定 / 重复）；≥2 个 step 才能判结构性",
    )
    diag.add_argument("--export-root", default=None, help="导出根目录；缺省用默认位置")
    diag.add_argument("--tag", default=None, help="只看某个 recipe@backbone，如 r0@none")

    con = sub.add_parser(
        "contract", help="先验契约检查：特征契约的目标类别 vs 检测器实际类别名",
    )
    con.add_argument(
        "--objects", default="app.services.inference.offline.impl.clean.OBJECTS",
        help="目标类别清单的全限定路径",
    )
    con.add_argument(
        "--checkpoints", nargs="+", required=True, help="部署中的检测器权重路径",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.command == "run":
        return _run(args)
    if args.command == "diagnose":
        return _diagnose(args)
    if args.command == "contract":
        return _contract(args)
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
