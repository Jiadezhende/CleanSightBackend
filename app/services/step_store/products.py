"""step 目录的产物注册表与目录内容查询：**这目录里有什么、最后何时活动。**

step 目录是**多个服务共用**的落盘单元。本模块把「这里有谁的东西」变成一份可读的清单
（`PRODUCTS`），并在它之上回答两个查询：有哪些产物（`list_products`）、最后何时活动
（`last_activity`）。

**只回答，不做决定**：删除动作在 `store.purge_step`，保留策略在
[persistence/workers/cleanup_worker.py](../../persistence/workers/cleanup_worker.py)。别把
保留天数、扫描周期挪进来 —— 那会让本包从「格式真源」变成「生命周期管理者」。

命名分界（与 `layout` 各持一半，是历史分界，别再扩大）：`layout` 出 HLS 5 类
（segment / init / playlist / sidecar / metadata）的具名函数，本模块的 `PRODUCTS` 直接引用
它们；推理侧 4 类（features / facts / offline_result / visual_roi）的名字以 lambda 内联在
`PRODUCTS` 里。

依赖上界：stdlib + `layout`（L0）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from app.services.step_store import layout

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Product:
    """step 目录内一类产物的登记项。

    Attributes:
        kind: 写侧点名用的 key（`Step.product_path(kind, **key)`）。未登记的 kind 直接抛，
            这是本注册表作为**写侧真源**而非文档的那一步。
        pattern: 相对 step 目录的 glob，TTL 扫描用。**必须能与临时文件区分开** —— 读侧会在
            step 目录写 `.clip_{nonce}.m3u8` / `.export_{nonce}.m3u8`，崩溃残留时若被算作
            产物，会让目录永远显得「活跃」而躲过 TTL 回收。
        name: 文件名构造函数，签名即该 kind 的命名参数（`segment` 要 track + ts_us，
            `metadata` 无参）。与 `pattern` 是同一命名约定的两个方向。
        writer: 谁写的（模块路径）。**只作文档与排查用途，不是权限** —— 写成员的调用者由
            导入门禁按模块限制（见 tests/test_import_hygiene.py）。
    """

    kind: str
    pattern: str
    name: Callable[..., str]
    writer: str


# step 目录产物注册表。新增落盘产物**必须**在此登记，否则 `product_path` 的 kind 校验失败；
# 漏登记的后果是它对 TTL 不可见 ——「目录被提前回收，而那个产物还有人要读」。
PRODUCTS: Tuple[Product, ...] = (
    # --- HLS 视频轨（persistence 写，traceback / lab / step_store 读）---
    Product("segment", "*_segment_*.mp4", layout.segment_name, "persistence/hls_strategy"),
    Product("init", "*_init.mp4", layout.init_name, "persistence/hls_strategy"),
    Product("playlist", "*_playlist.m3u8", layout.playlist_name, "persistence/hls_strategy"),
    Product("sidecar", "*_segment_*.idx", layout.sidecar_name, "persistence/hls_strategy"),
    Product("metadata", layout.METADATA_NAME, lambda: layout.METADATA_NAME, "persistence/hls_strategy"),
    # --- 推理产物（inference 写，offline 读）---
    Product("features", "features.jsonl", lambda: "features.jsonl",
            "inference/feature/store.FeatureStore"),
    Product("facts", "facts.jsonl", lambda: "facts.jsonl",
            "inference/feature/store.FactLedger"),
    Product("offline_result", "offline_inference_result.json",
            lambda: "offline_inference_result.json", "inference/offline/runner"),
    # --- 三期 ROI 视觉特征，尚未落地；先登记以免届时漏掉 TTL 可见性 ---
    Product("visual_roi", "visual_roi_*.npz", lambda name: f"visual_roi_{name}.npz",
            "inference/offline/visual（未落地）"),
)

_BY_KIND: Dict[str, Product] = {p.kind: p for p in PRODUCTS}


def product_name(kind: str, **key) -> str:
    """已登记 kind 的文件名。kind 未登记抛 `KeyError`，命名参数不对抛 `TypeError`。

    只出名字不出路径；对外入口是 `Step.product_path` / `Step.open_product`。
    """
    try:
        product = _BY_KIND[kind]
    except KeyError:
        raise KeyError(
            f"未登记的产物 kind {kind!r}；已登记：{sorted(_BY_KIND)}。"
            f"新增落盘产物必须先进 PRODUCTS，否则它对 TTL 不可见 —— 表现为"
            f"「目录被提前回收，而那个产物还有人要读」。"
        ) from None
    return product.name(**key)


def iter_steps(root: Path) -> Iterator[Tuple[int, int]]:
    """枚举 root 下全部已落盘的 `(task_id, step_id)`。**只认两层数字目录名**，
    `.lab_exports` 等非任务目录跳过。

    **出的是 id 不是 Path** —— 服务层不该拿到目录对象，那会把「目录结构长什么样」漏回去。
    对外入口是 `store.steps(include_empty=True)`。

    刻意与 `store.steps()` 的默认语义不同：那个丢弃「两轨都没段」的 step，而 TTL 恰恰要看见
    这类目录（只有 `features.jsonl` 没有 HLS 段的 step 就属于它）。
    """
    if not root.exists() or not root.is_dir():
        return
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.isdigit():
            continue
        try:
            children = sorted(task_dir.iterdir())
        except OSError as e:
            logger.debug("[StepStore] 无法读取 task 目录 %s: %s", task_dir, e)
            continue
        for child in children:
            if child.is_dir() and child.name.isdigit():
                yield int(task_dir.name), int(child.name)


def _products_in(step_dir: Path) -> List[Path]:
    """该 step 目录内已登记的产物文件（不含临时文件与未登记文件）。

    **前导点一律排除**：step 目录里的临时文件全部以 `.` 开头，且其中两类会命中产物 glob
    （`.{stem}.tmp_init.mp4` 撞 `*_init.mp4`、`.{stem}.tmp_seg_0.mp4` 撞 `*_segment_*.mp4`），
    光靠 glob 区分不开。新增临时文件**必须**沿用前导点命名，否则会被算作产物、让死 step 躲
    过回收。
    """
    found: List[Path] = []
    for product in PRODUCTS:
        try:
            found.extend(
                p
                for p in step_dir.glob(product.pattern)
                if p.is_file() and not p.name.startswith(".")
            )
        except OSError as e:
            logger.debug("[StepStore] glob 失败 %s/%s: %s", step_dir, product.pattern, e)
    return found


def list_products(step_dir: Path) -> List[str]:
    """该 step 已登记产物的**文件名**（不出路径，不含临时文件与未登记文件）。"""
    return [p.name for p in _products_in(step_dir)]


def last_activity(step_dir: Path) -> Optional[float]:
    """该 step 最后一次产出的时刻（unix 秒）；无任何已登记产物时 None。

    取**已登记产物的 mtime 最大值**，而不是这两个看起来更省事的判据：

    - 目录 mtime：会被临时文件的增删刷新，崩溃残留的 `.export_*.m3u8` 能让一个死 step 永远
      显得活跃。
    - `metadata.json.updated_at`：那是 **HLS 独有**的产物。只有 `features.jsonl` 而没有 HLS
      段的 step（HLS 未启用，或首段 transcode 失败但推理照常跑）读不到它，这类目录会永远扫
      不到而无限期堆积。

    活跃 step 每 ~10s 落一个新段，段文件 mtime 随之更新，故不会被误判为过期。
    """
    mtimes: List[float] = []
    for path in _products_in(step_dir):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue  # 竞态：刚被别人删掉，当它不存在
    return max(mtimes) if mtimes else None
