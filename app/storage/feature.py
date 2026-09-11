"""
features 域 —— `{step}/features/features.jsonl` 的定位、编解码与读写。

    {root}/{task_id}/{step_id}/features/
      features.jsonl   每帧一行，多流对齐的检测特征（online 推理写回，常开）
      facts.jsonl      同域产物，但**本模块暂不认识它**，见下方「facts.jsonl 为什么不在这里」

## 本模块管什么、不管什么

**管**：目录在哪、文件叫什么、一行是什么、坏行怎么办。
**不管**（都在 `inference/feature/store.py`，见包 docstring 的边界清单）：

    批缓冲 / batch_size          攒够多少行再落盘是写侧的节流策略
    owner fence / open_fresh     run 身份与 supersede 语义（那把锁罩的是 run 身份，见下）
    best-effort 吞异常            本模块**照抛 OSError**，包成什么由调用方定

**并发：本域尚未按规范 §7.4 C2/C7 加锁——已知缺口，不是漏改。**

别以为 `append_features` 自身是原子的：一批超过缓冲会拆成多次底层 write，Windows 的
`mode="a"` 也不保证追加原子。它现在不出问题，是因为**同一 step 只有一个写者**且
`store.py` 的 `self._lock` 已串行 `_enqueue`/`_write`/`flush`/`close`/`open_fresh`。

真正缺的互斥不在 append 之间，而在 **append 与 `tasks.purge_step` 之间**：TTL 回收从
`cleanup_worker` 发起，它持的是 hls 的目录锁，与本域的写者持的 `store._lock` 是两把不同
的锁，`rmtree` 与 append 之间零互斥。表现是 `features/` 删到一半，或 `_domain_root(
create=True)` 在 rmtree 之后把目录重建出来、留一个已被记账删除的僵尸 step——**都不报错**。

补法是 §7.4 的 per-`(task, step)` 读写锁：本域的写取共享，`purge_step` 取独占。它与
`store.py` 那把锁保护的是两件事（层锁挡 purge 与并发写，store 锁挡跨 run 串台），不是
两把锁护同一个不变式，故 owner fence 照旧留在层外。

## facts.jsonl 为什么不在这里

落盘布局上它属于本域（第 1 期定的三域隔离没变），但代码这一期不迁，原因是**货币定不下来**：

`EventFact` / `SegmentFact` / `fact_from_json` 住在 `app.services.inference.types`，而本层
只许 import `app.storage` / `app.domain` / `app.settings`
（门禁 `test_layer_package_imports_only_whitelisted_app_modules`）。
本模块最多只能收发 `Dict[str, Any]`，与 features 侧收发 `FrameFeature` 不对称——而
`FrameFeature` 之所以能整个进来，只因为它在 `app.domain`。

两条出路都不在本期范围内：把事实类型升格到 `app.domain`（要改一批 import），或接受
dict 这层缝。**在拍板之前，facts.jsonl 的读写整个留在 `store.py`**，本模块一个成员都不为
它开——半迁一份产物比不迁更坏：路径知识会分裂成两处。

`tasks.purge_step` 不受影响，它删的是整个 step 目录，三个域一起没。

依赖上界：`app.domain`（L1，numpy 随 `Detection.mask` 的类型标注进来）+ stdlib。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from app.domain.detection import Detection, FrameDetections, FrameFeature
from app.storage import _root

logger = logging.getLogger(__name__)

# 本域的域名 —— 全文件只出现这一次（样板见 `_root.py`）。
# ⚠ 模块叫 `feature`、域目录叫 `features`，两者刻意不同名：模块名对齐"一域一模块"的
# 命名习惯，域目录名是盘上既成事实。写成 "feature" 会被 `_root.DOMAINS` 白名单当场拦下。
_DOMAIN = "features"

# 产物文件名 —— 内容归各域自己持有，`_root` 对其零知识（见包 docstring 的「布局 vs 内容」）。
_FEATURES_NAME = "features.jsonl"


# 本域在该 step 下的根目录 —— 域内所有路径都经它
def _domain_root(task_id: int, step_id: int, *, create: bool = False) -> Path:
    return _root.path(task_id, step_id, _DOMAIN, create=create)


# ── FrameFeature ↔ 磁盘 record 的对称映射（一对逆运算紧挨放置，往返测试见 T1）─────────
#
# 契约：磁盘 record 是 FrameFeature 的**精简投影**，只保留离线必要信息 = ts + 每源检测框
# (bbox/conf/cls) + 帧分辨率。刻意不落（回读按默认还原）：
#   - mask / keypoints：重（seg/pose 才有，每帧一张数组），且离线不消费；
#   - metadata / extra：离线不消费。
# 磁盘键全命名（frame_width / frame_height），无位置约定；位置约定只活在 Detection 的
# bbox=[x1,y1,x2,y2]（其本身就是坐标序）。
#
# 投影有损是**有意的**，故往返断言只在"投影后的字段"上闭合——mask 传进去、回读是 None，
# 这是契约不是 bug。


def _serialize_detection(det: Detection) -> Dict[str, Any]:
    """单个 Detection → 特征 dict（bbox 即特征；mask/keypoints 太重不落）。"""
    return {
        "bbox": [int(x) for x in det.bbox],  # 强制原生 int（json 不吃 np.int64），与下方同风格
        "conf": float(det.confidence),
        "cls_id": int(det.class_id),
        "cls": det.class_name,
    }


def _deserialize_detection(d: Mapping[str, Any]) -> Detection:
    """特征 dict → Detection（mask/keypoints 未落盘，回读为 None）。"""
    return Detection(
        bbox=d["bbox"],
        confidence=d["conf"],
        class_id=d["cls_id"],
        class_name=d["cls"],
    )


def _feature_to_record(feature: FrameFeature) -> Dict[str, Any]:
    """FrameFeature → 磁盘 record（逆运算 `_record_to_feature`）。"""
    record: Dict[str, Any] = {
        "ts": feature.ts,
        "features": {
            source: [_serialize_detection(d) for d in fd.detections]
            for source, fd in feature.by_source.items()
        },
    }
    if feature.frame_width is not None and feature.frame_height is not None:
        record["frame_width"] = feature.frame_width
        record["frame_height"] = feature.frame_height
    return record


def _record_to_feature(rec: Mapping[str, Any]) -> FrameFeature:
    """磁盘 record → FrameFeature（`_feature_to_record` 的逆；未落字段按契约默认还原）。

    每源 `FrameDetections.timestamp = 记录级 ts`（同帧多流同源同值）；`metadata={}`、
    `success=True`、`mask/keypoints=None` 均为默认。含 detections 为空的 source ——
    "这一帧该流没检出" 与 "这一帧没有该流" 是两回事，present-key 语义必须保住。
    """
    ts = float(rec.get("ts", 0.0))  # 反序列化边界统一 float（手写 JSONL 可能给 int）
    features = rec.get("features") or {}
    by_source = {
        source: FrameDetections(
            detections=[_deserialize_detection(d) for d in dets],
            metadata={},
            timestamp=ts,
        )
        for source, dets in features.items()
    }
    fw = rec.get("frame_width")
    fh = rec.get("frame_height")
    return FrameFeature(
        ts=ts,
        by_source=by_source,
        frame_width=int(fw) if fw is not None else None,
        frame_height=int(fh) if fh is not None else None,
    )


# ── JSONL 行框定 ─────────────────────────────────────────────────────────────────
#
# **错误语义：内容坏了逐行隔离，环境坏了原样抛。**
#
# - 单行解析失败 → 跳过 + warning。JSONL 逐行独立本就是这个格式的性质，一行坏了不该让
#   其余几万帧陪葬；这是格式事实，不是策略。
# - IO 失败（打不开、写不进、建不了目录）→ OSError 原样抛。「落盘失败要不要打断主链路」
#   是调用方的判断：online 写回是 best-effort（吞掉记日志），别的消费方未必。本模块给不出
#   一个对所有调用方都对的答案，所以不给（规范 §7.1 R3「不定义错误语义」）。


def _encode(records: Sequence[Mapping[str, Any]]) -> str:
    """一批 record → 待写文本。**整批先编码完再碰盘**：中途 `TypeError` 时文件一个字节
    没动，不会留下半行——半行会让整个文件从那里起对读侧变成"坏行"。"""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


def _decode(path: Path) -> List[Dict[str, Any]]:
    """读整个 JSONL → record 列表。文件不存在返回 `[]`（"还没写过" 不是错误）。

    `utf-8-sig` 容忍 Windows 手写文件的 UTF-8 BOM，后端自己写出的无 BOM 亦正常解析。
    """
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError as e:  # json.JSONDecodeError 是它的子类
                logger.warning("[storage.feature] 跳过损坏行 %s: %s", path, e)
                continue
            # 合法 JSON 但不是对象（`123` / `[1,2]` 都能解析成功）同样算坏行：本域每行
            # 按契约是一条 record，放行会让 `.get` 在下游炸成 AttributeError。
            if not isinstance(rec, dict):
                logger.warning("[storage.feature] 跳过非对象行 %s: %r", path, rec)
                continue
            records.append(rec)
    return records


# ── features.jsonl：对外三个成员 ──────────────────────────────────────────────────


def append_features(task_id: int, step_id: int, features: Sequence[FrameFeature]) -> None:
    """追加一批帧特征（规范 §7.2 路线 B：一次 `open("a")` + 一次 write）。

    调用方给多少写多少，包内不攒批（W5）。空序列是 no-op 且**不建目录** —— 否则
    `tasks.ids()` 会列出一个从没写过东西的 step。编码早于 `mkdir`，理由同 `_encode`：
    失败时盘上不留任何痕迹。

    Raises:
        OSError: 建目录或写文件失败。是否吞掉由调用方定（online 写回是 best-effort）。
    """
    if not features:
        return
    payload = _encode([_feature_to_record(f) for f in features])
    path = _domain_root(task_id, step_id, create=True) / _FEATURES_NAME
    with path.open("a", encoding="utf-8") as f:
        f.write(payload)


def load_features(task_id: int, step_id: int) -> List[FrameFeature]:
    """回读整段特征，**按 ts 升序**。文件不存在返回 `[]`。

    一次调用一次扫盘（R5）。排序是返回值的契约而非业务判断：文件本身是追加序，绝大多数
    情况已然有序，显式排一次让离线链路的 `bisect` / 滑窗可以直接建立在它上面。

    形状不对的 record（缺 `conf`、`features` 不是对象……）与坏行同等对待：跳过 + warning，
    不中断其余帧。判据同 `_decode` —— 内容坏了逐行隔离，别让一行毁掉整段序列。
    """
    path = _domain_root(task_id, step_id) / _FEATURES_NAME
    frames: List[FrameFeature] = []
    for rec in _decode(path):
        try:
            frames.append(_record_to_feature(rec))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.warning("[storage.feature] 跳过形状不对的 record %s: %s", path, e)
    frames.sort(key=lambda ff: ff.ts)
    return frames


def remove_features(task_id: int, step_id: int) -> bool:
    """删掉 features.jsonl；返回它此前是否存在。

    给写侧的 supersede 用：同 (task, step) 重启一次 run 前清掉旧序列，避免新旧混写。
    **判断"该不该清"不在这里**（那是 run 生命周期，归 `inference` 的 `open_fresh`），
    本函数只执行——与 `tasks.purge_step` 同款分工。

    只删自己这一份产物：同域的 facts.jsonl 与域目录本身都不碰。
    """
    try:
        (_domain_root(task_id, step_id) / _FEATURES_NAME).unlink()
        return True
    except FileNotFoundError:
        return False
