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
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from app.models.frame import FrameData
from app.settings import settings
from app.utils.exceptions import PersistenceError

logger = logging.getLogger(__name__)


class HLSPersistenceStrategy:
    """HLS持久化策略"""

    def __init__(
        self,
        db_dir: Path,
        raw_fps: float = 30.0,
        processed_fps: float = 20.0,
        enable_db_write: bool = False,
    ):
        self.db_dir = db_dir
        self.raw_fps = raw_fps
        self.processed_fps = processed_fps
        self.enable_db_write = enable_db_write
        # 按 target_dir 路径索引的细粒度锁，序列化同一任务目录下的 playlist/metadata 写操作
        self._dir_locks: Dict[str, threading.Lock] = {}
        self._dir_locks_guard = threading.Lock()

    def _get_dir_lock(self, target_dir: Path) -> threading.Lock:
        key = str(target_dir)
        with self._dir_locks_guard:
            if key not in self._dir_locks:
                self._dir_locks[key] = threading.Lock()
            return self._dir_locks[key]

    # 段文件名格式：{track}_segment_{ts_us}.mp4
    _SEGMENT_FNAME_RE = re.compile(r"^(raw|processed)_segment_(\d+)\.mp4$")
    _EXTINF_RE = re.compile(r"^#EXTINF:([0-9.]+),?$")

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
    def _transcode_to_fmp4_segment(cls, path: Path) -> None:
        """将 cv2 写出的 mp4v 段转码为 HLS-ready fMP4 fragment，并写入 step 级 init.mp4。

        Pipeline：cv2 mp4v → ffmpeg HLS muxer → init.mp4（首段）+ fMP4 fragment（原地替换）。

        - 普通 MP4（moov+mdat 整体）无法被 hls.js 在 m3u8 中作为段播放，会 fragParsingError
        - 改用 `-hls_segment_type fmp4` 让 ffmpeg 产出 init segment（ftyp+moov）+
          fragment（ftyp+moof+mdat），符合 HLS 协议要求
        - init.mp4 每 step 一份共享：首次写入时落盘到 step 目录，已存在则丢弃产出物
          （同 step 同摄像头、同编码参数，SPS/PPS 一致，可安全复用）
        - 各 fragment 的 tfdt 必须累计：用 `-output_ts_offset` 把当前段的 PTS 起点平移到
          已写入 playlist 的累计 EXTINF（即 Σ len(frames_prev)/fps）。EXTINF 公式与
          fragment 媒体时长保持一致（见 `_persist_*_segment` 的 segment_duration），
          三套时间线对齐 —— hls.js 连续播放不停在段尾、总时长也不会缩水

        失败时保留 mp4v 原文件并打 warning，不抛异常 —— 主流程可用性优先。
        """
        target_dir = path.parent
        init_path = target_dir / "init.mp4"
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

        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-loglevel", "error",
            "-i", str(path),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            "-output_ts_offset", f"{ts_offset:.6f}",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", tmp_init.name,
            "-hls_segment_filename", str(tmp_segment_template),
            "-start_number", "0",
            "-hls_time", "99999",
            "-hls_list_size", "0",
            "-hls_flags", "temp_file",
            "-f", "hls",
            str(tmp_playlist),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
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

        # init.mp4 落盘：仅当 step 目录尚无 init 时
        if tmp_init.exists():
            if not init_path.exists():
                try:
                    os.replace(tmp_init, init_path)
                except OSError as e:
                    logger.warning(
                        "[HLS] failed to install init.mp4 %s: %s", init_path, e
                    )
                    tmp_init.unlink(missing_ok=True)
            else:
                tmp_init.unlink(missing_ok=True)

        # fragment 原地替换原 mp4v 段文件
        try:
            os.replace(tmp_segment, path)
        except OSError as e:
            logger.warning("[HLS] failed to replace segment %s: %s", path, e)
            tmp_segment.unlink(missing_ok=True)

        # 临时 playlist 不再需要（由 persist_segment 自己维护）
        tmp_playlist.unlink(missing_ok=True)

    def persist_segment(
        self, task_id: int, step_id: int, segment_type: str, frames: List[FrameData]
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
        self, target_dir: Path, frames: List[FrameData], task_id: int, step_id: int
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

        # 1. 生成原始视频段（使用原始视频源帧率30fps）
        raw_segment_path = target_dir / f"raw_segment_{int(start_ts * 1e6)}.mp4"
        height, width = frames[0].frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

        try:
            out_raw = cv2.VideoWriter(
                str(raw_segment_path), fourcc, self.raw_fps, (width, height)
            )
            for fd in frames:
                out_raw.write(fd.frame)
            out_raw.release()
        except (IOError, cv2.error) as e:
            raise PersistenceError(
                message=f"Failed to write raw video segment: {raw_segment_path}",
                operation="hls_write_raw",
                retryable=True,
            ) from e

        self._transcode_to_fmp4_segment(raw_segment_path)

        # 2. 计算视频段时长：必须与 fMP4 fragment 实际媒体时长完全一致。
        # cv2.VideoWriter 用固定 fps 写 N 帧 → 输出 mp4v 媒体时长 = N/fps，
        # ffmpeg 转码到 fMP4 保持该时长。EXTINF 用 wall-clock 帧时间戳差会引入抖动，
        # 与 fragment 实际时长偏差 0.5+ 秒 → hls.js 段尾 MSE 缓冲洞 → 卡死 + 总时长缩水。
        segment_duration = len(frames) / self.raw_fps

        # 3 & 4. 持锁更新播放列表和 metadata（防止并发写入竞态）
        raw_playlist_path = target_dir / "raw_playlist.m3u8"
        with self._get_dir_lock(target_dir):
            try:
                if not raw_playlist_path.exists():
                    with raw_playlist_path.open("w") as f:
                        f.write(
                            "#EXTM3U\n"
                            "#EXT-X-VERSION:7\n"
                            "#EXT-X-TARGETDURATION:10\n"
                            '#EXT-X-MAP:URI="init.mp4"\n'
                        )
                with raw_playlist_path.open("a") as f:
                    f.write(f"#EXTINF:{segment_duration:.3f},\n")
                    f.write(f"{raw_segment_path.name}\n")
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

        logger.info(
            "Raw segment已持久化: task_id=%s step_id=%s frames=%d duration=%.3fs",
            task_id,
            step_id,
            len(frames),
            segment_duration,
        )
        return True

    def _persist_processed_segment(
        self, target_dir: Path, frames: List[FrameData], task_id: int, step_id: int
    ) -> bool:
        """
        持久化处理后视频段和keypoints JSON（业务代码：纯净）

        Raises:
            PersistenceError: 持久化失败（IOError, cv2.error等）
        """
        if not frames:
            logger.warning("Processed segment为空: %s", target_dir)
            return False

        start_ts = frames[0].timestamp

        # 1. 生成处理后视频段（使用推理帧率）
        segment_path = target_dir / f"processed_segment_{int(start_ts * 1e6)}.mp4"
        height, width = frames[0].frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

        try:
            out_processed = cv2.VideoWriter(
                str(segment_path), fourcc, self.processed_fps, (width, height)
            )
            for fd in frames:
                out_processed.write(fd.frame)
            out_processed.release()
        except (IOError, cv2.error) as e:
            raise PersistenceError(
                message=f"Failed to write processed video segment: {segment_path}",
                operation="hls_write_processed",
                retryable=True,
            ) from e

        self._transcode_to_fmp4_segment(segment_path)

        # 2. 写keypoints JSON
        keypoints_path = target_dir / f"keypoints_{int(start_ts * 1e6)}.json"
        keypoints_list = []
        for fd in frames:
            kp = fd.keypoints if hasattr(fd, "keypoints") else None
            ir = fd.inference_result if hasattr(fd, "inference_result") else None
            keypoints_list.append(
                {
                    "timestamp": fd.timestamp,
                    "keypoints": self._make_serializable(kp),
                    "inference_result": self._make_serializable(ir),
                }
            )

        try:
            with keypoints_path.open("w", encoding="utf-8") as f:
                json.dump(keypoints_list, f, ensure_ascii=False, indent=2)
        except IOError as e:
            raise PersistenceError(
                message=f"Failed to write keypoints JSON: {keypoints_path}",
                operation="hls_write_keypoints",
                retryable=True,
            ) from e

        # 3. 计算视频段时长：必须与 fMP4 fragment 实际媒体时长完全一致。
        # 详见 _persist_raw_segment 对应注释 —— 用 wall-clock 算会导致 hls.js 段尾停摆。
        segment_duration = len(frames) / self.processed_fps

        # 4 & 5. 持锁更新播放列表和 metadata（防止并发写入竞态）
        playlist_path = target_dir / "processed_playlist.m3u8"
        with self._get_dir_lock(target_dir):
            try:
                if not playlist_path.exists():
                    with playlist_path.open("w") as f:
                        f.write(
                            "#EXTM3U\n"
                            "#EXT-X-VERSION:7\n"
                            "#EXT-X-TARGETDURATION:10\n"
                            '#EXT-X-MAP:URI="init.mp4"\n'
                        )
                with playlist_path.open("a") as f:
                    f.write(f"#EXTINF:{segment_duration:.3f},\n")
                    f.write(f"{segment_path.name}\n")
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

    def _make_serializable(self, obj: Any) -> Any:
        """递归过滤对象，移除不可JSON序列化的内容（从InferenceManager._make_json_serializable迁移）"""
        if obj is None:
            return None
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                # 跳过已知的不可序列化字段
                if key in ("annotated_frame", "processed_frame", "frame"):
                    continue
                result[key] = self._make_serializable(value)
            return result
        elif isinstance(obj, (list, tuple)):
            return [self._make_serializable(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            # numpy 数组转为列表（如果是小数组）
            if obj.size < 100:
                return obj.tolist()
            logger.warning(
                "Dropping large numpy array during keypoints serialization: shape=%s dtype=%s size=%d",
                obj.shape,
                obj.dtype,
                obj.size,
            )
            raise PersistenceError(
                message=f"numpy array too large to serialize (shape={obj.shape}, size={obj.size}); keypoints data discarded",
                operation="hls_serialize_keypoints",
                retryable=False,
            )
        elif isinstance(obj, (np.integer, np.floating)):
            return obj.item()
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        else:
            return str(obj)
