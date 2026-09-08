"""
HLS 持久化策略 —— **只产字节，不管落盘格式**。

产出一段视频的像素与容器：cv2 mp4v 编码 → ffmpeg 转 fMP4 fragment（+ 该轨首段的 init）
→ hex-patch tfdt.baseMediaDecodeTime。**文件叫什么、落在哪、提交顺序、playlist 怎么写，
全在 `step_store.hls.write_segment` 事务里** —— 本模块只往它给的 stage 落点写字节，然后
`commit(duration_s, frame_timestamps)`。

故本模块不 import `step_store` 的任何内部模块，也不知道 `{track}_segment_{ts_us}.mp4`
这个命名的存在。段/init/playlist/sidecar 四件产物的原子提交见该事务的 docstring。

detection 不在此落盘——已由 FeatureStore 按帧 ts 单源写入 features.jsonl。
"""

import logging
import os
import struct
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from app.domain.frame import Frame
from app.services.step_store import hls
from app.services.step_store import store as step_store
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

    # 本类**无实例状态**：段编码帧率全程从帧 ts 反推（见 _effective_fps），不接收任何上游
    # fps；落盘位置与提交顺序全在 `hls.write_segment` 事务里。
    #
    # 此前有一张按 (task_id, step_id) 索引的目录锁表，序列化同一 step 的
    # transcode + playlist append —— 它保护的不变式是「相邻段不能读到相同的累计 EXTINF，
    # 否则 tfdt 碰撞」。**HLS 写侧现在是单线程**（HLSWorkerPool 固定一个 worker），该不变式
    # 自动成立，锁与它的按 task 回收逻辑一并删除。前提被打破时由
    # `hls.HlsConcurrentWrite` 响亮地报出来，不会静默写坏 tfdt。

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

    def purge_step_dir(self, task_id: int, step_id: int) -> bool:
        """重启 supersede：删除该 `(task_id, step_id)` 的整个 step 目录，返回是否删除。

        触发时机由本方法决定（同 (task_id, step_id) 重启一次 run 之前），**删除动作走
        `step_store.purge_step`**——step 目录是多个服务共用的落盘单元，删除入口只此一个。

        ⚠ **删的不只是 HLS 产物**：`features.jsonl` / `facts.jsonl`（inference 写）、
        离线推理结果一并消失。本方法与 `FeatureStore.open_fresh` 对称，两者共同完成
        supersede；**本方法必须先于 open_fresh**，反过来新建的 features.jsonl 会被这里
        抹掉（见 `purge_step` docstring 与 run_control.start_run）。

        为什么 HLS 侧可以整目录删了重建：落盘全靠磁盘文件存在性驱动、无每目录内存态
        （playlist 首行、init 均按 `exists()` 惰性重建），故 rmtree 后由后续首段自然重建。
        否则新段带唯一时间戳文件名不覆盖旧段，只会往同一 playlist 里持续累计。

        正常重启路径此刻已无活跃 worker（stop_run 已 flush 残段并把 CQ 移出 registry），
        故不需要与写路径互斥；HLS 写侧单线程后也没有锁可持了。
        """
        # 删除动作交给 step_store（目录不存在时它返回 False，无需先探一次 exists()）
        return step_store.purge_step(task_id, step_id)

    # 段文件名与 EXTINF 的解析均在 step_store（读写两侧共用一份，不在此另建）

    # ISO/IEC 14496-12 box 容器集合：递归扫描 box 树时只下钻这些类型，
    # 其余 box（含 tfdt、mdhd）按 leaf 处理。
    _BOX_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"moof", b"traf", b"mvex"})

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
    def _transcode_to_fmp4_segment(
        cls, stage_path: Path, init_stage_path: Optional[Path], ts_offset_s: float
    ) -> None:
        """把 cv2 写出的 mp4v 段**原地**转码成 HLS-ready fMP4 fragment，并产出 track 级 init。

        只吃 `hls.write_segment` 给的落点，不知道最终文件叫什么、playlist 在哪：

            stage_path       输入 mp4v，成功则被 fragment 原地替换（提交时再 rename 成正式名）
            init_stage_path  该轨还没有 init 时非 None，把产出的 init 放这（None = 丢弃产出）
            ts_offset_s      本段 tfdt 起点（= 之前所有 EXTINF 之和），由事务在锁外读取


        Pipeline：cv2 mp4v → ffmpeg HLS muxer → {track}_init.mp4（首段）+ fMP4 fragment
        （原地替换）→ hex-patch tfdt.baseMediaDecodeTime 写入累计偏移。

        - 普通 MP4（moov+mdat 整体）无法被 hls.js 在 m3u8 中作为段播放，会 fragParsingError
        - 改用 `-hls_segment_type fmp4` 让 ffmpeg 产出 init segment（ftyp+moov）+
          fragment（ftyp+moof+mdat），符合 HLS 协议要求
        - init 按 track 分开存：raw 与 processed 是两条独立
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

        失败时 stage 里保留 mp4v 原文件并打 warning，不抛异常 —— 主流程可用性优先，该段
        照常提交，只是不是 fMP4。
        """
        # ffmpeg 的 cwd（见下方路径策略）。**这是唯一正当的「拿到目录」方式**：从已定位的
        # 文件路径取 .parent，而不是自己拼「根 + 两级 id」。
        target_dir = stage_path.parent

        # 本方法自己的中间产物，就近派生自 stage 名（它已带前导点，故这些也都以 `.` 开头，
        # 落在段名正则之外，不会被段扫描当成真段）。名字确定而非再要一个随机 nonce：
        # 下面的 `_cleanup_tmp` 要能预清同名残留，且段模板必须含 `%d`。
        stem = stage_path.stem
        tmp_init = target_dir / f"{stem}.tmp_init.mp4"
        # ffmpeg HLS muxer 要求 -hls_segment_filename 必须含 %d 模板（即便只有 1 段），
        # 否则报 "Invalid segment filename template"。pin -start_number 0 让产物固定为 _0.mp4
        tmp_segment_template = target_dir / f"{stem}.tmp_seg_%d.mp4"
        tmp_segment = target_dir / f"{stem}.tmp_seg_0.mp4"
        tmp_playlist = target_dir / f"{stem}.tmp.m3u8"

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
            "-i", str(stage_path),  # 输入保留绝对路径，与 cwd 无关
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
                type(e).__name__, stage_path,
            )
            _cleanup_tmp()
            return

        if result.returncode != 0 or not tmp_segment.exists():
            logger.warning(
                "[HLS] ffmpeg fmp4 transcode failed (rc=%s): %s\nstderr: %s",
                result.returncode, stage_path, result.stderr.strip(),
            )
            _cleanup_tmp()
            return

        # init 交给事务去装：该轨已有 init 时 init_stage_path 是 None，产出物丢弃
        if tmp_init.exists():
            if init_stage_path is not None:
                try:
                    os.replace(tmp_init, init_stage_path)
                except OSError as e:
                    logger.warning(
                        "[HLS] failed to stage init %s: %s", init_stage_path, e
                    )
                    tmp_init.unlink(missing_ok=True)
            else:
                tmp_init.unlink(missing_ok=True)

        # fragment 原地替换 stage 里的 mp4v
        replaced = False
        try:
            os.replace(tmp_segment, stage_path)
            replaced = True
        except OSError as e:
            logger.warning("[HLS] failed to replace segment %s: %s", stage_path, e)
            tmp_segment.unlink(missing_ok=True)

        # 临时 playlist 不再需要（由 persist_segment 自己维护）
        tmp_playlist.unlink(missing_ok=True)

        # hex-patch tfdt：把累计 EXTINF（秒）→ tick 写进 fragment 的 moof/traf/tfdt。
        # timescale 是 pin 死的常量，与 init.mp4 声明的必然一致，无需回读产物。
        # 首段 offset=0，本就正确，跳过。
        if replaced and ts_offset_s > 0.0:
            cls._patch_fragment_tfdt(
                stage_path, int(round(ts_offset_s * _HLS_TIMESCALE))
            )

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
        # 不再使用 client_id（source_ip），因为 step 切洗消台时该字段会被业务侧覆写。
        if segment_type not in ("raw", "processed"):
            raise ValueError(f"Unknown segment type: {segment_type}")
        return self._persist_segment(task_id, step_id, segment_type, frames)

    def _persist_segment(
        self, task_id: int, step_id: int, track: str, frames: List[Frame]
    ) -> bool:
        """落一段视频。**编码是本方法的事，落盘顺序是 `hls.write_segment` 的事。**

        对 track **完全对称**：帧 ts 无条件交出去，哪条轨真的产 sidecar 由布局决定
        （`_layout.SIDECAR_TRACKS`）。此前是两个各 ~95 行、逐行平行的方法。

        eff_fps 逐段反推而非用名义帧率：processed 的实际成帧率随 throttle / 渲染尖峰在窗口
        间漂移（实测 ~11-15fps），固定帧率编码会按 兜底/真实率 倍快放，且逐段速率不同 →
        段间忽快忽慢。raw 同理（解码 CFR 名义 30 但实际会漂）。详见
        docs/update/20260629_PROCESSED_PLAYBACK_RATE_PROPOSAL.md。

        Raises:
            PersistenceError: 编码失败（IOError / cv2.error），可重试
        """
        if not frames:
            logger.warning("%s segment为空: task=%s step=%s", track, task_id, step_id)
            return False

        start_ts = frames[0].timestamp
        eff_fps = self._effective_fps(frames)
        # EXTINF 必须与 fragment 实际媒体时长完全一致：cv2.VideoWriter 用 eff_fps 写 N 帧 →
        # 输出媒体时长 = N/eff_fps，ffmpeg 转 fMP4 保持该时长。故此处必须同用 eff_fps，
        # 用 wall-clock 算会导致 hls.js 段尾 MSE 缓冲洞 + 总时长缩水。
        segment_duration = len(frames) / eff_fps
        height, width = frames[0].frame.shape[:2]

        # cv2 只被本方法用到，故在函数体内导入（规范 §2 通路 2）：写在模块级会让
        # `import app.services.persistence.*` 一律拉起 OpenCV（~250ms）。
        import cv2

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

        with hls.write_segment(task_id, step_id, track, start_ts) as seg:
            out = None
            try:
                out = cv2.VideoWriter(
                    str(seg.stage_path), fourcc, eff_fps, (width, height)
                )
                for fd in frames:
                    out.write(fd.frame)
            except (IOError, cv2.error) as e:
                raise PersistenceError(
                    message=f"Failed to write {track} video segment: {seg.stage_path}",
                    operation=f"hls_write_{track}",
                    retryable=True,
                ) from e
            finally:
                if out is not None:
                    out.release()  # 异常路径也须释放原生编码器句柄

            self._transcode_to_fmp4_segment(
                seg.stage_path, seg.init_stage_path, seg.tfdt_offset_s
            )
            seg.commit(
                duration_s=segment_duration,
                frame_timestamps=[f.timestamp for f in frames],
            )

        logger.info(
            "%s segment已持久化: task_id=%s step_id=%s frames=%d duration=%.3fs",
            track, task_id, step_id, len(frames), segment_duration,
        )
        return True

