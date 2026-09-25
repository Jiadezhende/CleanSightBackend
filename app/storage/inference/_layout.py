"""本域的布局：域根目录、三份产物的文件名，以及整域删除。

    domain_dir(task, step, create=)   本域在该 step 下的根，域内所有路径都经它
    DETECTIONS_NAME / TEMPORAL_NAME / DEBUG_NAME
    delete(task, step)                清掉整域（三份产物一起没）

域名 `_DOMAIN` 全文件只出现一次；文件名属「内容」归本域持有，`_root` 对其零知识。
`delete` 住在这里是因为它跨两份产物、只需要 `domain_dir` 一个知识；两个产物模块谁也收不下它。

依赖上界：stdlib（规范见 `docs/kb/DESIGN_STORAGE_LAYER.md` §3）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.storage import _root

logger = logging.getLogger(__name__)

# 本域的域名 —— 全文件只出现这一次，写错会被 `_root.DOMAINS` 白名单当场拦下。
_DOMAIN = "inference"

# 产物文件名。
DETECTIONS_NAME = "detections.jsonl"  # 每帧一行，L1 检测结果
TEMPORAL_NAME = "temporal.jsonl"      # 每条一行，L3 时序分析事实
DEBUG_NAME = "offline_debug.json" # 离线策略逐帧中间量，给人看的


def domain_dir(task_id: int, step_id: int, *, create: bool = False) -> Path:
    """本域在该 step 下的根目录 —— 域内所有路径都经它。"""
    return _root.path(task_id, step_id, _DOMAIN, create=create)


def delete(task_id: int, step_id: int) -> bool:
    """删掉本域在该 step 下的**全部**产物（整个 `{step}/inference/` 目录）。

    Returns:
        该目录此前是否存在。删除失败记 warning 后返回 False——调用场景是「新一代 run 开写前
        清掉上一代」，抛出去只会把一次 run 整个葬掉。

    **三份产物一起没**：detections / facts / 调试产物同去同归。同 (task, step) 重启一次 run 之
    后，旧 facts 是对旧 detections 的分析结果，留着即脏数据。

    **只删本域**：同 step 的 `hls/` 与 `lab/` 一个字节都不碰；域目录本身一起删，下次写侧的
    `create=True` 会重建。**只执行，不判断该不该删**——那是 run 生命周期，归调用方。

    **不加锁**：同一 step 的写与本函数由调用侧串行（规范 §6）。
    """
    root = domain_dir(task_id, step_id)
    if not root.exists():
        return False
    try:
        shutil.rmtree(root)
        return True
    except OSError as e:
        logger.warning("[storage.inference] 删除域目录失败 %s: %s", root, e)
        return False
