import json
import cv2
import time
import threading
import base64
from pathlib import Path
from collections import deque
import numpy as np
from typing import Optional, Dict, Deque, Tuple, Union, Any,List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future
from abc import ABC, abstractmethod

from app.models.frame import ProcessedFrame, HLSSegment, FrameData
from app.models.task import Task as CleaningTask
from app.services.ai_models import motion, detection
from app.database import get_db, engine
from app.config import settings
import urllib.request
from sqlalchemy import text

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
                "processed_frame": processed_frame,  # 用于可视化，不会序列化
                "keypoints": keypoints
            }
        except Exception as e:
            print(f"Detection task error: {e}")
            return {
                "success": False,
                "error": str(e),
                "processed_frame": frame.copy(),  # 用于可视化，不会序列化
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
    - CA-ReadyQueue: 从 RTMP 提取的原始帧，等待推理（设置最大长度防止溢出）
    - CA-RawQueue: 原始帧副本，用于生成原始视频 HLS 段（设置最大长度防止溢出）
    - CA-ProcessedQueue: 推理后的处理帧（含关键点），用于生成处理后 HLS 段（设置最大长度防止溢出）
    - RT-ProcessedQueue: 实时推理结果（含关键点），用于 WebSocket 推送（约1秒缓存）
    
    内存保护：
    - 所有队列都设置了 maxlen 限制，当队列满时自动丢弃最旧的帧
    - 默认 CA 队列最大长度为 500 帧，约 16.7 秒的视频缓存（30fps）
    - RT 队列长度约为 1 秒的帧数，用于实时推送
    """

    def __init__(self, rt_maxlen: int, ca_segment_len: int, ca_maxlen: int = 500):
        # CA-ReadyQueue: 等待推理的原始帧（设置最大长度限制防止溢出）
        self.ca_ready: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-RawQueue: 原始帧副本，用于落盘生成原始视频（设置最大长度限制）
        self.ca_raw: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-ProcessedQueue: 处理后的帧，用于生成 HLS（设置最大长度限制）
        self.ca_processed: Deque[FrameData] = deque(maxlen=ca_maxlen)
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

    def __init__(self, rt_fps: int = 30, ca_segment_seconds: int = 5, db_dir: Optional[str] = None, ca_maxlen: int = 500):
        # 约 1 秒实时缓存长度
        self._rt_maxlen = max(5, int(rt_fps))
        # 缓存段长度（帧数）
        self._ca_segment_len = max(10, int(rt_fps * ca_segment_seconds))
        # CA 队列最大长度（防止内存溢出）
        self._ca_maxlen = max(50, ca_maxlen)
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
        # 报警去重/批量上报相关结构
        self._alarm_lock = threading.Lock()
        # pending_alarms: key -> { 'count': int, 'first_seen': ts, 'last_seen': ts, 'alarm_info': dict }
        self._pending_alarms: Dict[str, Dict[str, Any]] = {}
        # recent_alarms: key -> last_sent_timestamp
        self._recent_alarms: Dict[str, float] = {}
        # 批量上报间隔（秒）和去重冷却（秒），可从 settings 中配置
        self._alarm_batch_interval = getattr(settings, 'alarm_batch_interval', 30)
        self._alarm_cooldown_seconds = getattr(settings, 'alarm_cooldown_seconds', 60)
        self._alarm_thread: Optional[threading.Thread] = None
    
    def _register_default_tasks(self):
        """注册默认的推理任务"""
        # return # Test Only
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
            client_queues = ClientQueues(
                rt_maxlen=self._rt_maxlen, 
                ca_segment_len=self._ca_segment_len,
                ca_maxlen=self._ca_maxlen
            )
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

    def set_stream_url(self, client_id: str, stream_url: str) -> None:
        """为客户端设置通用流地址（RTMP/RTSP/其他）。

        该方法是对 `set_rtmp_url` 的通用替代，保留老接口以兼容历史代码。

        Args:
            client_id: The client identifier.
            stream_url: 流地址，例如 `rtmp://...` 或 `rtsp://...`。
        """
        # 目前内部仍然使用 client_queues.rtmp_url 字段保持向后兼容
        with self._lock:
            client_queues = self._get_or_create_client(client_id)
            client_queues.rtmp_url = stream_url

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
        
        # 过滤推理结果，移除不可序列化的对象（如 numpy 数组）
        inference_result = self._make_json_serializable(frame_data.inference_result or {})
        
        return ProcessedFrame(
            task_id=task_id,
            client_id=client_id,
            raw_timestamp=datetime.fromtimestamp(frame_data.timestamp),
            processed_frame_b64=b64,
            inference_result=inference_result
        )
    
    def _make_json_serializable(self, obj: Any) -> Any:
        """递归过滤对象，移除不可 JSON 序列化的内容（如 numpy 数组、annotated_frame 等）"""
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                # 跳过已知的不可序列化字段
                if key in ('annotated_frame', 'processed_frame', 'frame'):
                    continue
                result[key] = self._make_json_serializable(value)
            return result
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            # numpy 数组转为列表（如果是小数组）或跳过
            if obj.size < 100:  # 只转换小数组（如检测框坐标）
                return obj.tolist()
            return None
        elif isinstance(obj, (np.integer, np.floating)):
            # numpy 标量转为 Python 原生类型
            return obj.item()
        else:
            # 基本类型直接返回
            return obj

    def _execute_inference_pipeline(
        self, 
        frame: np.ndarray, 
        task: Optional[CleaningTask],
        client_id: Optional[str] = None
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
        print("Inferring on task:", task.task_id if task else "No Task")
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
        
        # 添加通用信息（任务状态等），放到底部以避免与顶部可视化文字重叠，并绘制背景框提高可读性
        if task:
            info_text = f"Task ID: {task.task_id} | Bending: {task.bending}"
            h, w = result_frame.shape[:2]
            # 文本尺寸与基线
            (text_w, text_h), baseline = cv2.getTextSize(info_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            x, y = 10, h - 10
            # 背景矩形（稍留边距）
            rect_tl = (x - 6, y - text_h - 6)
            rect_br = (x + text_w + 6, y + 6)
            cv2.rectangle(result_frame, rect_tl, rect_br, (0, 0, 0), -1)
            cv2.putText(result_frame, info_text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        # 异常检测：
        # - 任一子任务返回 success=False -> 认为推理异常
        # - motion 任务返回 actions 指示的异常（如 bending_detected, bubble_detected）
        try:
            alarm_needed = False
            alarm_info = {
                "client_id": client_id,
                "task_id": task.task_id if task else None,
                "step_id": getattr(task, 'current_step', None) if task else None,
                "detection_result": {}
            }

            # 检查是否有任务失败
            for name, res in all_results.items():
                if isinstance(res, dict) and res.get('success') is False:
                    alarm_needed = True
                    alarm_info['detection_result'][name] = {'error': res.get('error')}

            # motion 异常判定（若存在）
            motion_res = all_results.get('motion')
            if isinstance(motion_res, dict) and motion_res.get('success'):
                actions = motion_res.get('actions', {})
                # 常见异常标志
                if actions.get('bending_detected') or actions.get('bubble_detected') or actions.get('submersion_status') in ('not_submerged', 'partial'):
                    alarm_needed = True
                    alarm_info['detection_result']['motion'] = actions

            if alarm_needed:
                # 将告警入队，交由批量去重线程处理，避免高频重复上报
                try:
                    self._enqueue_alarm(alarm_info)
                except Exception as e:
                    print(f"Failed to enqueue alarm: {e}")

        except Exception as e:
            print(f"Alarm detection error: {e}")

        return result_frame, all_results

    def _handle_alarm(self, alarm_info: Dict[str, Any]):
        """统一处理告警：调用远端告警上报接口并在本地数据库记录一条告警记录。

        alarm_info 示例结构:
        {
            'task_id': 1,
            'step_id': '0',
            'detection_result': { ... }
        }
        """
        try:
            task_id = alarm_info.get('task_id')
            step_id = alarm_info.get('step_id')
            client_id = alarm_info.get('client_id')

            # 若 task_id/step_id 缺失，可尝试从 client queue 补全
            if (task_id is None or step_id is None) and client_id:
                try:
                    with self._lock:
                        cq = self._clients.get(str(client_id))
                        if cq:
                            print(f"_handle_alarm: found client queue for {client_id}, task={cq.task}")
                        else:
                            print(f"_handle_alarm: no client queue found for {client_id}; available clients: {list(self._clients.keys())}")
                        if cq and cq.task:
                            if task_id is None:
                                task_id = getattr(cq.task, 'task_id', None)
                            if step_id is None:
                                step_id = getattr(cq.task, 'current_step', None)
                except Exception as e:
                    print(f"Failed to fill task_id/step_id from client queues: {e}")

            print(f"_handle_alarm: final task_id={task_id}, step_id={step_id}, client_id={client_id}")
            detection_result = alarm_info.get('detection_result')

            alarm_type = '流程违规' if detection_result else '推理异常'
            alarm_level = 'high'
            alarm_message = 'AI推理检测到异常' if detection_result else 'AI推理异常'

            # 调用远端告警上报（仅在 task_id 和 step_id 可用时上报）
            try:
                # 把聚合字段一并上报（若存在）
                alarm_count = alarm_info.get('alarm_count') if isinstance(alarm_info, dict) else None
                first_seen = alarm_info.get('first_seen') if isinstance(alarm_info, dict) else None
                last_seen = alarm_info.get('last_seen') if isinstance(alarm_info, dict) else None
                if task_id and step_id:
                    self._send_alarm_report(
                        task_id=task_id,
                        step_id=step_id,
                        alarm_type=alarm_type,
                        alarm_level=alarm_level,
                        alarm_message=alarm_message,
                        detection_result=detection_result,
                        camera_ip=None,
                        reader_ip=None,
                        alarm_count=alarm_count,
                        first_seen=first_seen,
                        last_seen=last_seen
                    )
                else:
                    print(f"Skipping remote alarm report: missing task_id or step_id (task_id={task_id}, step_id={step_id})")
            except Exception as e:
                print(f"Remote alarm report failed: {e}")

            # 写入本地数据库表 alarm_record（如果不存在则先创建）
            try:
                self._record_alarm_db(
                    task_id=task_id,
                    step_id=step_id,
                    alarm_type=alarm_type,
                    alarm_level=alarm_level,
                    alarm_message=alarm_message,
                    detection_result=detection_result
                )
            except Exception as e:
                print(f"Local DB alarm record failed: {e}")

        except Exception as e:
            print(f"_handle_alarm exception: {e}")

    def _enqueue_alarm(self, alarm_info: Dict[str, Any]):
        """将告警信息入队，交由批量去重线程处理。"""
        try:
            key = f"{alarm_info.get('task_id')}_{alarm_info.get('step_id')}"
            with self._alarm_lock:
                if key not in self._pending_alarms:
                    self._pending_alarms[key] = {
                        'count': 1,
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'alarm_info': alarm_info
                    }
                else:
                    self._pending_alarms[key]['count'] += 1
                    self._pending_alarms[key]['last_seen'] = time.time()
        except Exception as e:
            print(f"_enqueue_alarm error: {e}")

    def _flush_pending_alarms(self):
        """检查并上报待处理的告警（去重/批量逻辑）。

        策略：
        - 每次 flush 遍历 pending_alarms
        - 如果最近已上报且处于冷却期（_alarm_cooldown_seconds），则保留在 pending
        - 否则将该告警聚合（添加 count/first_seen/last_seen）并提交到 `_handle_alarm`
        """
        now = time.time()
        to_send = []
        with self._alarm_lock:
            keys = list(self._pending_alarms.keys())
            for key in keys:
                item = self._pending_alarms.get(key)
                if not item:
                    continue
                last_sent = self._recent_alarms.get(key)
                if last_sent and (now - last_sent) < self._alarm_cooldown_seconds:
                    # 仍在冷却期，跳过（保留在 pending）
                    continue
                # 准备发送：构建聚合告警信息
                agg = dict(item['alarm_info']) if item.get('alarm_info') else {}
                agg['alarm_count'] = item.get('count', 1)
                agg['first_seen'] = datetime.fromtimestamp(item.get('first_seen')).strftime("%Y-%m-%d %H:%M:%S")
                agg['last_seen'] = datetime.fromtimestamp(item.get('last_seen')).strftime("%Y-%m-%d %H:%M:%S")
                to_send.append((key, agg))
                # 更新最近上报时间并移除 pending
                self._recent_alarms[key] = now
                del self._pending_alarms[key]

        # 在锁外发送，避免阻塞其他操作
        for key, agg_alarm in to_send:
            try:
                self._executor.submit(self._handle_alarm, agg_alarm)
            except Exception as e:
                print(f"Failed to submit aggregated alarm for {key}: {e}")

    def _alarm_flush_loop(self):
        """后台线程，周期性 flush pending alarms。"""
        print("Alarm flush thread started")
        while not self._stop_event.is_set():
            try:
                time.sleep(self._alarm_batch_interval)
                self._flush_pending_alarms()
            except Exception as e:
                print(f"Alarm flush loop error: {e}")
        # 在退出前再 flush 一次（尝试上报剩余告警）
        try:
            self._flush_pending_alarms()
        except Exception as e:
            print(f"Final alarm flush error: {e}")

    def _send_alarm_report(self, task_id: int, step_id: int, alarm_type: str, alarm_level: str, alarm_message: str, detection_result: Optional[Dict]=None, camera_ip: Optional[str]=None, reader_ip: Optional[str]=None, alarm_count: Optional[int]=None, first_seen: Optional[str]=None, last_seen: Optional[str]=None) -> bool:
        """按照外部接口文档上报告警（同步调用，故需在后台线程使用）。

        增强：支持重试与可选的聚合字段（alarm_count, first_seen, last_seen）。
        """
        url = "http://116.204.65.72:8881/gdmp/v1/api/nt/alarm_report"
        payload = {
            "task_id": task_id if task_id is not None else 0,
            "step_id": step_id if step_id is not None else 0,
            "alarm_type": alarm_type,
            "alarm_level": alarm_level,
            "alarm_message": alarm_message,
            "alarm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if detection_result is not None:
            payload['detection_result'] = detection_result
        if camera_ip:
            payload['camera_ip'] = camera_ip
        if reader_ip:
            payload['reader_ip'] = reader_ip
        # 聚合字段（可选）
        if alarm_count is not None:
            payload['alarm_count'] = int(alarm_count)
        if first_seen:
            payload['first_seen'] = first_seen
        if last_seen:
            payload['last_seen'] = last_seen

        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'AI-Backend/1.0'
        }

        # 重试逻辑
        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_text = resp.read().decode('utf-8')
                    try:
                        j = json.loads(resp_text)
                        if j.get('code') == 0:
                            print(f"Alarm reported successfully: task_id={task_id}")
                            return True
                        else:
                            print(f"Alarm report returned non-zero code: {j}")
                            # 不做立即重试，记录并返回 False
                            return False
                    except Exception:
                        print(f"Alarm report response (non-json): {resp_text}")
                        return False
            except Exception as e:
                print(f"Attempt {attempt} failed to send alarm report: {e}")
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                else:
                    return False

    def _record_alarm_db(self, task_id: Optional[int], step_id: Optional[Any], alarm_type: str, alarm_level: str, alarm_message: str, detection_result: Optional[Dict]=None, camera_ip: Optional[str]=None, reader_ip: Optional[str]=None) -> None:
        """在数据库中创建表 `clean_alarm`（若不存在）并插入一条告警记录。

        使用最新字段属性：
        - alarm_id (SERIAL PRIMARY KEY)
        - task_id INTEGER
        - step_id INTEGER
        - alarm_type TEXT
        - message TEXT
        - severity TEXT
        - resolved BOOLEAN
        - resolved_by INTEGER
        - detected_at TIMESTAMP
        - resolved_at TIMESTAMP
        """
        try:
            create_sql = '''
            CREATE TABLE IF NOT EXISTS clean_alarm (
                alarm_id SERIAL PRIMARY KEY,
                task_id INTEGER,
                step_id INTEGER,
                alarm_type TEXT,
                message TEXT,
                severity TEXT,
                resolved BOOLEAN DEFAULT FALSE,
                resolved_by INTEGER,
                detected_at BIGINT,
                resolved_at BIGINT,
                created_at TIMESTAMP DEFAULT now()
            )
            '''
            with engine.begin() as conn:
                conn.execute(text(create_sql))
                insert_sql = '''
                INSERT INTO clean_alarm (task_id, step_id, alarm_type, message, severity, resolved, resolved_by, detected_at, resolved_at)
                VALUES (:task_id, :step_id, :alarm_type, :message, :severity, :resolved, :resolved_by, :detected_at, :resolved_at)
                '''
                params = {
                    'task_id': int(task_id) if task_id is not None else None,
                    'step_id': int(step_id) if step_id is not None and str(step_id).isdigit() else None,
                    'alarm_type': alarm_type,
                    'message': alarm_message,
                    'severity': alarm_level,
                    'resolved': False,
                    'resolved_by': None,
                    'detected_at': int(time.time()),
                    'resolved_at': None
                }
                conn.execute(text(insert_sql), params)
        except Exception as e:
            print(f"_record_alarm_db error: {e}")
            # 尝试回退：根据实际表结构动态构建 INSERT
            try:
                with engine.connect() as conn2:
                    info_sql = text("SELECT column_name, column_default, is_nullable FROM information_schema.columns WHERE table_name = 'clean_alarm'")
                    res = conn2.execute(info_sql).fetchall()
                    print("clean_alarm table columns:")
                    cols = {}
                    for row in res:
                        print(row)
                        cols[row[0]] = {
                            'default': row[1],
                            'nullable': row[2]
                        }

                    # 只插入目标参数中存在于表中的列
                    candidate_params = {
                        'task_id': int(task_id) if task_id is not None else None,
                        'step_id': int(step_id) if step_id is not None and str(step_id).isdigit() else None,
                        'alarm_type': alarm_type,
                        'message': alarm_message,
                        'severity': alarm_level,
                        'resolved': False,
                        'resolved_by': None,
                        'detected_at': int(time.time()),
                        'resolved_at': None
                    }

                    insert_cols = []
                    insert_vals = {}
                    # 使用存在的列填充参数
                    for k, v in candidate_params.items():
                        if k in cols:
                            insert_cols.append(k)
                            insert_vals[k] = v

                    # 对于表中非空且没有默认值的列，如果未提供则尝试生成合理的占位值
                    for col_name, meta in cols.items():
                        if col_name not in insert_cols and meta.get('nullable') == 'NO' and not meta.get('default'):
                            # 生成占位值：带 id 字段用时间戳，文本列用空串，布尔用 False
                            if 'id' in col_name or col_name.endswith('_id'):
                                insert_cols.append(col_name)
                                insert_vals[col_name] = int(time.time() * 1000)
                            elif col_name in ('resolved',):
                                insert_cols.append(col_name)
                                insert_vals[col_name] = False
                            else:
                                insert_cols.append(col_name)
                                insert_vals[col_name] = ''

                    if not insert_cols:
                        print("没有可用于回退插入的列，跳过 clean_alarm 回退插入")
                    else:
                        cols_sql = ", ".join(insert_cols)
                        vals_sql = ", ".join([f":{c}" for c in insert_cols])
                        fallback_sql = f"INSERT INTO clean_alarm ({cols_sql}) VALUES ({vals_sql})"
                        print(f"尝试回退插入 clean_alarm，使用列: {insert_cols}")
                        conn2.execute(text(fallback_sql), insert_vals)
                        print("回退插入 clean_alarm 成功")
            except Exception as e2:
                print(f"Failed to fetch clean_alarm schema info or fallback insert failed: {e2}")

    def _flush_segment_if_needed(self, client_id: str, client_queues:ClientQueues):
        """当队列达到阈值时，生成原始和处理后的 HLS 视频段及关键点 JSON。"""
        print(f"Checking HLS segment flush for client: {client_id}")
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
        keypoints_list = []
        for fd in processed_frames_data:
            kp = fd.keypoints if hasattr(fd, 'keypoints') else None
            ir = fd.inference_result if hasattr(fd, 'inference_result') else None
            keypoints_list.append({
                "timestamp": fd.timestamp,
                "keypoints": self._make_json_serializable(kp),
                "inference_result": self._make_json_serializable(ir)
            })
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

        # 记录到数据库（使用与告警相同的 SQL 路径：创建表（若不存在）并以参数化 INSERT 写入）
        try:
            create_sql = '''
            CREATE TABLE IF NOT EXISTS file_path (
                _id BIGSERIAL PRIMARY KEY,
                client_id TEXT,
                task_id INTEGER,
                segment_path TEXT,
                playlist_path TEXT,
                start_ts BIGINT,
                end_ts BIGINT,
                created_at BIGINT
            )
            '''

            insert_sql = '''
            INSERT INTO file_path (client_id, task_id, segment_path, playlist_path, start_ts, end_ts, created_at)
            VALUES (:client_id, :task_id, :segment_path, :playlist_path, :start_ts, :end_ts, :created_at)
            '''

            # 尝试将 task_id 转为整数，否则使用 NULL
            try:
                t_id = int(task_id) if task_id is not None and str(task_id).isdigit() else None
            except Exception:
                t_id = None

            params_raw = {
                'client_id': str(client_id),
                'task_id': t_id,
                'segment_path': str(raw_segment_path),
                'playlist_path': str(raw_playlist_path),
                'start_ts': int(start_ts),
                'end_ts': int(end_ts),
                'created_at': int(time.time())
            }

            params_processed = {
                'client_id': str(client_id),
                'task_id': t_id,
                'segment_path': str(segment_path),
                'playlist_path': str(playlist_path),
                'start_ts': int(start_ts),
                'end_ts': int(end_ts),
                'created_at': int(time.time())
            }

            # 调用外部 file_path 插入接口，避免本服务直接生成主键 id
            insert_url = settings.file_path_insert_url or f"http://116.204.65.72:8881/gdmp/v1/api/nt/file_path_insert"

            payload_raw = {
                'client_id': str(client_id),
                'task_id': t_id,
                'segment_path': str(raw_segment_path),
                'playlist_path': str(raw_playlist_path),
                'start_ts': int(start_ts),
                'end_ts': int(end_ts)
            }

            payload_processed = {
                'client_id': str(client_id),
                'task_id': t_id,
                'segment_path': str(segment_path),
                'playlist_path': str(playlist_path),
                'start_ts': int(start_ts),
                'end_ts': int(end_ts)
            }

            # 使用 urllib 发送 POST 请求，带简单重试逻辑
            def post_json(url, data, retries=3, timeout=10):
                jd = json.dumps(data, ensure_ascii=False).encode('utf-8')
                headers = {'Content-Type': 'application/json; charset=utf-8'}
                attempt = 0
                backoff = 1.0
                while attempt < retries:
                    attempt += 1
                    try:
                        req = urllib.request.Request(url, data=jd, headers=headers, method='POST')
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            body = resp.read().decode('utf-8')
                            return resp.getcode(), body
                    except Exception as e:
                        print(f"Attempt {attempt} to POST to {url} failed: {e}")
                        if attempt < retries:
                            time.sleep(backoff)
                            backoff *= 2
                        else:
                            return None, str(e)

            code1, body1 = post_json(insert_url, payload_raw)
            code2, body2 = post_json(insert_url, payload_processed)

            if code1 and (200 <= code1 < 300):
                print(f"file_path insert raw succeeded: {code1} {body1}")
            else:
                print(f"file_path insert raw failed: {code1} {body1}")

            if code2 and (200 <= code2 < 300):
                print(f"file_path insert processed succeeded: {code2} {body2}")
            else:
                print(f"file_path insert processed failed: {code2} {body2}")

            print(f"已生成原始+处理后 HLS 段 + 关键点 JSON for {client_id}: raw={raw_segment_path.name}, processed={segment_path.name}")

        except Exception as e:
            print(f"HLS 插入接口调用失败 for {client_id}: {e}")

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
                        client_queues.task,
                        client_id=client_id
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
        # 启动告警批量上报线程（如果尚未运行）
        if self._alarm_thread is None or not getattr(self._alarm_thread, 'is_alive', lambda: False)():
            try:
                self._alarm_thread = threading.Thread(target=self._alarm_flush_loop, daemon=True, name="AlarmFlushThread")
                self._alarm_thread.start()
            except Exception as e:
                print(f"Failed to start alarm flush thread: {e}")

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        # 停止并等待告警线程
        if self._alarm_thread is not None:
            try:
                self._alarm_thread.join(timeout=1.0)
            except Exception:
                pass
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
            print(f"任务已设置 for client {client_id}: {task}")
            return True

    def terminate_task_by_id(self, client_id: str) -> bool:
        """终止指定客户端的任务，清理所有队列和资源。

        Args:
            client_id: 客户端ID

        Returns:
            是否成功终止
        """
        with self._lock:
            client_queues = self._clients.get(client_id)
            if client_queues is None:
                return False

            # 清理所有队列，释放内存
            client_queues.ca_ready.clear()
            client_queues.ca_raw.clear()
            client_queues.ca_processed.clear()
            client_queues.rt_processed.clear()

            # 清除其他引用
            client_queues.latest_processed = None
            client_queues.task = None
            client_queues.rtmp_url = None

            # 从客户端字典中移除
            del self._clients[client_id]

            print(f"任务已终止，客户端 {client_id} 的所有队列和资源已清理")
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


def set_stream_url(client_id: str, stream_url: str):
    """设置客户端的通用流地址（RTMP/RTSP）。

    推荐在新代码中使用此函数以避免命名歧义。
    """
    manager.set_stream_url(client_id, stream_url)


def get_result(client_id: str, as_model: bool = False):
    return manager.get_result(client_id, as_model=as_model)


def remove_client(client_id: str):
    print(f"Removing client: {client_id}")
    manager.remove_client(client_id)


def status():
    return manager.status()


def set_task(client_id: str, task: Optional[CleaningTask]) -> bool:
    """为客户端设置任务。"""
    return manager.set_task(client_id, task)

def get_task(client_id: str) -> Optional[CleaningTask]:
    """获取客户端的任务。"""
    return manager.get_task(client_id)