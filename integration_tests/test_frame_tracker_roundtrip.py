"""
HLS 帧反查（FrameTracker / Timeline）端到端 round-trip 测试

不需要后端服务、RTSP、数据库或推理引擎，只需 FFmpeg：
直接调 `HLSPersistenceStrategy` 走真实写路径落 fMP4 段 + `.idx` sidecar，
再用 `FrameTracker` 按 ts 读回，逐帧比对。

**这是唯一能抓「ts ↔ 像素错配」的手段**：帧内中心色块编码了 frame_id
（三通道 16 阶量化，抗 H.264 有损压缩），读回后解码 id 与期望 gid 逐帧比。
`tests/test_frame_tracker_boundary.py` 用 seam 覆盖了同一套边界数学但不起
ffmpeg，抓不到「解码出来的像素是不是那一帧」——两者互补，都要跑。

用法:
    python integration_tests/test_frame_tracker_roundtrip.py [--task_id 9900002] [--keep]

参数:
    --task_id <int>  测试任务 ID（默认 9900002，避开真实数据）
    --keep           保留生成的 database/{task_id}/ 目录（默认结束即删）

测试项（括号内为修复前的实测表现，见 docs/update/20260901_FRAME_TRACKER_BOUNDARY_FIX.md）:
    T1  全量遍历 ts 位级相等 + 像素 id 逐帧匹配   （修复前 1651/1800，末段整段丢失）
    T2  区间起点落段中部                          （修复前该段被整段跳过）
    T3  区间起点恰为段首帧                        （修复前同样被跳过：ts_us 截断）
    T4  跨段区间帧数精确                          （修复前 221 帧只出 171）
    T5  越界区间返回空
    T6  find 单点像素命中                         （修复前 ValueError）
    T7  find 多点跨段全命中                       （修复前 ValueError）
    T8  find 返回序为 ts 升序（契约，非 bug）
    T9  find 重复 ts 产出两帧
    T10 find 漂移 ts 抛 ValueError（位级精确契约）
    T11 缺 sidecar 只跳过该段、其余照常                （修复前整条迭代中断）
    T12 返回帧可写（下游 cv2 原地操作）           （修复前 np.frombuffer 只读）
    T13 自定义尺寸生效
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, List, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.domain.frame import Frame
from app.services.inference.offline.frame_tracker import FrameTracker, Timeline
from app.services.persistence.strategies.hls_strategy import HLSPersistenceStrategy
from app.settings import settings

# ---------------------------------------------------------------------------
# 测试数据参数
# ---------------------------------------------------------------------------

STEP_ID = 2
FPS = 15.0
FRAMES_PER_SEG = 150          # 与线上一段 10s @15fps 同量级
N_SEG = 12
W, H = 640, 480
BASE_TS = 1786731122.204701   # 固定起点，让 ts 抖动与截断行为可复现
TOTAL = N_SEG * FRAMES_PER_SEG

_rng = np.random.default_rng(42)
# 背景用平滑噪声：纯噪声码率爆炸、纯色又编不出真实的帧间依赖，
# 都会让「解码是否对齐」的结论失真。
_TEXTURE = cv2.GaussianBlur(
    _rng.integers(0, 256, size=(H, W + 256, 3), dtype=np.uint8), (9, 9), 0
)


def make_frame(gid: int) -> np.ndarray:
    """帧内容 = 平移的背景纹理 + 中心色块编码的 frame_id。"""
    img = _TEXTURE[:, gid % 256:gid % 256 + W].copy()
    b = (gid % 16) * 16 + 8
    g = ((gid // 16) % 16) * 16 + 8
    r = ((gid // 256) % 16) * 16 + 8
    img[H // 2 - 60:H // 2 + 60, W // 2 - 60:W // 2 + 60] = (b, g, r)
    return img


def decode_id(img: np.ndarray) -> int:
    """从中心色块解回 frame_id。取内 60×60 求均值，躲开块边缘的压缩振铃。"""
    h, w = img.shape[:2]
    patch = img[h // 2 - 30:h // 2 + 30, w // 2 - 30:w // 2 + 30]
    b, g, r = [int(round((float(patch[:, :, c].mean()) - 8) / 16)) for c in range(3)]
    return (b % 16) + (g % 16) * 16 + (r % 16) * 256


def ts_of(gid: int) -> float:
    """15fps + 确定性抖动 —— 真实采集 ts 不等距，等距会掩盖边界 bug。"""
    return BASE_TS + gid / FPS + 0.004 * np.sin(gid * 1.7)


# ---------------------------------------------------------------------------
# 造数（走真实写路径）
# ---------------------------------------------------------------------------

def seed(task_id: int) -> None:
    base = settings.storage_base_dir
    target = base / str(task_id)
    if target.exists():
        shutil.rmtree(target)

    strategy = HLSPersistenceStrategy(base)
    t0 = time.perf_counter()
    for s in range(N_SEG):
        gids = range(s * FRAMES_PER_SEG, (s + 1) * FRAMES_PER_SEG)
        strategy.persist_segment(
            task_id, STEP_ID, "raw",
            [Frame(timestamp=ts_of(g), frame=make_frame(g)) for g in gids],
        )
    dt = time.perf_counter() - t0

    d = target / str(STEP_ID)
    mp4s = sorted(d.glob("raw_segment_*.mp4"))
    idxs = sorted(d.glob("raw_segment_*.idx"))
    size = sum(p.stat().st_size for p in mp4s)
    print(f"造数：{len(mp4s)} 段 / {len(idxs)} sidecar / {TOTAL} 帧，"
          f"{dt:.1f}s，视频 {size / 1e6:.1f}MB，索引 {sum(p.stat().st_size for p in idxs) / 1e3:.1f}KB")
    assert len(mp4s) == N_SEG and len(idxs) == N_SEG, "写路径没产出预期数量的段/sidecar"

    # 前置断言：sidecar 必须位级等于写入的 ts，否则后面所有对齐结论都无意义
    for s, p in enumerate(idxs):
        arr = np.fromfile(p, dtype=np.float64)
        exp = np.array([ts_of(s * FRAMES_PER_SEG + i) for i in range(FRAMES_PER_SEG)])
        assert np.array_equal(arr, exp), f"sidecar {p.name} 与源 ts 不位级一致"
    print("前置：sidecar 与源 ts 位级一致 ✅\n")


# ---------------------------------------------------------------------------
# 测试项
# ---------------------------------------------------------------------------

def build_checks(task_id: int) -> List[Tuple[str, Callable[[], str]]]:
    def tl() -> Timeline:
        return Timeline(task_id, STEP_ID)

    def tracker() -> FrameTracker:
        return FrameTracker(task_id, STEP_ID)

    def t1_full() -> str:
        frames = list(tl().iter())
        assert len(frames) == TOTAL, f"帧数 {len(frames)} != {TOTAL}"
        got_ts = [f.timestamp for f in frames]
        assert got_ts == [ts_of(g) for g in range(TOTAL)], "ts 序列与 sidecar 不位级一致"
        mism = [(g, decode_id(f.frame)) for g, f in enumerate(frames)
                if decode_id(f.frame) != g]
        assert not mism, f"像素-ts 错配 {len(mism)} 帧，首例 {mism[:3]}"
        return f"{TOTAL} 帧：ts 位级相等 + 像素 id 逐帧匹配"

    def t2_mid_start() -> str:
        g = FRAMES_PER_SEG + 70
        got = [f.timestamp for f in tl().iter(ts_of(g), ts_of(g + 20))]
        assert got == [ts_of(k) for k in range(g, g + 21)], f"实得 {len(got)}/21 帧"
        return "21 帧精确"

    def t3_exact_seg_start() -> str:
        g = 5 * FRAMES_PER_SEG
        got = [f.timestamp for f in tl().iter(ts_of(g), ts_of(g + 5))]
        assert got == [ts_of(k) for k in range(g, g + 6)], f"实得 {len(got)}/6 帧"
        return "段首帧未被截断的 ts_us 挤掉"

    def t4_cross_seg() -> str:
        g0, g1 = 3 * FRAMES_PER_SEG + 100, 5 * FRAMES_PER_SEG + 20
        got = [f.timestamp for f in tl().iter(ts_of(g0), ts_of(g1))]
        assert got == [ts_of(k) for k in range(g0, g1 + 1)], f"实得 {len(got)}/{g1-g0+1} 帧"
        return f"跨 3 段 {g1 - g0 + 1} 帧"

    def t5_out_of_range() -> str:
        far = ts_of(TOTAL - 1) + 100
        assert list(tl().iter(far, far + 10)) == [], "越界区间应为空"
        assert list(tl().iter(BASE_TS - 100, BASE_TS - 50)) == [], "早于首段的区间应为空"
        return "越界（两侧）均返回空"

    def t6_find_single() -> str:
        g = 2 * FRAMES_PER_SEG + 33
        frames = list(tracker().find([ts_of(g)], W, H))
        assert len(frames) == 1, f"应得 1 帧，实得 {len(frames)}"
        assert frames[0].timestamp == ts_of(g), "ts 不匹配"
        assert decode_id(frames[0].frame) == g, "像素 id 不匹配 —— 取到了别的帧"
        return "单点反查像素命中"

    def t7_find_multi() -> str:
        gids = [10, 200, 455, 900, 1333, 1799]
        frames = list(tracker().find([ts_of(g) for g in gids], W, H))
        assert [decode_id(f.frame) for f in frames] == gids, "像素 id 不匹配"
        return f"{len(gids)} 点跨段全命中"

    def t8_find_order() -> str:
        gids = [900, 10, 1333]
        ids = [decode_id(f.frame) for f in tracker().find([ts_of(g) for g in gids], W, H)]
        assert ids == sorted(gids), f"应按 ts 升序产出，实得 {ids}"
        return "按 ts 升序（非入参序）—— 调用方须按 frame.timestamp 对号入座"

    def t9_find_duplicate() -> str:
        g = 500
        frames = list(tracker().find([ts_of(g), ts_of(g)], W, H))
        assert len(frames) == 2, f"重复 ts 应得 2 帧，实得 {len(frames)}"
        assert all(decode_id(f.frame) == g for f in frames), "像素 id 不匹配"
        return "重数各产出一帧"

    def t10_find_drift() -> str:
        for eps in (1e-6, 0.02):
            try:
                list(tracker().find([ts_of(300) + eps], W, H))
            except ValueError:
                continue
            raise AssertionError(f"漂移 {eps}s 的 ts 应抛 ValueError，实际静默通过")
        return "位级精确：漂移 1µs 即报错，不做近似匹配"

    def t11_missing_sidecar() -> str:
        d = settings.storage_base_dir / str(task_id) / str(STEP_ID)
        victim = sorted(d.glob("raw_segment_*.idx"))[6]
        bak = victim.with_suffix(".idx.bak")
        victim.rename(bak)
        try:
            got = [f.timestamp for f in tl().iter()]
        finally:
            bak.rename(victim)
        exp = [ts_of(g) for g in range(TOTAL)
               if not (6 * FRAMES_PER_SEG <= g < 7 * FRAMES_PER_SEG)]
        assert got == exp, f"应剩 {len(exp)} 帧，实得 {len(got)}（缺一段索引不该打断整条迭代）"
        return f"仅丢该段 {FRAMES_PER_SEG} 帧，其余 {len(exp)} 帧照常"

    def t12_writeable() -> str:
        f = next(tl().iter(ts_of(0), ts_of(0)))
        assert f.frame.flags.writeable, "返回的 ndarray 只读，下游 cv2 原地操作会抛错"
        cv2.rectangle(f.frame, (0, 0), (9, 9), (0, 0, 255), -1)  # 真做一次原地写
        return "可写，cv2 原地绘制通过"

    def t13_scale() -> str:
        f = next(tl().iter(ts_of(0), ts_of(0), width=320, height=320))
        assert f.frame.shape == (320, 320, 3), f"shape={f.frame.shape}"
        return "scale=320:320 生效（不保持宽高比，调用方自负）"

    return [
        ("T1  全量遍历 ts/像素对齐", t1_full),
        ("T2  区间起点落段中部", t2_mid_start),
        ("T3  区间起点恰为段首帧", t3_exact_seg_start),
        ("T4  跨段区间", t4_cross_seg),
        ("T5  越界区间", t5_out_of_range),
        ("T6  find 单点", t6_find_single),
        ("T7  find 多点跨段", t7_find_multi),
        ("T8  find 返回序契约", t8_find_order),
        ("T9  find 重复 ts", t9_find_duplicate),
        ("T10 find ts 漂移", t10_find_drift),
        ("T11 缺 sidecar 降级", t11_missing_sidecar),
        ("T12 返回帧可写性", t12_writeable),
        ("T13 自定义尺寸", t13_scale),
    ]


def run(task_id: int) -> bool:
    results = []
    for name, fn in build_checks(task_id):
        try:
            results.append(("✅", name, fn() or ""))
        except AssertionError as e:
            results.append(("❌", name, str(e)))
        except Exception as e:  # noqa: BLE001
            results.append(("💥", name, f"{type(e).__name__}: {e}"))

    print("=" * 88)
    for mark, name, detail in results:
        print(f"{mark} {name:26s} {detail}")
    print("=" * 88)
    passed = sum(1 for r in results if r[0] == "✅")
    print(f"PASS {passed} / {len(results)}")
    return passed == len(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="FrameTracker 端到端 round-trip 测试")
    parser.add_argument("--task_id", type=int, default=9900002,
                        help="测试任务 ID（默认 9900002，避开真实数据）")
    parser.add_argument("--keep", action="store_true",
                        help="保留生成的 database/{task_id}/ 目录")
    args = parser.parse_args()

    target = settings.storage_base_dir / str(args.task_id)
    try:
        seed(args.task_id)
        ok = run(args.task_id)
    finally:
        # 不留残迹：SegmentFinder.list_task_ids 会把它当成真实任务列出来
        if not args.keep and target.exists():
            shutil.rmtree(target)
            print(f"已清理 {target}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
