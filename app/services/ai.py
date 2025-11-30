import json
import cv2
import time
import threading
import base64
from pathlib import Path
from collections import deque
import numpy as np
from typing import Optional, Dict, Deque, Tuple, Union, Any, Callable, List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future
from abc import ABC, abstractmethod

from app.models.frame import RawFrame, ProcessedFrame, FrameSegment, HLSSegment, FrameData
from app.models.task import Task as CleaningTask
from app.services.ai_models import motion, detection
from app.database import get_db
from app.config import settings

# Type aliases for better readability
FrameTuple = Tuple[float, np.ndarray]  # (timestamp, frame_array)
InferenceResult = Dict[str, Any]  # 推理结果类型


class InferenceTask(ABC):
    """推理任务基类，所有推理任务都应继承此类。
    
    每个任务都是独立的，可以并行执行。
    """
    
    def __init__(self, name: str, enabled: bool = True):
        """
        Args:
            name: 任务名称
            enabled: 是否启用此任务
        """
        self.name = name
        self.enabled = enabled
    
    @abstractmethod
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行推理任务。
        
        Args:
            frame: 输入帧
            context: 上下文信息，包含其他任务的结果、任务对象等
            
        Returns:
            推理结果字典
        """
        pass
    
    @abstractmethod
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """在帧上可视化推理结果。
        
        Args:
            frame: 输入帧
            result: 推理结果
            
        Returns:
            可视化后的帧
        """
        pass
    
    def requires_context(self) -> List[str]:
        """返回此任务依赖的其他任务名称列表。
        
        Returns:
            依赖的任务名称列表，空列表表示无依赖
        """
        return []


class DetectionTask(InferenceTask):
    """关键点检测任务"""
    
    def __init__(self):
        super().__init__(name="detection", enabled=True)
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行检测推理"""
        try:
            # 调用检测模型
            processed_frame, keypoints = detection.detect_keypoints(frame)
            return {
                "success": True,
                "processed_frame": processed_frame,
                "keypoints": keypoints
            }
        except Exception as e:
            print(f"Detection task error: {e}")
            return {
                "success": False,
                "error": str(e),
                "processed_frame": frame.copy(),
                "keypoints": {}
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化检测结果（检测模型已经画好了框）"""
        return result.get("processed_frame", frame)


class MotionTask(InferenceTask):
    """动作分析任务（依赖检测结果）"""
    
    def __init__(self):
        super().__init__(name="motion", enabled=True)
    
    def requires_context(self) -> List[str]:
        """依赖检测任务的结果"""
        return ["detection"]
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行动作分析"""
        try:
            # 获取检测结果
            detection_result = context.get("results", {}).get("detection", {})
            keypoints = detection_result.get("keypoints", {})
            
            # 获取任务对象
            task = context.get("task")
            
            if not task or not keypoints:
                return {
                    "success": False,
                    "error": "Missing task or keypoints",
                    "actions": {}
                }
            
            # 调用动作分析模型
            actions = motion.analyze_motion(keypoints, task)
            
            return {
                "success": True,
                "actions": actions
            }
        except Exception as e:
            print(f"Motion task error: {e}")
            return {
                "success": False,
                "error": str(e),
                "actions": {}
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化动作分析结果"""
        if not result.get("success"):
            return frame
        
        result_frame = frame.copy()
        actions = result.get("actions", {})
        y_offset = 100  # 避免与检测结果重叠
        
        if actions.get("bending_detected"):
            cv2.putText(result_frame, "Bending Detected!", (10, y_offset),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            y_offset += 25
        
        if actions.get("bubble_detected"):
            cv2.putText(result_frame, "Bubble Detected!", (10, y_offset),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            y_offset += 25
        
        status = actions.get("submersion_status", "unknown")
        if status != "unknown":
            cv2.putText(result_frame, f"Status: {status}", (10, y_offset),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        return result_frame


class TaskRegistry:
    """任务注册表，管理所有推理任务"""
    
    def __init__(self):
        self._tasks: Dict[str, InferenceTask] = {}
        self._execution_order: List[str] = []
    
    def register(self, task: InferenceTask):
        """注册一个推理任务"""
        self._tasks[task.name] = task
        self._recompute_execution_order()
    
    def unregister(self, task_name: str):
        """注销一个推理任务"""
        if task_name in self._tasks:
            del self._tasks[task_name]
            self._recompute_execution_order()
    
    def get_task(self, name: str) -> Optional[InferenceTask]:
        """获取指定任务"""
        return self._tasks.get(name)
    
    def get_enabled_tasks(self) -> List[InferenceTask]:
        """获取所有启用的任务，按执行顺序"""
        return [self._tasks[name] for name in self._execution_order 
                if self._tasks[name].enabled]
    
    def _recompute_execution_order(self):
        """重新计算任务执行顺序（拓扑排序）"""
        # 简单实现：先执行无依赖的，再执行有依赖的
        independent = []
        dependent = []
        
        for name, task in self._tasks.items():
            if not task.requires_context():
                independent.append(name)
            else:
                dependent.append(name)
        
        # TODO: 实现完整的拓扑排序以支持复杂依赖关系
        self._execution_order = independent + dependent
from datetime import datetime


class ClientQueues:
    """容器，管理每个客户端的队列。
    
    架构说明：
    - CA-ReadyQueue: 从 RTMP 提取的原始帧，等待推理（弹出后进入推理+落盘）
    - CA-RawQueue: 原始帧副本，用于生成原始视频 HLS 段
    - CA-ProcessedQueue: 推理后的处理帧（含关键点），用于生成处理后 HLS 段
    - RT-ProcessedQueue: 实时推理结果（含关键点），用于 WebSocket 推送
    """

    def __init__(self, rt_maxlen: int, ca_segment_len: int):
        # CA-ReadyQueue: 等待推理的原始帧（无最大长度限制）
        self.ca_ready: Deque[FrameData] = deque()
        # CA-RawQueue: 原始帧副本，用于落盘生成原始视频（无最大长度限制）
        self.ca_raw: Deque[FrameData] = deque()
        # CA-ProcessedQueue: 处理后的帧，用于生成 HLS（无最大长度限制）
        self.ca_processed: Deque[FrameData] = deque()
        # RT-ProcessedQueue: 实时推理结果，约 1 秒缓存用于 WebSocket 推送
        self.rt_processed: Deque[FrameData] = deque(maxlen=rt_maxlen)
        self.ca_segment_len = ca_segment_len
        self.latest_processed: Optional[FrameData] = None  # 快速访问最新处理帧
        self.task: Optional[CleaningTask] = None  # 关联的清洗任务
        self.rtmp_url: Optional[str] = None  # RTMP 流地址


class InferenceManager:
    """按 client_id 管理实时(RT)与缓存(CA)队列，并在后台进行推理与持久化。

    - RT 队列：保存约 1 秒的帧
    - CA 队列：达阈值后落盘为 JSON/HLS 视频段
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
        
        # 任务注册表
        self._task_registry = TaskRegistry()
        self._register_default_tasks()
        
        # 线程池用于并行推理
        self._executor = ThreadPoolExecutor(max_workers=4)

        # 数据库存储目录（开发阶段使用 JSON 文件）
        base_dir = Path(__file__).parent.parent.parent.resolve()
        self._db_dir = Path(db_dir) if db_dir else base_dir / "database"
        self._db_dir.mkdir(parents=True, exist_ok=True)
    
    def _register_default_tasks(self):
        """注册默认的推理任务"""
        self._task_registry.register(DetectionTask())
        self._task_registry.register(MotionTask())
        
        # 注册内镜弯折检测任务
        try:
            from app.services.ai_models.yolo_task import EndoscopeBendingDetectionTask
            bending_task = EndoscopeBendingDetectionTask(
                model_path=settings.yolo_model_path,
                conf_threshold=settings.yolo_conf_threshold,
                iou_threshold=settings.yolo_iou_threshold,
                enabled=True  # 默认启用，可通过 enable_task 控制
            )
            self._task_registry.register(bending_task)
            print(f"内镜弯折检测任务已注册 (模型: {settings.yolo_model_path})")
        except Exception as e:
            print(f"内镜弯折检测任务注册失败 (可能未安装 ultralytics): {e}")
        
        # 未来可以在这里添加更多任务
        # self._task_registry.register(BubbleDetectionTask())
        # self._task_registry.register(CleanlinessTask())
    
    def register_task(self, task: InferenceTask):
        """动态注册新的推理任务（用于扩展）"""
        self._task_registry.register(task)
    
    def enable_task(self, task_name: str, enabled: bool = True):
        """启用或禁用特定任务"""
        task = self._task_registry.get_task(task_name)
        if task:
            task.enabled = enabled

    def _get_or_create_client(self, client_id: str) -> ClientQueues:
        client_queues = self._clients.get(client_id)
        if client_queues is None:
            client_queues = ClientQueues(rt_maxlen=self._rt_maxlen, ca_segment_len=self._ca_segment_len)
            self._clients[client_id] = client_queues
        return client_queues

    def submit_frame(self, client_id: str, frame: np.ndarray) -> None:
        """从 RTMP 流中提交原始帧到 CA-ReadyQueue。

        Args:
            client_id: The client identifier.
            frame: The numpy array of the frame.
        """
        now = time.time()
        frame_data = FrameData(timestamp=now, frame=frame)
        with self._lock:
            client_queues = self._get_or_create_client(client_id)
            # 推入 CA-ReadyQueue，等待推理
            client_queues.ca_ready.append(frame_data)

    def set_rtmp_url(self, client_id: str, rtmp_url: str) -> None:
        """为客户端设置 RTMP 流地址。

        Args:
            client_id: The client identifier.
            rtmp_url: RTMP 流地址，如 rtmp://localhost:1935/live/stream
        """
        with self._lock:
            client_queues = self._get_or_create_client(client_id)
            client_queues.rtmp_url = rtmp_url

    def get_result(self, client_id: str, as_model: bool = False) -> Union[None, FrameData, ProcessedFrame]:
        """返回最新处理帧（从 RT-ProcessedQueue）。

        as_model=True 时返回 ProcessedFrame Pydantic 对象（含 Base64），否则返回 FrameData。
        """
        with self._lock:
            client_queues = self._clients.get(client_id)
            if not client_queues:
                return None
            frame_data = client_queues.rt_processed[-1] if client_queues.rt_processed else client_queues.latest_processed
        
        if frame_data is None:
            return None
        
        if not as_model:
            return frame_data
        
        task_id = client_queues.task.task_id if client_queues.task else None
        return self._create_processed_frame(frame_data, task_id, client_id)

    def remove_client(self, client_id: str) -> None:
        """Remove a client and its queues.

        Args:
            client_id: The client identifier to remove.
        """
        with self._lock:
            self._clients.pop(client_id, None)

    def status(self) -> Dict[str, Any]:
        """获取所有客户端及其队列状态。

        Returns:
            包含客户端数量和每个客户端队列长度的字典。
        """
        with self._lock:
            stats = {client_id: {
                "ca_ready": len(client_queues.ca_ready),
                "ca_raw": len(client_queues.ca_raw),
                "ca_processed": len(client_queues.ca_processed),
                "rt_processed": len(client_queues.rt_processed),
                "rtmp_url": client_queues.rtmp_url
            } for client_id, client_queues in self._clients.items()}
            return {"clients": len(self._clients), "queues": stats}

    def _create_processed_frame(self, frame_data: FrameData, task_id: Optional[int], client_id: str) -> ProcessedFrame:
        """从 FrameData 创建 ProcessedFrame 对象（含 Base64 编码）。"""
        _, buf = cv2.imencode('.jpg', frame_data.frame)
        b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
        return ProcessedFrame(
            task_id=task_id,
            client_id=client_id,
            raw_timestamp=datetime.fromtimestamp(frame_data.timestamp),
            processed_frame_b64=b64,
            inference_result=frame_data.inference_result or {}
        )

    def _execute_inference_pipeline(
        self, 
        frame: np.ndarray, 
        task: Optional[CleaningTask]
    ) -> Tuple[np.ndarray, Dict[str, InferenceResult]]:
        """执行完整的推理管道。
        
        将任务分为独立任务和依赖任务两个阶段:
        1. 并行执行所有独立任务
        2. 串行执行依赖任务（按依赖顺序）
        3. 合并所有可视化结果
        
        Args:
            frame: 输入帧
            task: 清洗任务对象
            
        Returns:
            (可视化后的帧, 所有任务的结果字典)
        """
        all_results: Dict[str, InferenceResult] = {}
        tasks = self._task_registry.get_enabled_tasks()
        
        # 构建上下文
        context: Dict[str, Any] = {
            "task": task,
            "results": all_results
        }
        
        # 阶段1: 并行执行独立任务
        independent_tasks = [t for t in tasks if not t.requires_context()]
        if independent_tasks:
            futures: Dict[Future, InferenceTask] = {}
            for inference_task in independent_tasks:
                future = self._executor.submit(inference_task.infer, frame, context)
                futures[future] = inference_task
            
            # 收集独立任务结果
            for future, inference_task in futures.items():
                try:
                    result = future.result(timeout=5.0)
                    all_results[inference_task.name] = result
                except Exception as e:
                    print(f"Task {inference_task.name} failed: {e}")
                    all_results[inference_task.name] = {
                        "success": False,
                        "error": str(e)
                    }
        
        # 阶段2: 串行执行依赖任务
        dependent_tasks = [t for t in tasks if t.requires_context()]
        for inference_task in dependent_tasks:
            try:
                result = inference_task.infer(frame, context)
                all_results[inference_task.name] = result
            except Exception as e:
                print(f"Task {inference_task.name} failed: {e}")
                all_results[inference_task.name] = {
                    "success": False,
                    "error": str(e)
                }
        
        # 阶段3: 合并可视化结果
        result_frame = frame.copy()
        for inference_task in tasks:
            task_result = all_results.get(inference_task.name, {})
            if task_result.get("success", False):
                try:
                    result_frame = inference_task.visualize(result_frame, task_result)
                except Exception as e:
                    print(f"Visualization for {inference_task.name} failed: {e}")
        
        # 添加通用信息（任务状态等）
        if task:
            info_text = f"Task ID: {task.task_id} | Bending: {task.bending_count}"
            cv2.putText(result_frame, info_text, (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        
        return result_frame, all_results

    def _flush_segment_if_needed(self, client_id: str, client_queues:ClientQueues):
        """当队列达到阈值时，生成原始和处理后的 HLS 视频段及关键点 JSON。"""
        seg_len = client_queues.ca_segment_len
        # 检查是否同时达到阈值
        if len(client_queues.ca_raw) < seg_len or len(client_queues.ca_processed) < seg_len:
            return

        print(f"Generating HLS segment for client: {client_id}")
        # 创建目录
        client_dir = self._db_dir / client_id
        task_id = client_queues.task.task_id if client_queues.task else "no_task"
        task_dir = client_dir / str(task_id)
        hls_dir = task_dir / "hls"
        hls_dir.mkdir(parents=True, exist_ok=True)

        # 收集原始帧数据
        raw_frames_data: List[FrameData] = []
        take = min(seg_len, len(client_queues.ca_raw))
        for _ in range(take):
            raw_frames_data.append(client_queues.ca_raw.popleft())

        # 收集处理后的帧数据
        processed_frames_data: List[FrameData] = []
        take = min(seg_len, len(client_queues.ca_processed))
        for _ in range(take):
            processed_frames_data.append(client_queues.ca_processed.popleft())

        if not raw_frames_data or not processed_frames_data:
            return

        start_ts = processed_frames_data[0].timestamp
        end_ts = processed_frames_data[-1].timestamp

        # 生成原始视频 HLS 段
        raw_segment_path = hls_dir / f"raw_segment_{int(start_ts * 1e6)}.mp4"
        height, width = raw_frames_data[0].frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # type: ignore
        out_raw = cv2.VideoWriter(str(raw_segment_path), fourcc, 30.0, (width, height))
        for frame_data in raw_frames_data:
            out_raw.write(frame_data.frame)
        out_raw.release()

        # 生成处理后视频 HLS 段（含关键点标注）
        segment_path = hls_dir / f"processed_segment_{int(start_ts * 1e6)}.mp4"
        height, width = processed_frames_data[0].frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # type: ignore
        out_processed = cv2.VideoWriter(str(segment_path), fourcc, 30.0, (width, height))
        for frame_data in processed_frames_data:
            out_processed.write(frame_data.frame)
        out_processed.release()

        # 生成关键点 JSON 文件（每个段对应一个 JSON）
        keypoints_path = hls_dir / f"keypoints_{int(start_ts * 1e6)}.json"
        keypoints_list = [
            {
                "timestamp": fd.timestamp,
                "keypoints": fd.keypoints,
                "inference_result": fd.inference_result
            }
            for fd in processed_frames_data
        ]
        with keypoints_path.open('w', encoding='utf-8') as f:
            json.dump(keypoints_list, f, ensure_ascii=False, indent=2)

        # 更新原始视频 M3U8 播放列表
        raw_playlist_path = hls_dir / "raw_playlist.m3u8"
        segment_duration = len(raw_frames_data) / 30.0  # 假设 30 FPS
        if not raw_playlist_path.exists():
            with raw_playlist_path.open('w') as f:
                f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
        with raw_playlist_path.open('a') as f:
            f.write(f"#EXTINF:{segment_duration:.3f},\n")
            f.write(f"{raw_segment_path.name}\n")

        # 更新处理后视频 M3U8 播放列表
        playlist_path = hls_dir / "processed_playlist.m3u8"
        segment_duration = len(processed_frames_data) / 30.0  # 假设 30 FPS 假设 30 FPS
        
        # 初始化播放列表（如果不存在）
        if not playlist_path.exists():
            with playlist_path.open('w') as f:
                f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
        
        # 追加段信息
        with playlist_path.open('a') as f:
            f.write(f"#EXTINF:{segment_duration:.3f},\n")
            f.write(f"{segment_path.name}\n")

        # 记录到数据库
        db = next(get_db())
        try:
            # 记录原始视频段
            raw_hls_segment = HLSSegment(
                client_id=client_id,
                task_id=task_id,
                segment_path=str(raw_segment_path),
                playlist_path=str(raw_playlist_path),
                start_ts=datetime.fromtimestamp(start_ts),
                end_ts=datetime.fromtimestamp(end_ts)
            )
            db.add(raw_hls_segment)
            
            # 记录处理后视频段
            processed_hls_segment = HLSSegment(
                client_id=client_id,
                task_id=task_id,
                segment_path=str(segment_path),
                playlist_path=str(playlist_path),
                start_ts=datetime.fromtimestamp(start_ts),
                end_ts=datetime.fromtimestamp(end_ts)
            )
            db.add(processed_hls_segment)
            db.commit()
            print(f"已生成原始+处理后 HLS 段 + 关键点 JSON for {client_id}: raw={raw_segment_path.name}, processed={segment_path.name}")
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

                # 从 CA-ReadyQueue 取一帧进行推理
                with self._lock:
                    ready_frame = client_queues.ca_ready.popleft() if client_queues.ca_ready else None

                if ready_frame is None:
                    continue

                ts, frame = ready_frame.timestamp, ready_frame.frame

                # 推理前将帧副本放入 CA-RawQueue（用于生成原始视频 HLS 段）
                with self._lock:
                    client_queues.ca_raw.append(ready_frame)

                try:
                    # 执行多任务并行推理
                    final_frame, all_results = self._execute_inference_pipeline(
                        frame, 
                        client_queues.task
                    )

                    processed_frame = FrameData(timestamp=ts, frame=final_frame, inference_result=all_results)
                    print(f"Inference completed for client: {client_id}, results: {all_results.keys()}")
                    with self._lock:
                        # 写入 CA-ProcessedQueue（用于生成 HLS 段）
                        client_queues.ca_processed.append(processed_frame)
                        # 写入 RT-ProcessedQueue（用于实时推送）
                        client_queues.rt_processed.append(processed_frame)
                        # 更新快速访问
                        client_queues.latest_processed = processed_frame
                        
                except Exception as e:
                    print(f"推理异常 for {client_id}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

                # 达到阈值时生成 HLS 段
                try:
                    self._flush_segment_if_needed(client_id, client_queues)
                except Exception as e:
                    print(f"HLS 段生成异常 for {client_id}: {e}")

            # 轻微休眠，避免 CPU 占用过高
            time.sleep(0.001)

        print("AI 推理服务已停止")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._inference_loop, daemon=True, name="InferenceThread")
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        # 关闭线程池
        self._executor.shutdown(wait=True, cancel_futures=True)

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


def submit_frame(client_id: str, frame: np.ndarray):
    """从 RTMP 流提交帧到 CA-ReadyQueue。"""
    manager.submit_frame(client_id, frame)


def set_rtmp_url(client_id: str, rtmp_url: str):
    """设置客户端的 RTMP 流地址。"""
    manager.set_rtmp_url(client_id, rtmp_url)


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