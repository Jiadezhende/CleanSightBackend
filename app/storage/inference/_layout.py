"""本域的布局：域根目录与三份产物的文件名。

    domain_dir(run, create=)          本域在该 run 下的根，域内所有路径都经它
    DETECTIONS_NAME / TEMPORAL_NAME / LABEL_PROBS_NAME

域名 `_DOMAIN` 全文件只出现一次；文件名属「内容」归本域持有，`_root` 对其零知识。

依赖上界：stdlib（规范见 `docs/kb/DESIGN_STORAGE_LAYER.md` §3）。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.run import RunIdentity
from app.storage import _root

# 本域的域名 —— 全文件只出现这一次，写错会被 `_root.DOMAINS` 白名单当场拦下。
_DOMAIN = "inference"

# 产物文件名。
DETECTIONS_NAME = "detections.jsonl"  # 每帧一行，L1 检测结果
TEMPORAL_NAME = "temporal.jsonl"      # 每条一行，L3 时序分析事实
LABEL_PROBS_NAME = "label_probs.npz"  # 离线分割逐帧类别概率，可视化旁路


def domain_dir(run: RunIdentity, *, create: bool = False) -> Path:
    """本域在该 run 下的根目录 —— 域内所有路径都经它。`create` 语义见 `_root.domain_dir`：
    `RunIdentity` 只建域这一级，run 目录不在即 `OSError`。
    """
    return _root.domain_dir(run, _DOMAIN, create=create)

