"""本域的布局：域根目录、三份产物的文件名，以及整域删除。

    domain_dir(run, create=)          本域在该 run 下的根，域内所有路径都经它
    DETECTIONS_NAME / TEMPORAL_NAME / LABEL_PROBS_NAME
    delete(task, step)                清掉整域（三份产物一起没）

域名 `_DOMAIN` 全文件只出现一次；文件名属「内容」归本域持有，`_root` 对其零知识。
`delete` 住在这里是因为它跨两份产物、只需要 `domain_dir` 一个知识；两个产物模块谁也收不下它。

依赖上界：stdlib（规范见 `docs/kb/DESIGN_STORAGE_LAYER.md` §3）。
"""

from __future__ import annotations

from pathlib import Path

from app.storage import _fs, _root
from app.storage._root import RunKey, legacy_key

# 本域的域名 —— 全文件只出现这一次，写错会被 `_root.DOMAINS` 白名单当场拦下。
_DOMAIN = "inference"

# 产物文件名。
DETECTIONS_NAME = "detections.jsonl"  # 每帧一行，L1 检测结果
TEMPORAL_NAME = "temporal.jsonl"      # 每条一行，L3 时序分析事实
LABEL_PROBS_NAME = "label_probs.npz"  # 离线分割逐帧类别概率，可视化旁路


@legacy_key
def domain_dir(run: RunKey, *, create: bool = False) -> Path:
    """本域在该 run 下的根目录 —— 域内所有路径都经它。`create` 语义见 `_root.domain_dir`：
    `RunIdentity` 只建域这一级，run 目录不在即 `OSError`。
    """
    return _root.domain_dir(run, _DOMAIN, create=create)


def delete(task_id: int, step_id: int) -> bool:
    """删掉本域在该 step 下的**全部**产物（整个 `{step}/inference/` 目录）。

    Returns:
        是否删掉了（`_fs.remove` 为 REMOVED）。不存在或删除失败返回 False，失败只记 warning——
        调用场景是「新一代 run 开写前清掉上一代」，抛出去只会把一次 run 整个葬掉。删除是原子的：
        失败时盘上原样不动。

    **三份产物一起没**：detections / temporal / label_probs 同去同归。同 (task, step) 重启一次 run 之
    后，旧 facts 是对旧 detections 的分析结果，留着即脏数据。

    **只删本域**：同 step 的 `hls/` 与 `lab/` 一个字节都不碰；域目录本身一起删，下次写侧的
    `create=True` 会重建。**只执行，不判断该不该删**——那是 run 生命周期，归调用方。

    **不加锁**：同一 step 的写与本函数由调用侧串行（规范 §6）。
    """
    return _fs.remove(domain_dir(task_id, step_id)) is _fs.Removed.REMOVED
