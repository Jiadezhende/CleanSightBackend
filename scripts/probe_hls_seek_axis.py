"""探针：ffmpeg 读「只含 step 中间几段」的 m3u8 时，输入时间轴从第几秒开始？

背景：写侧把每个 fMP4 fragment 的 tfdt patch 成「从 step 开头算的累计 EXTINF」
（见 docs/update/20260908_EXTINF_TFDT_CONTRACT.md §3）。clip_builder 造临时 m3u8
时只截取中间几段，于是出现两个互相矛盾的说法：

    fragment 字节里的 tfdt      「我从 T 秒开始」（T = 前面所有段的 EXTINF 之和）
    临时 m3u8 的累计 EXTINF     「第一段从 0 开始」

ffmpeg 信哪个，决定 clip_builder 的 `-ss` 该传相对量还是绝对量。传错的失败是静默的
（输出时长永远对，错的是内容位置）。

本脚本用合成帧走真实写入路径（HLSPersistenceStrategy.persist_segment）造一个 step，
再用 ffprobe 分别探「全量 m3u8」与「只含后半段的 m3u8」的 start_time。

只写临时目录，不连 DB、不发告警、不需要 RTSP。用法：
    .venv/Scripts/python.exe scripts/probe_hls_seek_axis.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 存储根必须在 import app.settings 之前定死 —— settings 是启动即解析的单例
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="hls_seek_probe_"))
os.environ["CLEANSIGHT_STORAGE_DIR"] = str(_TMP_ROOT)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app.domain.frame import Frame  # noqa: E402
from app.services.persistence.strategies.hls_strategy import (  # noqa: E402
    HLSPersistenceStrategy,
)
from app.services.step_store import _layout, _playlist  # noqa: E402
from app.services.step_store import hls  # noqa: E402
from app.settings import settings  # noqa: E402

TASK_ID = 999001
STEP_ID = 1
N_SEGMENTS = 6
FRAMES_PER_SEGMENT = 150
FPS = 15.0
W, H = 320, 240
BASE_TS = 1_700_000_000.0  # 固定起点，便于人工核对段名

# 从第几段开始截子集 m3u8。取 3 → 前面 3 段（各 10s）应累计出 30s 的 tfdt 偏移
SUBSET_FROM = 3


def make_frames(seg_index: int) -> list[Frame]:
    """造一段的合成帧：纯色图（灰度随段号递增）+ 严格等间隔 ts。

    等间隔是有意的 —— 让 eff_fps 精确等于 FPS，EXTINF 精确等于 10.000，
    子集偏移的期望值才是干净的整数，肉眼就能判读 ffprobe 的结果。
    """
    frames = []
    for i in range(FRAMES_PER_SEGMENT):
        n = seg_index * FRAMES_PER_SEGMENT + i
        img = np.full((H, W, 3), (seg_index * 40) % 256, dtype=np.uint8)
        # 画面里放个随帧变化的方块，避免编码器把整段压成一帧
        img[10:40, (i * 2) % (W - 30):(i * 2) % (W - 30) + 30] = 255
        frames.append(Frame(timestamp=BASE_TS + n / FPS, frame=img))
    return frames


def _ffprobe_bin() -> str:
    """与 ffmpeg 同目录的 ffprobe。不能对整条路径做 replace("ffmpeg","ffprobe")
    —— 自包含目录名 `.ffmpeg/` 里也含 "ffmpeg"，会把目录一起改掉。"""
    exe = Path(settings.ffmpeg_path)
    return str(exe.with_name(exe.name.replace("ffmpeg", "ffprobe")))


def ffprobe_format(path: Path) -> dict:
    out = subprocess.run(
        [
            _ffprobe_bin(),
            "-v", "error",
            "-allowed_extensions", "ALL",
            "-show_entries", "format=start_time,duration,nb_streams",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        return {"_error": (out.stderr or "").strip()[-800:]}
    return json.loads(out.stdout).get("format", {})


def ffprobe_first_packet_pts(path: Path) -> str:
    """首个视频包的 pts_time —— start_time 之外的第二个证据。"""
    out = subprocess.run(
        [
            _ffprobe_bin(),
            "-v", "error",
            "-allowed_extensions", "ALL",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time",
            "-read_intervals", "%+#1",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    return (out.stdout or out.stderr or "").strip().splitlines()[:1]


def main() -> int:
    print(f"存储根: {_TMP_ROOT}")
    print(f"ffmpeg: {settings.ffmpeg_path}\n")

    strategy = HLSPersistenceStrategy()
    for k in range(N_SEGMENTS):
        ok = strategy.persist_segment(
            task_id=TASK_ID, step_id=STEP_ID,
            segment_type="raw", frames=make_frames(k),
        )
        print(f"  段 {k}: persist_segment -> {ok}")

    pl_path = hls.playlist_path(TASK_ID, STEP_ID, "raw")
    entries = _playlist.parse_playlist_entries(pl_path)

    print(f"\n落盘 playlist（{len(entries)} 段）:")
    cum = 0.0
    cumulative = []
    for name, dur in entries:
        cumulative.append(cum)
        print(f"  tfdt={cum:7.3f}s  EXTINF={dur:6.3f}  {name}")
        cum += dur

    if len(entries) <= SUBSET_FROM:
        print("段数不足，无法构造子集")
        return 1

    expected_offset = cumulative[SUBSET_FROM]
    print(f"\n子集取第 {SUBSET_FROM}~{len(entries)-1} 段，"
          f"其首段 tfdt = {expected_offset:.3f}s")

    # 全量 m3u8（走成品出口）
    full_m3u8 = step._dir / ".probe_full.m3u8"
    full_m3u8.write_text(
        hls.vod_playlist(TASK_ID, STEP_ID, "raw"), encoding="utf-8"
    )

    # 子集 m3u8：同样的骨架，只是 entries 少了前 SUBSET_FROM 段
    subset = entries[SUBSET_FROM:]
    subset_m3u8 = step._dir / ".probe_subset.m3u8"
    subset_m3u8.write_text(
        _playlist.build_vod_playlist(
            entries=[(n, d) for n, d in subset],
            map_uri=_layout.init_name("raw"),
            target_duration=max(int(round(max(d for _, d in subset))), 1),
        ),
        encoding="utf-8",
    )

    print("\n--- ffprobe ---")
    for label, p in (("全量 (6 段)", full_m3u8), (f"子集 ({len(subset)} 段)", subset_m3u8)):
        fmt = ffprobe_format(p)
        pkt = ffprobe_first_packet_pts(p)
        print(f"{label:16s} start_time={fmt.get('start_time')!s:>10s}  "
              f"duration={fmt.get('duration')!s:>10s}  首包 pts={pkt}")
        if "_error" in fmt:
            print(f"                 ffprobe 报错: {fmt['_error']}")

    print(f"\n判读：start_time ≈ 0 则 ffmpeg 按临时 m3u8 累计 EXTINF 定位；"
          f"≈ {expected_offset:.0f} 则按 fragment 自带 tfdt 定位。")

    # ---------------- 第二阶段：实际裁一个 clip，看内容对不对 ----------------
    # 窗口取「段 3 的后半 + 段 4 的前半」= 墙钟 BASE_TS+35 ~ +45。
    # 每段的合成画面是纯色，灰度 = (段号 × 40) % 256 —— 解出首帧读灰度即知来自哪段。
    win_start_s, win_end_s = 35.0, 45.0
    sel = entries[3:5]                       # 与窗口重叠的段：3、4
    sel_first_tfdt = cumulative[3]           # 30.000
    rel_offset = win_start_s - 30.0          # clip_builder 现在算出来的：5.000
    duration_s = win_end_s - win_start_s

    clip_m3u8 = step._dir / ".probe_clip.m3u8"
    clip_m3u8.write_text(
        _playlist.build_vod_playlist(
            entries=[(n, d) for n, d in sel],
            map_uri=_layout.init_name("raw"),
            target_duration=max(int(round(max(d for _, d in sel))), 1),
            media_sequence=None,
        ),
        encoding="utf-8",
    )

    probe_start = ffprobe_format(clip_m3u8).get("start_time")
    abs_offset = float(probe_start) + rel_offset if probe_start else None

    print(f"\n--- 第二阶段：裁 [{win_start_s:.0f}s, {win_end_s:.0f}s]（墙钟相对 BASE_TS）---")
    print(f"该窗口应落在段 3 后半（灰度 120）+ 段 4 前半（灰度 160），首帧灰度应为 120")
    print(f"子集 m3u8 含段 3~4，其 start_time={probe_start}，首段 tfdt={sel_first_tfdt:.3f}")

    # 第三个变体：段不变、-ss 不变，只把声明的 EXTINF 故意写错（真值 10.000 → 7.000）。
    # 若输出与「现状」一致，说明裁剪位置由 fragment 自身的 tfdt + 样本时长决定，
    # playlist 声明的 EXTINF 对 clip 内容是惰性的。
    bad_m3u8 = step._dir / ".probe_clip_badextinf.m3u8"
    bad_m3u8.write_text(
        _playlist.build_vod_playlist(
            entries=[(n, 7.0) for n, _ in sel],
            map_uri=_layout.init_name("raw"),
            target_duration=8,
            media_sequence=None,
        ),
        encoding="utf-8",
    )

    for label, ss, src in (
        (f"现状 -ss {rel_offset:.3f}（相对量）", rel_offset, clip_m3u8),
        (f"绝对 -ss {abs_offset:.3f}（tfdt+相对）", abs_offset, clip_m3u8),
        (f"EXTINF 写错成 7.000，-ss {rel_offset:.3f}", rel_offset, bad_m3u8),
    ):
        if ss is None:
            continue
        out = step._dir / f".probe_out_{ss:.0f}_{src.stem}.mp4"
        r = subprocess.run(
            [
                settings.ffmpeg_path, "-y", "-loglevel", "error",
                "-allowed_extensions", "ALL",
                "-i", str(src),
                "-ss", f"{ss:.3f}", "-to", f"{ss + duration_s:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(out),
            ],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not out.exists():
            print(f"  {label:34s} → ffmpeg 失败: {(r.stderr or '').strip()[-300:]}")
            continue
        fmt = ffprobe_format(out)
        gray = first_frame_gray(out)
        print(f"  {label:34s} → 时长={fmt.get('duration')}s  首帧灰度={gray}")

    print(f"\n对照：段0=0 段1=40 段2=80 段3=120 段4=160 段5=200")

    # ---------------- 第三阶段：在途段能不能喂进去 ----------------
    # 在途段 = cv2 刚写完 mp4v、transcode 尚未把它换成 fMP4 fragment 的那个窗口。
    # clip_builder 现在用 playable_only=False，会把它选进来。这里手工造一个同形态的
    # 文件（mp4v + 自带 moov），按 clip_builder 的做法写进 m3u8，看 ffmpeg 什么反应。
    import cv2

    inflight_name = f"raw_segment_{int((BASE_TS + 60) * 1e6)}.mp4"
    inflight_path = step._dir / inflight_name
    vw = cv2.VideoWriter(
        str(inflight_path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H)
    )
    for f in make_frames(6):
        vw.write(f.frame)
    vw.release()

    inflight_m3u8 = step._dir / ".probe_inflight.m3u8"
    inflight_m3u8.write_text(
        _playlist.build_vod_playlist(
            entries=[(entries[5][0], entries[5][1]), (inflight_name, 10.0)],
            map_uri=_layout.init_name("raw"),
            target_duration=11,
            media_sequence=None,
        ),
        encoding="utf-8",
    )

    print(f"\n--- 第三阶段：m3u8 含一个在途段（mp4v，未 transcode）---")
    print(f"  在途文件已落盘: {inflight_name} "
          f"({inflight_path.stat().st_size} bytes)")
    fmt = ffprobe_format(inflight_m3u8)
    print(f"  ffprobe: start_time={fmt.get('start_time')} "
          f"duration={fmt.get('duration')}")
    if "_error" in fmt:
        print(f"  ffprobe 报错: {fmt['_error']}")

    out = step._dir / ".probe_inflight_out.mp4"
    r = subprocess.run(
        [
            settings.ffmpeg_path, "-y", "-loglevel", "error",
            "-allowed_extensions", "ALL", "-i", str(inflight_m3u8),
            "-ss", "5.000", "-to", "15.000",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(out),
        ],
        capture_output=True, text=True, timeout=300,
    )
    print(f"  ffmpeg exit={r.returncode}")
    if r.stderr.strip():
        print(f"  stderr: {r.stderr.strip()[-400:]}")
    if out.exists():
        print(f"  产物时长={ffprobe_format(out).get('duration')} "
              f"首帧={first_frame_gray(out)}")
        print(f"  （段5=灰度200，在途段是第6段=灰度240）")
    return 0


def first_frame_gray(path: Path) -> str:
    """解出首帧，报「来自哪段」+「段内第几帧」。

    段号由背景灰度给（= 段号 × 40）；段内帧号由白方块横坐标给
    （make_frames 里 x = (i*2) % (W-30)）—— **只看灰度不够**：`-ss` 完全
    不生效时首帧是该段第 0 帧，灰度与正确结果一模一样。
    """
    r = subprocess.run(
        [
            settings.ffmpeg_path, "-v", "error", "-i", str(path),
            "-vframes", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0 or len(r.stdout) < W * H:
        return f"<解码失败 {(r.stderr or b'').decode('utf-8','replace').strip()[-200:]}>"
    buf = np.frombuffer(r.stdout[: W * H], dtype=np.uint8).reshape((H, W))
    bg = int(buf[150, 5])
    band = buf[12:38, :]                       # 白方块所在行带
    cols = np.where(band.max(axis=0) > 200)[0]  # h264 有损，用阈值而非 ==255
    if len(cols) == 0:
        return f"段{bg // 40}(灰度{bg}) 方块未找到"
    x = int(cols[0])
    return f"段{bg // 40}(灰度{bg}) 方块x={x} → 段内帧≈{x // 2}（{x / 2 / FPS:.2f}s）"


if __name__ == "__main__":
    try:
        code = main()
    finally:
        print(f"\n清理临时存储根: {_TMP_ROOT}")
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    sys.exit(code)
