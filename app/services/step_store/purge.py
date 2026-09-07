"""
step 目录的产物清单、活动时间与删除动作。

**本模块只回答「这目录里有什么、最后何时活动」并执行删除，不判断该不该删。**
保留策略（保留天数、扫描周期、哪些目录该扫、删不删的决定）属慢任务异步处理，
在 [persistence/workers/cleanup_worker.py](../../persistence/workers/cleanup_worker.py)。
两侧 docstring 互指，别把策略挪进来——那会让本包从「格式真源」变成「生命周期管理者」。

## 为什么需要一个统一入口

`{base_dir}/{task_id}/{step_id}/` 是**多个服务共用**的落盘单元，但此前两个删除者都以为
自己只在删自己的东西：

- `hls_strategy.purge_step_dir` 的 docstring 逐条列举它删「段 / playlist / metadata /
  init」，唯独没列 `features.jsonl` / `facts.jsonl` / `offline_inference_result.json`
  —— 那些是 inference 写的，但 `rmtree` 一并删掉。
- `cleanup_worker` 按 `metadata.json` 判 TTL，而那是 HLS 独有的产物。

统一入口的价值不在少写几行 `rmtree`，在于**让「这里有谁的东西」成为一份可读的清单**。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from app.services.step_store import layout

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Product:
    """step 目录内一类产物的登记项。

    Attributes:
        kind: 写侧点名用的 key（`Step.product_path(kind, **key)`）。**它是本注册表从
            「文档」升级成「写侧真源」的那一步** —— 未登记的 kind 直接抛，而不是像
            此前那样静默地对 TTL 不可见。
        pattern: 相对 step 目录的 glob，TTL 扫描用。**必须能与临时文件区分开** ——
            读侧会在 step 目录写 `.clip_{nonce}.m3u8` / `.export_{nonce}.m3u8`，崩溃
            残留时若被算作产物，会让目录永远显得"活跃"而躲过 TTL 回收。
        name: 文件名构造函数，签名即该 kind 的命名参数（`segment` 要 track + ts_us，
            `metadata` 无参）。与 `pattern` 是同一命名约定的两个方向。
        writer: 谁写的（模块路径）。**只作文档与排查用途，不是权限** —— 写成员的
            调用者由导入门禁按模块限制（见 tests/test_import_hygiene.py）。
    """

    kind: str
    pattern: str
    name: Callable[..., str]
    writer: str


# step 目录产物注册表。新增落盘产物**必须**在此登记，否则 `product_path` 的 kind 校验
# 失败；此前忘登记不报错，只表现为「目录被提前回收，而那个产物还有人要读」。
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

    对外入口是 `Step.product_path(kind, **key)` / `Step.open_product` —— 路径不出包，
    本函数只出名字。
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
    """枚举 root 下全部已落盘的 `(task_id, step_id)`。

    **出的是 id 不是 Path** —— 服务层不该拿到目录对象，那会把「目录结构长什么样」
    重新漏回去。对外入口是 `store.steps(include_empty=True)`。

    **只认两层数字目录名**，`.lab_exports` 等非任务目录跳过。

    刻意与 `store.steps()` 的默认语义不同：那个按契约丢弃「两轨都没段」的
    step（对回放没意义，不该露给前端），而 TTL 恰恰要看见这类目录 —— 只有
    `features.jsonl` 没有 HLS 段的 step 正是此前泄漏的那一类。
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

    **前导点一律排除**：step 目录里的临时文件全部以 `.` 开头，且其中两类会命中产物
    glob —— `hls_strategy` 转码期的 `.{stem}.tmp_init.mp4` 撞 `*_init.mp4`、
    `.{stem}.tmp_seg_0.mp4` 撞 `*_segment_*.mp4`。光靠 glob 区分不开，故加这道过滤。

    新增临时文件**必须**沿用前导点命名，否则会被算作产物、让死 step 躲过回收。
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

    取**已登记产物的 mtime 最大值**，而不是目录 mtime，也不是 `metadata.json` 的
    `updated_at` 字段：

    - 目录 mtime：会被临时文件的增删刷新，崩溃残留的 `.export_*.m3u8` 能让一个死
      step 永远显得活跃。
    - `metadata.json.updated_at`：那是 **HLS 独有**的产物。只有 `features.jsonl`
      而没有 HLS 段的 step（HLS 未启用，或首段 transcode 失败但推理照常跑）读不到
      它 —— 此前这类目录永远扫不到，无限期堆积。

    活跃 step 每 ~10s 落一个新段，段文件 mtime 随之更新，故不会被误判为过期。
    """
    mtimes: List[float] = []
    for path in _products_in(step_dir):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue  # 竞态：刚被别人删掉，当它不存在
    return max(mtimes) if mtimes else None


def purge_step(step_dir: Path) -> bool:
    """删除该 step 的全部落盘产物，返回是否真的删了（不存在返回 False）。

    ⚠ **删的是整个目录，含所有服务的产物**——不只是调用方自己写的那些。见 `PRODUCTS`：
    HLS 段与 playlist（persistence 写）、`features.jsonl` / `facts.jsonl`（inference 写）、
    离线推理结果，一并消失。

    ⚠ **本函数只执行删除，不判断该不该删。** 保留策略见
    `persistence/workers/cleanup_worker.py`；重启 supersede 的判断见
    `persistence/strategies/hls_strategy.purge_step_dir`。

    ⚠ **调用方必须在本调用返回之后，才能创建本 run 的任何产物。** 这条顺序约束此前
    只活在 [run_control.start_run](../../run_control.py) 的注释里，两侧代码互不知情：
    `persistence.start_run`（rmtree）必须先于 `FeatureStore.open_fresh`（建文件），
    反过来新建的 `features.jsonl` 会被这里的 rmtree 抹掉，且不报错。

    并发控制**不在本函数内**：`hls_strategy` 持它自己的目录锁，锁是并发机制不随删除
    动作搬家。best-effort：删除失败只打 warning 并返回 False，不抛。
    """
    target = step_dir
    if not target.exists():
        return False
    try:
        shutil.rmtree(target)
        return True
    except OSError as e:
        logger.warning("[StepStore] 删除 step 目录失败 %s: %s", target, e)
        return False


def sweep_empty_tasks(root: Path) -> int:
    """回收 step 全被抽走后留下的空 task 目录，返回删除个数。

    调用方不必自己遍历存储根 —— 那是目录结构知识。`rmdir` 对非空目录会安全失败，
    故无需先检查是否为空。
    """
    if not root.exists() or not root.is_dir():
        return 0
    removed = 0
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.isdigit():
            continue
        try:
            task_dir.rmdir()
            removed += 1
            logger.info("[StepStore] 回收空 task 目录: %s", task_dir)
        except OSError:
            pass  # 非空或无权限，跳过
    return removed
