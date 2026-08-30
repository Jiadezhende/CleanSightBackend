"""
HLS持久化策略

负责：
- 视频段编码（MP4）
- M3U8播放列表生成
- Keypoints JSON序列化
- metadata.json更新
"""

import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2

from app.domain.frame import Frame
from app.settings import settings
from app.utils.exceptions import PersistenceError

logger = logging.getLogger(__name__)

# HLS 段的编码帧率正常由 `_effective_fps` 从帧 ts 反推（完全自适应，不引用任何上游 fps）。
# 以下三个常量定义"无可测速率"的退化判定与兜底，全部具名、不散落在条件里：
#   _EFF_FPS_MIN / _EFF_FPS_MAX —— 反推值的合理带；落带外（乱序/重复 ts 致 span 异常）视为不可信。
#   _DEGENERATE_FALLBACK_FPS   —— 单帧段 / span<=0 / 带外 时的兜底。此时本就无时序信息，
#       取值与上游 fps 无关，只需给退化的单帧段一个合理 EXTINF；故用本地常量而非上游 raw_fps/inference_fps。
_EFF_FPS_MIN = 1.0
_EFF_FPS_MAX = 60.0
_DEGENERATE_FALLBACK_FPS = 15.0

# fMP4 媒体时间基（mdhd.timescale，即 1 秒切成多少 tick），显式 pin 给 ffmpeg。
#
# 必须 pin 的理由：不指定时 ffmpeg 按 `fps 有理数的约分分子 × 2^k`（k 取到 ≥10000）自选
# timescale，而 init.mp4 只由首段生成、被整条 playlist 复用（EXT-X-MAP 声明的就是它的
# 时间基）。逐段 eff_fps 不同 → 逐段 timescale 不同 → 后续 fragment 的 tick 被按首段尺度
# 解读，误差是乘性的：实测 15fps 定 init、14.37fps 段（其自选 timescale=11496）→ 声明
# 10.02s 却被读成 7.60s，单段 2.4s 空洞，hls.js 段尾停摆。
# 注意该自选值对 fps 极不连续——15.0→15360 但 14.37→11496（分子 1437 约不动），fps 抖 4%
# 可致 timescale 差 25%，故「fps 波动不大就没事」不成立。
#
# 取 90000：MPEG-TS/RTP 标准视频时钟，能整除 30/25/24/20/15/12/10 等常见帧率（每帧分别
# 3000/3600/3750/4500/6000/7500/9000 tick，无余数）；非整除帧率下 ffmpeg 按绝对 PTS 取整、
# 增量差分得出，误差有界 ≤ 半 tick（5.6μs）且不累积。
# pin 之后 timescale 与编码 fps 彻底解耦，逐段 eff_fps 才是合法的速率表达。
_HLS_TIMESCALE = 90000


class HLSPersistenceStrategy:
    """HLS持久化策略"""

    def __init__(
        self,
        db_dir: Path,
    ):
        self.db_dir = db_dir
        # HLS 段编码帧率全程从帧 ts 反推（见 _effective_fps），不接收任何上游 fps。
        # 按 target_dir 路径索引的细粒度锁，序列化同一任务目录下的 playlist/metadata 写操作
        self._dir_locks: Dict[str, threading.Lock] = {}
        self._dir_locks_guard = threading.Lock()

    @staticmethod
    def _effective_fps(frames: List[Frame]) -> float:
        """由帧时间戳跨度反推有效编码 fps：`(N-1) / (ts_last - ts_first)`。

        VideoWriter 与 EXTINF 须用同一个返回值，回放才对齐墙钟。raw/processed 段一律走此
        自适应反推、不引用上游 fps。span<=0 / 单帧 / 反推值落在合理带 [1, 60] 外（重复或
        乱序时间戳致 span 异常）时——即无可测速率的退化段——回退 `_DEGENERATE_FALLBACK_FPS`。
        """
        if len(frames) > 1:
            span = frames[-1].timestamp - frames[0].timestamp
            if span > 0:
                eff_fps = (len(frames) - 1) / span
                if _EFF_FPS_MIN <= eff_fps <= _EFF_FPS_MAX:
                    return eff_fps
        return _DEGENERATE_FALLBACK_FPS

    def _get_dir_lock(self, target_dir: Path) -> threading.Lock:
        key = str(target_dir)
        with self._dir_locks_guard:
            if key not in self._dir_locks:
                self._dir_locks[key] = threading.Lock()
            return self._dir_locks[key]

    def release_dir_locks(self, task_id: int) -> int:
        """回收指定 task 所有 step 目录的 dir 锁，返回回收数量（任务拆除时调用）。

        锁 key 形如 `str(db_dir/task_id/step_id)`，按 task 前缀批量剔除。拆除后不会再有
        该 task 的新段入队（CQ 已出 registry、sweeper 扫不到），残段 flush 已在此前入队；
        极少数在途 transcode 若再取锁会经 `_get_dir_lock` 按需重建同一把、不影响串行正确性。
        不回收则 `_dir_locks` 随 (task_id, step_id) 单调增长——长跑内存慢泄漏。
        """
        prefix = str(self.db_dir / str(task_id)) + os.sep
        with self._dir_locks_guard:
            stale = [k for k in self._dir_locks if k.startswith(prefix)]
            for k in stale:
                del self._dir_locks[k]
        return len(stale)

    def purge_step_dir(self, task_id: int, step_id: int) -> bool:
        """重启 supersede：删除 `{db_dir}/{task_id}/{step_id}` 整个 step 目录，返回是否删除。

        与 FeatureStore.open_fresh 对称——同 (task_id, step_id) 重启一次 run 前清空旧 HLS
        产物（段 / *_playlist.m3u8 / metadata.json / {track}_init.mp4），否则新段带唯一
        时间戳文件名不覆盖旧段，只会往同一 playlist 里持续累计。
        HLS 落盘全靠磁盘文件存在性驱动、无每目录内存态（playlist 首行、init 均按 `exists()`
        惰性重建），故 rmtree 后由后续首段自然重建，安全。
        持该目录锁串行化，防极端在途 persist_segment 竞争——正常重启路径此刻已无活跃 worker
        （stop_run 已 flush 残段 + 出 registry + release_dir_locks）。
        """
        target_dir = self.db_dir / str(task_id) / str(step_id)
        if not target_dir.exists():
            return False
        with self._get_dir_lock(target_dir):
            try:
                shutil.rmtree(target_dir)
                return True
            except OSError as e:
                logger.warning("[HLS] purge step dir failed %s: %s", target_dir, e)
                return False

    # 段文件名格式：{track}_segment_{ts_us}.mp4
    _SEGMENT_FNAME_RE = re.compile(r"^(raw|processed)_segment_(\d+)\.mp4$")
    _EXTINF_RE = re.compile(r"^#EXTINF:([0-9.]+),?$")

    # ISO/IEC 14496-12 box 容器集合：递归扫描 box 树时只下钻这些类型，
    # 其余 box（含 tfdt、mdhd）按 leaf 处理。
    _BOX_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"moof", b"traf", b"mvex"})

    @classmethod
    def _ts_offset_seconds(cls, path: Path) -> float:
        """计算当前段相对该 step+track 首段的时间偏移（秒）。

        每个段都由独立的 ffmpeg 进程转码，输入 mp4v 自身从 PTS=0 开始；不补偏移的话
        所有 fMP4 fragment 的 tfdt 都是 0，hls.js 在 VOD 单线连续播放时会停在第一段
        末尾不前进（必须手动 seek 才能恢复）。

        本函数读取同目录下 `{track}_playlist.m3u8` 中**当前段以前**所有 #EXTINF 求和。
        EXTINF 公式是 len(frames)/fps —— 与 cv2.VideoWriter 写出的 fMP4 fragment 媒体
        时长完全一致。这样保证三套时间线对齐：
            tfdt(N) = Σ EXTINF(0..N-1) = fragment 在 MSE 中的实际起点

        本函数在 `_persist_*_segment` 把当前段 append 到 playlist 之前调用，所以
        playlist 此刻只含 0..N-1 段，求和即得本段的 tfdt 起点。首段读不到任何条目，
        返回 0.0。

        ⚠ 不要回退到「文件名 ts_us 差」的算法 —— 那是 wall-clock 抖动值，与 fragment
        媒体时长不一致，会重新引入 hls.js 段尾停摆 / 总时长缩水 bug。
        """
        m = cls._SEGMENT_FNAME_RE.match(path.name)
        if not m:
            return 0.0
        track = m.group(1)
        playlist_path = path.parent / f"{track}_playlist.m3u8"
        if not playlist_path.exists():
            return 0.0
        total = 0.0
        try:
            with playlist_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    em = cls._EXTINF_RE.match(raw.strip())
                    if em:
                        try:
                            total += float(em.group(1))
                        except ValueError:
                            continue
        except OSError as e:
            logger.warning(
                "[HLS] read playlist for ts_offset failed (%s): %s — using 0",
                e, playlist_path,
            )
            return 0.0
        return max(0.0, total)

    @classmethod
    def _iter_boxes(cls, data: bytes, start: int, end: int):
        """遍历 [start, end) 范围内的 ISO BMFF box，逐个 yield (type, body_start, body_end)。

        遇到 `cls._BOX_CONTAINERS` 中的容器盒返回容器本身的位置，调用方自行决定
        是否再次 `_iter_boxes` 下钻。截断或 size 异常时停止。
        """
        i = start
        while i + 8 <= end:
            size = struct.unpack(">I", data[i : i + 4])[0]
            typ = data[i + 4 : i + 8]
            header = 8
            if size == 1:
                if i + 16 > end:
                    return
                size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
                header = 16
            elif size == 0:
                size = end - i  # extends to container end
            if size < header or i + size > end:
                return
            yield typ, i + header, i + size
            i += size

    @classmethod
    def _find_box_path(
        cls, data: bytes, start: int, end: int, path: Tuple[bytes, ...]
    ) -> Optional[Tuple[int, int]]:
        """按 box 类型路径定位最里层 box，返回其 body 范围；找不到返回 None。

        path 形如 (b'moov', b'trak', b'mdia', b'mdhd')。中间节点必须是容器；
        最后一段是 leaf（不再下钻）。
        """
        if not path:
            return start, end
        head, *rest = path
        for typ, body_start, body_end in cls._iter_boxes(data, start, end):
            if typ != head:
                continue
            if not rest:
                return body_start, body_end
            if typ in cls._BOX_CONTAINERS:
                found = cls._find_box_path(data, body_start, body_end, tuple(rest))
                if found is not None:
                    return found
        return None

    @classmethod
    def _patch_fragment_tfdt(cls, fragment_path: Path, base_media_decode_time: int) -> bool:
        """把 fmp4 fragment 的 moof/traf/tfdt.baseMediaDecodeTime 改写成指定值（单位=timescale tick）。

        ffmpeg 8.x 的 HLS muxer 在 `-start_number 0` + fmp4 模式下会强制把 tfdt 清零，
        `-output_ts_offset` / `-itsoffset+-copyts` / `-muxdelay` 均无效。改成转码完直接
        hex-patch tfdt box 是稳妥做法 —— fmp4 box 结构固定，size 不变，纯 metadata 改写。

        约定 tfdt 为 version 1（64-bit），ffmpeg HLS muxer 在 fmp4 输出时一律按 v1 写。
        遇到 v0 / 找不到 tfdt 打 warning 并返回 False，不抛。
        """
        try:
            data = bytearray(fragment_path.read_bytes())
        except OSError as e:
            logger.warning("[HLS] read fragment failed (%s): %s", e, fragment_path)
            return False
        moof = cls._find_box_path(bytes(data), 0, len(data), (b"moof",))
        if moof is None:
            logger.warning("[HLS] moof not found in %s — tfdt patch skipped", fragment_path)
            return False
        traf = cls._find_box_path(bytes(data), moof[0], moof[1], (b"traf",))
        if traf is None:
            logger.warning("[HLS] traf not found in %s — tfdt patch skipped", fragment_path)
            return False
        tfdt = cls._find_box_path(bytes(data), traf[0], traf[1], (b"tfdt",))
        if tfdt is None:
            logger.warning("[HLS] tfdt not found in %s — tfdt patch skipped", fragment_path)
            return False
        body_start, body_end = tfdt
        if body_end - body_start < 4:
            return False
        version = data[body_start]
        if version == 1:
            if body_end - body_start < 4 + 8:
                return False
            struct.pack_into(">Q", data, body_start + 4, base_media_decode_time)
        else:
            if body_end - body_start < 4 + 4:
                return False
            if base_media_decode_time > 0xFFFFFFFF:
                logger.warning(
                    "[HLS] tfdt v0 overflow (%d > 2^32) in %s",
                    base_media_decode_time, fragment_path,
                )
                return False
            struct.pack_into(">I", data, body_start + 4, base_media_decode_time)
        try:
            fragment_path.write_bytes(bytes(data))
        except OSError as e:
            logger.warning("[HLS] write patched fragment failed (%s): %s", e, fragment_path)
            return False
        return True

    @classmethod
    def _transcode_to_fmp4_segment(cls, path: Path, segment_type: str) -> None:
        """将 cv2 写出的 mp4v 段转码为 HLS-ready fMP4 fragment，并写入 track 级 init.mp4。

        Pipeline：cv2 mp4v → ffmpeg HLS muxer → {track}_init.mp4（首段）+ fMP4 fragment
        （原地替换）→ hex-patch tfdt.baseMediaDecodeTime 写入累计偏移。

        - 普通 MP4（moov+mdat 整体）无法被 hls.js 在 m3u8 中作为段播放，会 fragParsingError
        - 改用 `-hls_segment_type fmp4` 让 ffmpeg 产出 init segment（ftyp+moov）+
          fragment（ftyp+moof+mdat），符合 HLS 协议要求
        - init 按 track 分开存 `{segment_type}_init.mp4`：raw 与 processed 是两条独立
          playlist、各有各的 EXT-X-MAP，共用一个文件名会变成「谁先转码谁定」，另一条轨
          就指向别人的 init。每 track 首次写入落盘，已存在则丢弃产出物（同 track 同摄像头、
          同编码参数，SPS/PPS 一致）
        - `-hls_segment_options video_track_timescale=` 把 mdhd.timescale pin 成
          `_HLS_TIMESCALE`（理由见该常量注释）。注意必须走 `-hls_segment_options` 透传给
          内层 mp4 muxer——直接给 hls muxer 传 `-video_track_timescale` 会被静默忽略
        - ffmpeg 子进程 cwd=target_dir + 输出全 basename：ffmpeg 4.x/8.x 对
          `-hls_fmp4_init_filename` 的绝对路径解析行为相反（4.x 拼到 playlist 目录前，
          8.x 拼到进程 cwd），只有「cwd=输出目录 + basename」在两个版本上都对
        - 各 fragment 的 tfdt 必须累计 = 已写入 playlist 的累计 EXTINF（即 Σ len(frames_prev)/fps）。
          但 ffmpeg 8.x HLS muxer + fmp4 在 `-start_number 0` 下会把 tfdt 强制清零，
          `-output_ts_offset` 被丢弃。这里改成转码完直接 hex-patch tfdt baseMediaDecodeTime
          字段。三套时间线（EXTINF / tfdt / fragment 媒体时长）对齐到同一真值 ——
          hls.js 连续播放不卡段尾、总时长不缩水

        失败时保留 mp4v 原文件并打 warning，不抛异常 —— 主流程可用性优先。
        """
        target_dir = path.parent
        init_path = target_dir / f"{segment_type}_init.mp4"
        ts_offset = cls._ts_offset_seconds(path)

        stem = path.stem
        tmp_init = target_dir / f".{stem}.tmp_init.mp4"
        # ffmpeg HLS muxer 要求 -hls_segment_filename 必须含 %d 模板（即便只有 1 段），
        # 否则报 "Invalid segment filename template"。pin -start_number 0 让产物固定为 _0.mp4
        tmp_segment_template = target_dir / f".{stem}.tmp_seg_%d.mp4"
        tmp_segment = target_dir / f".{stem}.tmp_seg_0.mp4"
        tmp_playlist = target_dir / f".{stem}.tmp.m3u8"

        def _cleanup_tmp() -> None:
            for p in (tmp_init, tmp_segment, tmp_playlist):
                p.unlink(missing_ok=True)

        # 预清理可能残留的同名临时文件
        _cleanup_tmp()

        # 路径策略：ffmpeg 子进程 cwd=target_dir，所有输出文件全部传 basename。
        # 历史踩坑：
        #   - ffmpeg 8.x (Windows) 把 `-hls_fmp4_init_filename` 的 basename 解析到
        #     进程 cwd，传绝对路径才对
        #   - ffmpeg 4.x (Ubuntu 22.04) 把绝对路径**当相对路径**拼到 playlist 目录前，
        #     得到 `/dir/foo/dir/foo/.init.mp4` 这种荒诞路径 → ENOENT
        # 两版行为正好相反，唯一兼容写法就是 cwd=target_dir + basename：两边都拼到
        # target_dir。详见 docs/HLS_TIMELINE_PITFALL.md。
        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-loglevel", "error",
            "-i", str(path),  # 输入保留绝对路径，与 cwd 无关
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            # tfdt 偏移不在这里靠 -output_ts_offset 实现（ffmpeg 8.x HLS muxer + fmp4
            # 在 -start_number 0 下会清零 tfdt）—— 改成 transcode 完后 hex-patch tfdt box。
            "-hls_segment_type", "fmp4",
            # 透传给内层 mp4 muxer：pin mdhd.timescale，切断「timescale 随编码 fps 变」
            "-hls_segment_options", f"video_track_timescale={_HLS_TIMESCALE}",
            "-hls_fmp4_init_filename", tmp_init.name,
            "-hls_segment_filename", tmp_segment_template.name,
            "-start_number", "0",
            "-hls_time", "99999",
            "-hls_list_size", "0",
            "-hls_flags", "temp_file",
            "-f", "hls",
            tmp_playlist.name,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(target_dir),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(
                "[HLS] ffmpeg fmp4 transcode skipped (%s): %s — keeping mp4v file",
                type(e).__name__, path,
            )
            _cleanup_tmp()
            return

        if result.returncode != 0 or not tmp_segment.exists():
            logger.warning(
                "[HLS] ffmpeg fmp4 transcode failed (rc=%s): %s\nstderr: %s",
                result.returncode, path, result.stderr.strip(),
            )
            _cleanup_tmp()
            return

        # init 落盘：仅当该 track 尚无 init 时
        if tmp_init.exists():
            if not init_path.exists():
                try:
                    os.replace(tmp_init, init_path)
                except OSError as e:
                    logger.warning(
                        "[HLS] failed to install init %s: %s", init_path, e
                    )
                    tmp_init.unlink(missing_ok=True)
            else:
                tmp_init.unlink(missing_ok=True)

        # fragment 原地替换原 mp4v 段文件
        replaced = False
        try:
            os.replace(tmp_segment, path)
            replaced = True
        except OSError as e:
            logger.warning("[HLS] failed to replace segment %s: %s", path, e)
            tmp_segment.unlink(missing_ok=True)

        # 临时 playlist 不再需要（由 persist_segment 自己维护）
        tmp_playlist.unlink(missing_ok=True)

        # hex-patch tfdt：把累计 EXTINF（秒）→ tick 写进 fragment 的 moof/traf/tfdt。
        # timescale 是 pin 死的常量，与 init.mp4 声明的必然一致，无需回读产物。
        # 首段 offset=0，本就正确，跳过。
        if replaced and ts_offset > 0.0:
            cls._patch_fragment_tfdt(path, int(round(ts_offset * _HLS_TIMESCALE)))

    def persist_segment(
        self, task_id: int, step_id: int, segment_type: str, frames: List[Frame]
    ) -> bool:
        """
        持久化视频段（业务代码：纯净）

        Args:
            task_id: 任务ID
            step_id: 洗消步骤ID（来自 clean_task.current_step 转 int）
            segment_type: "raw" or "processed"
            frames: 帧数据列表

        Returns:
            是否成功

        Raises:
            PersistenceError: 持久化失败
            ValueError: 未知的segment类型
        """
        # 创建目标目录：{base_dir}/{task_id}/{step_id}/
        # 不再使用 client_id（source_ip），因为 step 切洗消台时该字段会被业务侧覆写
        target_dir = self.db_dir / str(task_id) / str(step_id)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise PersistenceError(
                message=f"Failed to create directory: {target_dir}",
                operation="hls_mkdir",
                retryable=True,
            ) from e

        if segment_type == "raw":
            return self._persist_raw_segment(target_dir, frames, task_id, step_id)
        elif segment_type == "processed":
            return self._persist_processed_segment(target_dir, frames, task_id, step_id)
        else:
            raise ValueError(f"Unknown segment type: {segment_type}")

    def _persist_raw_segment(
        self, target_dir: Path, frames: List[Frame], task_id: int, step_id: int
    ) -> bool:
        """
        持久化原始视频段（业务代码：纯净）

        Raises:
            PersistenceError: 持久化失败（IOError, cv2.error等）
        """
        if not frames:
            logger.warning("Raw segment为空: %s", target_dir)
            return False

        start_ts = frames[0].timestamp

        # 1. 生成原始视频段：帧率从帧 ts 反推（与 processed 段同款），无可测速率时退化兜底。
        # 解码 CFR 名义 30，但实际可漂移；用实测 eff_fps 让回放速率贴合真实墙钟。
        eff_fps = self._effective_fps(frames)
        raw_segment_path = target_dir / f"raw_segment_{int(start_ts * 1e6)}.mp4"
        height, width = frames[0].frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

        out_raw = None
        try:
            out_raw = cv2.VideoWriter(
                str(raw_segment_path), fourcc, eff_fps, (width, height)
            )
            for fd in frames:
                out_raw.write(fd.frame)
        except (IOError, cv2.error) as e:
            raise PersistenceError(
                message=f"Failed to write raw video segment: {raw_segment_path}",
                operation="hls_write_raw",
                retryable=True,
            ) from e
        finally:
            if out_raw is not None:
                out_raw.release()  # 异常路径也须释放原生编码器句柄

        # 2. 计算视频段时长：必须与 fMP4 fragment 实际媒体时长完全一致。
        # cv2.VideoWriter 用 eff_fps 写 N 帧 → 输出 mp4v 媒体时长 = N/eff_fps，
        # ffmpeg 转码到 fMP4 保持该时长。故 EXTINF 必须同用 eff_fps（与写入帧率一致），
        # 否则与 fragment 实际时长偏差 → hls.js 段尾 MSE 缓冲洞 → 卡死 + 总时长缩水。
        segment_duration = len(frames) / eff_fps

        # 3 & 4. 持锁完成：transcode（含 ts_offset 读 playlist）+ playlist append + metadata。
        # 三段必须原子，否则相邻段 transcode 会读到相同累计 EXTINF → tfdt 碰撞。
        raw_playlist_path = target_dir / "raw_playlist.m3u8"
        with self._get_dir_lock(target_dir):
            try:
                if not raw_playlist_path.exists():
                    with raw_playlist_path.open("w") as f:
                        f.write(
                            "#EXTM3U\n"
                            "#EXT-X-VERSION:7\n"
                            "#EXT-X-TARGETDURATION:10\n"
                            '#EXT-X-MAP:URI="raw_init.mp4"\n'
                        )
                self._transcode_to_fmp4_segment(raw_segment_path, "raw")
                with raw_playlist_path.open("a") as f:
                    f.write(f"#EXTINF:{segment_duration:.3f},\n{raw_segment_path.name}\n")
            except IOError as e:
                raise PersistenceError(
                    message=f"Failed to update raw playlist: {raw_playlist_path}",
                    operation="hls_update_playlist",
                    retryable=True,
                ) from e

            self._update_metadata(
                target_dir,
                task_id=task_id,
                step_id=step_id,
                segment_type="raw",
                segment_count_delta=1,
                duration_delta=segment_duration,
                timestamp=start_ts,
            )

        self._update_timeline(target_dir, frames=frames, timestamp=start_ts)

        logger.info(
            "Raw segment已持久化: task_id=%s step_id=%s frames=%d duration=%.3fs",
            task_id,
            step_id,
            len(frames),
            segment_duration,
        )
        return True

    def _persist_processed_segment(
        self, target_dir: Path, frames: List[Frame], task_id: int, step_id: int
    ) -> bool:
        """
        持久化处理后视频段（业务代码：纯净）。

        detection 已单源落盘到 FeatureStore（features.jsonl，按帧 ts 对齐），
        此处只写视频段，不再转储任何推理结果，避免重复落盘。

        Raises:
            PersistenceError: 持久化失败（IOError, cv2.error等）
        """
        if not frames:
            logger.warning("Processed segment为空: %s", target_dir)
            return False

        start_ts = frames[0].timestamp

        # 0. 按本段帧时间戳跨度反推有效 fps：processed 实际成帧率随 throttle / 渲染尖峰
        # 在窗口间漂移（~11-15fps），固定名义帧率编码会按 兜底/真实率 倍快放，且
        # 逐段速率不同 → 段间忽快忽慢的抖动。逐段各取自身 eff_fps，VideoWriter 与 EXTINF
        # 同源 → 每段播成 1.0x，对齐墙钟、抖动消失。详见
        # docs/update/20260629_PROCESSED_PLAYBACK_RATE_PROPOSAL.md。
        eff_fps = self._effective_fps(frames)

        # 1. 生成处理后视频段（使用实测有效帧率 eff_fps）
        segment_path = target_dir / f"processed_segment_{int(start_ts * 1e6)}.mp4"
        height, width = frames[0].frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

        out_processed = None
        try:
            out_processed = cv2.VideoWriter(
                str(segment_path), fourcc, eff_fps, (width, height)
            )
            for fd in frames:
                out_processed.write(fd.frame)
        except (IOError, cv2.error) as e:
            raise PersistenceError(
                message=f"Failed to write processed video segment: {segment_path}",
                operation="hls_write_processed",
                retryable=True,
            ) from e
        finally:
            if out_processed is not None:
                out_processed.release()  # 异常路径也须释放原生编码器句柄

        # 2. 计算视频段时长：必须与 fMP4 fragment 实际媒体时长完全一致，故与 VideoWriter
        # 用同一个 eff_fps。详见 _persist_raw_segment 对应注释 —— 用 wall-clock 算会导致
        # hls.js 段尾停摆。
        segment_duration = len(frames) / eff_fps

        # 3 & 4. 持锁完成：transcode（含 ts_offset 读 playlist）+ playlist append + metadata。
        # 三段必须原子，否则相邻段 transcode 会读到相同累计 EXTINF → tfdt 碰撞。
        playlist_path = target_dir / "processed_playlist.m3u8"
        with self._get_dir_lock(target_dir):
            try:
                if not playlist_path.exists():
                    with playlist_path.open("w") as f:
                        f.write(
                            "#EXTM3U\n"
                            "#EXT-X-VERSION:7\n"
                            "#EXT-X-TARGETDURATION:10\n"
                            '#EXT-X-MAP:URI="processed_init.mp4"\n'
                        )
                self._transcode_to_fmp4_segment(segment_path, "processed")
                with playlist_path.open("a") as f:
                    f.write(f"#EXTINF:{segment_duration:.3f},\n{segment_path.name}\n")
            except IOError as e:
                raise PersistenceError(
                    message=f"Failed to update processed playlist: {playlist_path}",
                    operation="hls_update_playlist",
                    retryable=True,
                ) from e

            self._update_metadata(
                target_dir,
                task_id=task_id,
                step_id=step_id,
                segment_type="processed",
                segment_count_delta=1,
                duration_delta=segment_duration,
                timestamp=start_ts,
            )

        logger.info(
            "Processed segment已持久化: task_id=%s step_id=%s frames=%d duration=%.3fs",
            task_id,
            step_id,
            len(frames),
            segment_duration,
        )
        return True

    def _update_metadata(
        self,
        target_dir: Path,
        task_id: int,
        step_id: int,
        segment_type: str,
        segment_count_delta: int,
        duration_delta: float,
        timestamp: float,
    ):
        """更新任务元信息文件（metadata.json）—— 调用方须持有该目录的锁"""
        metadata_path = target_dir / "metadata.json"

        # 读取现有metadata
        if metadata_path.exists():
            with metadata_path.open("r", encoding="utf-8") as f:
                metadata = json.load(f)
        else:
            # 初始化metadata
            metadata = {
                "task_id": task_id,
                "step_id": step_id,
                "start_time": int(timestamp),
                "end_time": None,
                "raw_segments": {
                    "count": 0,
                    "total_duration": 0.0,
                    "first_timestamp": None,
                    "last_timestamp": None,
                },
                "processed_segments": {
                    "count": 0,
                    "total_duration": 0.0,
                    "first_timestamp": None,
                    "last_timestamp": None,
                },
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

        # 更新统计信息
        segment_key = f"{segment_type}_segments"
        metadata[segment_key]["count"] += segment_count_delta
        metadata[segment_key]["total_duration"] += duration_delta

        if metadata[segment_key]["first_timestamp"] is None:
            metadata[segment_key]["first_timestamp"] = timestamp
        metadata[segment_key]["last_timestamp"] = timestamp

        metadata["updated_at"] = datetime.now().isoformat()

        # 写回文件
        with metadata_path.open("w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _update_timeline(
        self,
        target_dir: Path,
        frames: List[Frame],
        timestamp: float,
    ):
        idx_path = target_dir / f"raw_segment_{int(timestamp * 1e6)}.idx"
        tmp = idx_path.with_suffix(".tmp")

        import numpy as np
        timestamps = np.array([frame.timestamp for frame in frames], dtype=np.float64)
        try:
            tmp.unlink(missing_ok=True)
            with open(tmp, "wb") as f:
                timestamps.tofile(f)
            os.replace(tmp, idx_path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise PersistenceError(
                message=f"Failed to update timeline: {idx_path}",
                operation="hls_update_timeline",
                retryable=True,
            ) from e
