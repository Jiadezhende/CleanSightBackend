import cv2
import time
import threading
import base64
import json
from pathlib import Path
from collections import deque
import numpy as np
from typing import Optional, Dict, Deque, Tuple, List, Union, Any

from app.models.frame import RawFrame, ProcessedFrame, FrameSegment, HLSSegment
from app.models.task import Task as CleaningTask
from app.services.ai_models import motion, detection
from app.database import get_db
from datetime import datetime
from datetime import datetime

# Type aliases for better readability
FrameTuple = Tuple[float, np.ndarray]  # (timestamp, frame_array)


class ClientQueues:
    """Container for each client's real-time and cache queues (stores numpy arrays + metadata, delays encoding)."""

    def __init__(self, rt_maxlen: int, ca_segment_len: int):
        self.rt_raw: Deque[FrameTuple] = deque(maxlen=rt_maxlen)
        self.rt_processed: Deque[FrameTuple] = deque(maxlen=rt_maxlen)
        self.ca_raw: Deque[FrameTuple] = deque()
        self.ca_processed: Deque[FrameTuple] = deque()
        self.ca_segment_len = ca_segment_len
        self.latest_processed: Optional[FrameTuple] = None
        self.task: Optional[CleaningTask] = None  # 关联的清洗任务


class InferenceManager:
    """按 client_id 管理实时(RT)与缓存(CA)队列，并在后台进行推理与持久化。

    - RT 队列：rt_raw / rt_processed，保存约 1 秒的帧
    - CA 队列：ca_raw / ca_processed，达阈值后落盘为 JSON（开发阶段模拟 HLS/数据库）
    - 兼容原有 API：submit_*/get_result/status/remove_client
    """

    def __init__(self, rt_fps: int = 30, ca_segment_seconds: int = 5, db_dir: Optional[str] = None):
        # 约 1 秒实时缓存长度
        self._rt_maxlen = max(5, int(rt_fps))
        # 缓存段长度（帧数）
        self._ca_segment_len = max(10, int(rt_fps * ca_segment_seconds))
        # 维护各个客户端队列
        self._clients: Dict[str, ClientQueues] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 数据库存储目录（开发阶段使用 JSON 文件）
        base_dir = Path(__file__).parent.parent.parent.resolve()
        self._db_dir = Path(db_dir) if db_dir else base_dir / "database"
        self._db_dir.mkdir(parents=True, exist_ok=True)

    def _get_or_create_client(self, client_id: str) -> ClientQueues:
        client_queues = self._clients.get(client_id)
        if client_queues is None:
            client_queues = ClientQueues(rt_maxlen=self._rt_maxlen, ca_segment_len=self._ca_segment_len)
            self._clients[client_id] = client_queues
        return client_queues

    def submit_frame(self, client_id: str, frame: np.ndarray) -> None:
        """Submit a frame for processing.

        Args:
            client_id: The client identifier.
            frame: The numpy array of the frame.
        """
        now = time.time()
        with self._lock:
            client_queues = self._get_or_create_client(client_id)
            # 推入实时原始队列
            client_queues.rt_raw.append((now, frame))
            # 推入缓存原始队列
            client_queues.ca_raw.append((now, frame))

    def submit_base64(self, client_id: str, base64_frame: str) -> str:
        """Submit a base64 encoded frame for processing.

        Args:
            client_id: The client identifier.
            base64_frame: The base64 encoded frame string.

        Returns:
            "success" on success, or "error: <message>" on failure.
        """
        try:
            frame_data = base64.b64decode(base64_frame)
            np_arr = np.frombuffer(frame_data, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is None:
                return "error: 无效的图像数据"
            self.submit_frame(client_id, img)
            return "success"
        except Exception as e:
            return f"error: {e}"

    def get_result(self, client_id: str, as_model: bool = False) -> Union[None, np.ndarray, ProcessedFrame]:
        """返回最新处理帧。

        as_model=True 时返回 ProcessedFrame Pydantic 对象（含 Base64），否则返回 numpy.ndarray。
        """
        with self._lock:
            client_queues = self._clients.get(client_id)
            if not client_queues:
                return None
            item = client_queues.rt_processed[-1] if client_queues.rt_processed else client_queues.latest_processed
        if item is None:
            return None
        ts, arr = item
        if not as_model:
            return arr
        task_id = client_queues.task.task_id if client_queues.task else None
        return self._create_processed_frame(arr, task_id, client_id, ts)

    def remove_client(self, client_id: str) -> None:
        """Remove a client and its queues.

        Args:
            client_id: The client identifier to remove.
        """
        with self._lock:
            self._clients.pop(client_id, None)

    def status(self) -> Dict[str, Any]:
        """Get the status of all clients and their queues.

        Returns:
            A dict with client count and queue lengths per client.
        """
        with self._lock:
            stats = {client_id: {
                "rt_raw": len(client_queues.rt_raw),
                "rt_processed": len(client_queues.rt_processed),
                "ca_raw": len(client_queues.ca_raw),
                "ca_processed": len(client_queues.ca_processed)
            } for client_id, client_queues in self._clients.items()}
            return {"clients": len(self._clients), "queues": stats}

    def _create_processed_frame(self, frame: np.ndarray, task_id: Optional[int], client_id: str, timestamp: float) -> ProcessedFrame:
        """创建ProcessedFrame对象。"""
        _, buf = cv2.imencode('.jpg', frame)
        b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
        return ProcessedFrame(
            task_id=task_id,
            client_id=client_id,
            raw_timestamp=datetime.fromtimestamp(timestamp),  # 原始帧时间
            processed_frame_b64=b64,
            inference_result={"mock": True}
        )

    def _flush_segment_if_needed(self, client_id: str, client_queues):
        seg_len = client_queues.ca_segment_len
        if len(client_queues.ca_processed) >= seg_len and len(client_queues.ca_raw) >= seg_len:
            # 创建目录
            client_dir = self._db_dir / client_id
            task_id = client_queues.task.task_id if client_queues.task else "no_task"
            task_dir = client_dir / str(task_id)
            hls_dir = task_dir / "hls"
            hls_dir.mkdir(parents=True, exist_ok=True)

            # 收集帧
            frames = []
            timestamps = []
            start_ts = None
            end_ts = None
            take = min(seg_len, len(client_queues.ca_processed), len(client_queues.ca_raw))
            for _ in range(take):
                ts_p, proc = client_queues.ca_processed.popleft()
                ts_r, raw = client_queues.ca_raw.popleft()
                ts = min(ts_p, ts_r)
                frames.append(proc)  # 使用处理后的帧生成视频
                timestamps.append(ts)
                start_ts = ts if start_ts is None else min(start_ts, ts)
                end_ts = ts if end_ts is None else max(end_ts, ts)

            if start_ts is None or end_ts is None:
                return

            # 生成 HLS 段：用 OpenCV 创建 MP4 视频
            segment_path = hls_dir / f"segment_{int(start_ts * 1e6)}.mp4"
            height, width = frames[0].shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # type: ignore
            out = cv2.VideoWriter(str(segment_path), fourcc, 30.0, (width, height))
            for frame in frames:
                out.write(frame)
            out.release()

            # 更新 M3U8 播放列表
            playlist_path = hls_dir / "playlist.m3u8"
            segment_duration = len(frames) / 30.0  # 假设 30 FPS
            with playlist_path.open('a') as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{segment_path.name}\n")
            if not playlist_path.exists() or playlist_path.stat().st_size == 0:
                with playlist_path.open('w') as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")

            # 记录到数据库
            db = next(get_db())
            try:
                hls_segment = HLSSegment(
                    client_id=client_id,
                    task_id=task_id,
                    segment_path=str(segment_path),
                    playlist_path=str(playlist_path),
                    start_ts=datetime.fromtimestamp(start_ts),
                    end_ts=datetime.fromtimestamp(end_ts)
                )
                db.add(hls_segment)
                db.commit()
                print(f"已生成 HLS 段并记录到数据库 for {client_id}: {segment_path}")
            except Exception as e:
                db.rollback()
                print(f"数据库记录失败 for {client_id}: {e}")
            finally:
                db.close()

    def _inference_loop(self):
        print("AI 推理服务已启动（多客户端管理：RT/CA 队列）")
        while not self._stop_event.is_set():
            with self._lock:
                items = list(self._clients.items())

            if not items:
                time.sleep(0.01)
                continue

            for client_id, client_queues in items:
                if self._stop_event.is_set():
                    break

                # 从实时原始队列取一帧进行推理
                with self._lock:
                    item = client_queues.rt_raw.popleft() if client_queues.rt_raw else None

                if item is None:
                    continue

                ts, frame = item
                try:
                    processed, keypoints = detection.detect_keypoints(frame)
                    
                    # 第二层：动作分析，更新任务状态
                    if client_queues.task:
                        motion.analyze_motion(keypoints, client_queues.task)
                        
                except Exception as e:
                    print(f"推理异常 for {client_id}: {e}")
                    continue

                with self._lock:
                    # 写入实时处理队列
                    client_queues.rt_processed.append((ts, processed))
                    # 写入缓存处理队列
                    client_queues.ca_processed.append((ts, processed))
                    # 更新快速获取项
                    client_queues.latest_processed = (ts, processed)

                # 达到阈值时尝试落盘
                try:
                    self._flush_segment_if_needed(client_id, client_queues)
                except Exception as e:
                    print(f"落盘异常 for {client_id}: {e}")

            # 轻微休眠，避免 CPU 占用过高
            time.sleep(0.001)

        print("AI 推理服务已停止（多客户端管理：RT/CA 队列）")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._inference_loop, daemon=True, name="InferenceThread")
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def set_task(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """为客户端设置任务。

        Args:
            client_id: 客户端ID
            task: 任务对象，如果为None则清除任务

        Returns:
            是否成功设置
        """
        with self._lock:
            client_queues = self._get_or_create_client(client_id)
            client_queues.task = task
            return True

    def get_task(self, client_id: str) -> Optional[CleaningTask]:
        """获取客户端的任务。

        Args:
            client_id: 客户端ID

        Returns:
            任务对象或None
        """
        with self._lock:
            client_queues = self._clients.get(client_id)
            return client_queues.task if client_queues else None


# 单例管理器（模块级）
manager = InferenceManager()


def start():
    manager.start()


def stop():
    manager.stop()


def submit_base64_frame(client_id: str, base64_frame: str) -> str:
    return manager.submit_base64(client_id, base64_frame)


def submit_frame(client_id: str, frame: np.ndarray):
    manager.submit_frame(client_id, frame)


def get_result(client_id: str, as_model: bool = False):
    return manager.get_result(client_id, as_model=as_model)


def remove_client(client_id: str):
    manager.remove_client(client_id)


def status():
    return manager.status()


def set_task(client_id: str, task: Optional[CleaningTask]) -> bool:
    """为客户端设置任务。"""
    return manager.set_task(client_id, task)


def get_task(client_id: str) -> Optional[CleaningTask]:
    """获取客户端的任务。"""
    return manager.get_task(client_id)