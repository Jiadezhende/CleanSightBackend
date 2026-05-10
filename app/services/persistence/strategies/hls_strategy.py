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

    @staticmethod
    def _transcode_to_browser_mp4(path: Path) -> None:
        """将 cv2 写出的 mp4v 段转码为浏览器兼容的 H.264 + faststart。

        cv2.VideoWriter 用 mp4v (MPEG-4 Part 2) 写出的文件浏览器 <video> 标签
        无法稳定播放，且 moov atom 在文件尾部。本函数原地替换为 H.264 + faststart。

        失败时保留原文件并打 warning，不抛异常 —— 主流程的可用性优先于浏览器可播放性。
        """
        tmp_path = path.with_suffix(path.suffix + ".transcode.tmp")
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
            "-movflags", "+faststart",
            "-f", "mp4",
            str(tmp_path),
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
                "[HLS] ffmpeg transcode skipped (%s): %s — keeping mp4v file",
                type(e).__name__, path,
            )
            tmp_path.unlink(missing_ok=True)
            return

        if result.returncode != 0 or not tmp_path.exists():
            logger.warning(
                "[HLS] ffmpeg transcode failed (rc=%s): %s\nstderr: %s",
                result.returncode, path, result.stderr.strip(),
            )
            tmp_path.unlink(missing_ok=True)
            return

        try:
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning("[HLS] failed to replace transcoded file %s: %s", path, e)
            tmp_path.unlink(missing_ok=True)

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

        self._transcode_to_browser_mp4(raw_segment_path)

        # 2. 计算视频段时长（使用实际时间戳计算时长，而非帧数/fps）
        if len(frames) > 1:
            actual_duration = frames[-1].timestamp - frames[0].timestamp
            segment_duration = actual_duration + (1.0 / self.raw_fps)
        else:
            segment_duration = 1.0 / self.raw_fps

        # 3 & 4. 持锁更新播放列表和 metadata（防止并发写入竞态）
        raw_playlist_path = target_dir / "raw_playlist.m3u8"
        with self._get_dir_lock(target_dir):
            try:
                if not raw_playlist_path.exists():
                    with raw_playlist_path.open("w") as f:
                        f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
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

        self._transcode_to_browser_mp4(segment_path)

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

        # 3. 计算视频段时长（使用实际时间戳计算时长，而非帧数/fps）
        if len(frames) > 1:
            actual_duration = frames[-1].timestamp - frames[0].timestamp
            segment_duration = actual_duration + (1.0 / self.processed_fps)
        else:
            segment_duration = 1.0 / self.processed_fps

        # 4 & 5. 持锁更新播放列表和 metadata（防止并发写入竞态）
        playlist_path = target_dir / "processed_playlist.m3u8"
        with self._get_dir_lock(target_dir):
            try:
                if not playlist_path.exists():
                    with playlist_path.open("w") as f:
                        f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
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
