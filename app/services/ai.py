"""AI 推理模块，实现推理任务注册、调度与落盘。"""

import base64
import json
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from app.services.client import ClientQueues
from app.services.infer_task import InferenceResult, InferenceTask
from app.models.frame import ProcessedFrame, FrameData
from app.models.task import Task as CleaningTask

from app.database import engine
from app.settings import settings
from app.services.task_pipeline.leak.leak_test import LeakBubblePipelineService
import urllib.request
from sqlalchemy import text


class InferenceTaskRegistry:
    """底层推理任务注册表（仅负责 InferenceTask）。

    - 管理 YOLO 弯折 / 气泡等底层模型任务实例；
    - 维护执行顺序与启用状态；
    - 不关心具体客户端或 TaskPipeline。
    """

    def __init__(self):
        self._tasks: Dict[str, InferenceTask] = {}
        self._execution_order: List[str] = []

    def register(self, task: InferenceTask):
        """注册一个底层推理任务（如 YOLO 检测）。"""
        self._tasks[task.name] = task
        self._recompute_execution_order()

    def unregister(self, task_name: str):
        """注销一个底层推理任务。"""
        if task_name in self._tasks:
            del self._tasks[task_name]
            self._recompute_execution_order()

    def get_task(self, name: str) -> Optional[InferenceTask]:
        """获取指定底层推理任务。"""
        return self._tasks.get(name)

    def get_enabled_tasks(self) -> List[InferenceTask]:
        """获取所有启用的底层推理任务（按执行顺序）。"""
        return [self._tasks[name] for name in self._execution_order if self._tasks[name].enabled]

    def _recompute_execution_order(self):
        """重新计算底层任务执行顺序（简单拓扑排序占位）。"""
        independent: List[str] = []
        dependent: List[str] = []

        for name, task in self._tasks.items():
            if not task.requires_context():
                independent.append(name)
            else:
                dependent.append(name)

        self._execution_order = independent + dependent


class PipelineRegistry:
    """TaskPipeline 注册表（按 client_id 管理流水线实例）。

    只负责 per-client 的 Pipeline 生命周期与路由：
    - 持有对 InferenceTaskRegistry 的引用，用于复用底层任务实例；
    - 按 client_id（以及 CleanTask 上下文）创建 / 缓存 / 清理 TaskPipeline；
    - 目前仅支持 LeakBubblePipelineService，一旦有更多 Pipeline，
      可以在此类内部做集中路由。
    """

    def __init__(self, task_registry: InferenceTaskRegistry):
        self._task_registry = task_registry
        # 每个客户端当前活跃的 TaskPipeline（目前仅 LeakBubblePipelineService 一种）
        # key 约定为 str(client_id)，对于无 client_id 的场景使用 "default"
        self._pipelines: Dict[str, LeakBubblePipelineService] = {}
        self._lock = threading.Lock()

    def get_or_create_pipeline(
        self,
        client_id: Optional[str],
        task: Optional[CleaningTask],
        executor: ThreadPoolExecutor,
    ) -> LeakBubblePipelineService:
        """按 client + CleanTask 获取或创建当前活跃的 TaskPipeline。

        当前仅实现一个 LeakBubblePipelineService：
        - 将同一组 YOLO 任务实例复用到所有 Pipeline 中；
        - 后续若根据 task.current_step 选择不同 Pipeline，只需在此方法内扩展分支。
        """

        key = str(client_id) if client_id is not None else "default"
        with self._lock:
            pipeline = self._pipelines.get(key)
            if pipeline is not None:
                return pipeline

            # 复用已经注册的 YOLO 任务实例（如存在）
            bubble_task = self._task_registry.get_task("bubble_detection")
            bending_task = self._task_registry.get_task("endoscope_bending_detection")

            pipeline = LeakBubblePipelineService(
                executor=executor,
                bubble_task=bubble_task,
                bending_task=bending_task,
            )
            self._pipelines[key] = pipeline
            return pipeline

    def get_pipeline(self, client_id: Optional[str]) -> Optional[LeakBubblePipelineService]:
        """仅按 client_id 获取已存在的 TaskPipeline（不创建）。"""

        key = str(client_id) if client_id is not None else "default"
        with self._lock:
            return self._pipelines.get(key)

    def remove_pipelines_for_client(self, client_id: str) -> None:
        """清理指定客户端关联的所有 Pipeline 实例。"""

        key = str(client_id)
        with self._lock:
            pipeline = self._pipelines.pop(key, None)
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                # 保护性清理，避免单个 Pipeline 停止失败影响整体
                pass


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
        
        # 任务注册表（只管理底层 InferenceTask）
        self._task_registry = InferenceTaskRegistry()
        self._register_default_tasks()
        
        # 线程池用于并行推理
        self._executor = ThreadPoolExecutor(max_workers=4)

        # Pipeline 注册表：按 client_id 管理 per-client TaskPipeline
        self._pipeline_registry = PipelineRegistry(self._task_registry)

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
        # 持久化队列与线程（用于 HLS 段写盘与告警持久化，避免阻塞推理线程）
        self._persist_queue: "queue.Queue" = queue.Queue()
        self._persist_thread: Optional[threading.Thread] = None
        # GPU 批量大小（可配置）
        self._batch_size = getattr(settings, 'gpu_batch_size', 4)
        # Task / Pipeline 注册表：统一管理底层 InferenceTask 与 per-client TaskPipeline
    
    def _register_default_tasks(self):
        """注册默认的推理任务"""
        # return # Test Only
        # self._task_registry.register(DetectionTask())
        # self._task_registry.register(MotionTask())
        
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
        
        # 注册气泡检测任务
        try:
            from app.services.ai_models.bubble_task import BubbleDetectionTask
            bubble_task = BubbleDetectionTask(
                model_path=settings.bubble_model_path,
                conf_threshold=settings.bubble_conf_threshold,
                iou_threshold=settings.bubble_iou_threshold,
                enabled=True  # 默认启用，可通过 enable_task 控制
            )
            self._task_registry.register(bubble_task)
            print(f"气泡检测任务已注册 (模型: {settings.bubble_model_path})")
        except Exception as e:
            print(f"气泡检测任务注册失败 (可能未安装 ultralytics): {e}")
        
        # 未来可以在这里添加更多任务
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
        """从视频流中提交原始帧到 CA-ReadyQueue。

        Args:
            client_id: The client identifier.
            frame: The numpy array of the frame.
        """
        now = time.time()
        frame_data = FrameData(timestamp=now, frame=frame)
        with self._lock:
            client_queues = self._get_or_create_client(client_id)
            # 推入 CA-ReadyQueue，等待推理
            client_queues.append_ca_ready(frame_data)

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
            frame_data = client_queues.get_latest_result()
        
        if frame_data is None:
            return None
        
        if not as_model:
            return frame_data
        
        task_id = client_queues.get_task_id()
        return self._create_processed_frame(frame_data, task_id, client_id)

    def remove_client(self, client_id: str) -> None:
        """Remove a client and its queues.

        Args:
            client_id: The client identifier to remove.
        """
        # 先从客户端字典中取出对应队列引用
        with self._lock:
            client_queues = self._clients.pop(client_id, None)

        # 如果存在队列，则在移除前先强制将剩余缓存全部落盘
        if client_queues is not None:
            try:
                self._flush_all_remaining_segments(client_id, client_queues)
            except Exception as e:
                print(f"Failed to flush remaining segments when removing client {client_id}: {e}")

        # 无论是否存在队列，都尝试清理与该客户端关联的 TaskPipeline
        try:
            self._pipeline_registry.remove_pipelines_for_client(client_id)
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        """获取所有客户端及其队列状态。

        Returns:
            包含客户端数量和每个客户端队列长度的字典。
        """
        with self._lock:
            stats = {client_id: client_queues.to_status_dict() for client_id, client_queues in self._clients.items()}
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

    # --- TaskPipeline 集成 ---

    def _get_or_create_leak_pipeline(
        self,
        client_id: Optional[str],
        task: Optional[CleaningTask],
    ) -> LeakBubblePipelineService:
        """按 client + CleanTask 获取/创建泄漏+气泡 TaskPipeline。

        具体 Pipeline 的创建与缓存逻辑委托给 PipelineRegistry：
        - 由 PipelineRegistry 复用已注册的 YOLO 任务实例；
        - 由 PipelineRegistry 管理每个 client 的活跃 Pipeline 实例。
        """

        return self._pipeline_registry.get_or_create_pipeline(client_id, task, self._executor)

    def _execute_inference_pipeline_batch(self, frames: List[np.ndarray], timestamps: List[float], task: Optional[CleaningTask], client_id: Optional[str] = None) -> None:
        """使用 TaskPipeline 进行批量推理。

        说明：
        - 这里只负责驱动 ``LeakBubblePipelineService.infer_frame`` 填充各子任务 cache；
        - 实际的可视化帧与聚合结果由 TaskPipeline 异步线程写入
          ``rt_cache_frame/ca_cache_frame`` 和 ``rt_cache_msg/ca_cache_msg``；
        - 推理主循环随后通过读取这些 cache 并写入 ClientQueues，
          作为唯一的数据来源（不再直接依赖各 InferenceTask 的输出）。
        """

        n = len(frames)
        if n == 0:
            return

        if len(timestamps) != n:
            # 时间戳长度不匹配时，简单重建一组时间戳
            base = time.time()
            timestamps = [float(base + i * 1e-3) for i in range(n)]

        # 根据当前 CleanTask 从注册表中获取 / 创建对应 TaskPipeline
        pipeline = self._get_or_create_leak_pipeline(client_id, task)

        context: Dict[str, Any] = {"task": task, "client_id": client_id}
        try:
            # 触发 TaskPipeline 级别的批量推理，内部会调用各子任务的 infer_batch，
            # 利用 YOLO 的 detect_batch 接口提升整体推理效率。
            pipeline.infer_batch(frames, timestamps=timestamps, context=context)
        except Exception as e:
            print(f"TaskPipeline 批量推理异常 for client {client_id}: {e}")

    def _drain_pipeline_caches_to_client(self, client_id: str, client_queues: ClientQueues) -> None:
        """将 TaskPipeline 的聚合 cache 映射到对应客户端队列。

        - rt_cache_frame -> ClientQueues.rt_processed（用于实时展示）
        - ca_cache_frame -> ClientQueues.ca_processed（用于 HLS 段与 JSON 落盘）

        简化策略：
        - TaskPipeline 作为生产者持续 append 到 deque 尾部；
        - InferenceManager 作为消费者从队首 popleft，将元素转移到对应 client 队列；
        - 这样无需维护额外 offset 状态，逻辑更直观。
        """

        # 通过 PipelineRegistry 获取当前 client 对应的 Pipeline（不创建新实例）
        pipeline = self._pipeline_registry.get_pipeline(client_id)
        if pipeline is None:
            return

        # 1. 映射实时帧到 RT-ProcessedQueue
        try:
            rt_cache = pipeline.rt_cache_frame
            # 直接从队首消费所有可用帧，append 到客户端 RT 队列
            while rt_cache:
                fd = rt_cache.popleft()
                client_queues.append_rt_processed(fd)
        except Exception as e:
            print(f"从 TaskPipeline rt_cache_frame 映射到客户端队列失败 for {client_id}: {e}")

        # 2. 映射持久化帧到 CA-ProcessedQueue
        try:
            ca_cache = pipeline.ca_cache_frame
            while ca_cache:
                fd = ca_cache.popleft()
                client_queues.append_ca_processed(fd)
        except Exception as e:
            print(f"从 TaskPipeline ca_cache_frame 映射到客户端队列失败 for {client_id}: {e}")

        # 3. msg cache 目前主要由 TaskPipeline 内部与持久化逻辑消费，
        # 此处不再维护偏移，保持只读/调试用途。

    def _handle_alarm(self, alarm_info: Dict[str, Any]):
        """将告警信息放入持久化队列，由持久化线程执行上报与写 DB，避免阻塞推理线程。"""
        try:
            self._persist_queue.put({"type": "alarm", "alarm": alarm_info})
        except Exception as e:
            print(f"_handle_alarm enqueue failed: {e}")

    def _handle_alarm_now(self, alarm_info: Dict[str, Any]):
        """实际执行告警上报与写库的函数（将在持久化线程中调用）。

        该实现为原有 _handle_alarm 的实现内容。
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
                            print(f"_handle_alarm_now: found client queue for {client_id}, task={cq.task}")
                        else:
                            print(f"_handle_alarm_now: no client queue found for {client_id}; available clients: {list(self._clients.keys())}")
                        if cq and cq.task:
                            if task_id is None:
                                task_id = getattr(cq.task, 'task_id', None)
                            if step_id is None:
                                step_id = getattr(cq.task, 'current_step', None)
                except Exception as e:
                    print(f"Failed to fill task_id/step_id from client queues: {e}")

            print(f"_handle_alarm_now: final task_id={task_id}, step_id={step_id}, client_id={client_id}")
            detection_result = alarm_info.get('detection_result')

            alarm_type = '流程违规' if detection_result else '推理异常'
            alarm_level = 'high'
            alarm_message = 'AI推理检测到异常' if detection_result else 'AI推理异常'

            # 调用远端告警上报（仅在 task_id 和 step_id 可用时上报）
            try:
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
            print(f"_handle_alarm_now exception: {e}")

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
                agg['first_seen'] = datetime.fromtimestamp(item.get('first_seen')).strftime("%Y-%m-%d %H:%M:%S") # type: ignore
                agg['last_seen'] = datetime.fromtimestamp(item.get('last_seen')).strftime("%Y-%m-%d %H:%M:%S") # type: ignore
                to_send.append((key, agg))
                # 更新最近上报时间并移除 pending
                self._recent_alarms[key] = now
                del self._pending_alarms[key]

        # 在锁外发送，避免阻塞其他操作
        for key, agg_alarm in to_send:
            try:
                # 将告警入持久化队列，由持久化线程执行网络上报与写 DB
                self._persist_queue.put({"type": "alarm", "alarm": agg_alarm})
            except Exception as e:
                print(f"Failed to enqueue aggregated alarm for {key}: {e}")

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

    def _persistent_worker(self):
        """持久化线程：处理写盘 HLS 段与告警上报/落库等耗时操作。"""
        print("Persistent worker started")
        while not self._stop_event.is_set():
            try:
                job = None
                try:
                    job = self._persist_queue.get(timeout=1.0)
                except Exception:
                    job = None

                if job is None:
                    continue

                jtype = job.get('type')
                if jtype == 'segment':
                    print("[Persistent worker] Processing segment job")
                    try:
                        client_id = job.get('client_id')
                        client_dir = job.get('client_dir')
                        raw_frames = job.get('raw_frames', [])
                        processed_frames = job.get('processed_frames', [])
                        task_id = job.get('task_id')
                        # 调用原先的落盘逻辑（复用函数），但这里直接写文件并调用外部接口
                        # 为简洁性，直接把原先 _flush_segment_if_needed 的实现重用但以传入的数据为准
                        self._do_persist_segment(client_id, client_dir, raw_frames, processed_frames, task_id)
                    except Exception as e:
                        print(f"Persistent worker segment job failed: {e}")
                elif jtype == 'alarm':
                    print("[Persistent worker] Processing alarm job")
                    try:
                        alarm = job.get('alarm')
                        # 直接处理告警上报与数据库记录
                        self._handle_alarm_now(alarm)
                    except Exception as e:
                        print(f"Persistent worker alarm job failed: {e}")
                else:
                    print(f"Persistent worker unknown job type: {jtype}")

            except Exception as e:
                print(f"Persistent worker loop error: {e}")

        print("Persistent worker stopped")

    def _do_persist_segment(self, client_id: str, hls_dir: Path, raw_frames_data: List[FrameData], processed_frames_data: List[FrameData], task_id: Optional[Any] = None):
        """实际在持久化线程中执行的写盘与上报逻辑（从原 _flush_segment_if_needed 拆分）。"""
        try:
            start_ts = processed_frames_data[0].timestamp
            end_ts = processed_frames_data[-1].timestamp

            # 生成原始视频段
            raw_segment_path = hls_dir / f"raw_segment_{int(start_ts * 1e6)}.mp4"
            height, width = raw_frames_data[0].frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*'mp4v') # type: ignore
            out_raw = cv2.VideoWriter(str(raw_segment_path), fourcc, 30.0, (width, height))
            for fd in raw_frames_data:
                out_raw.write(fd.frame)
            out_raw.release()

            # 生成处理后视频段
            segment_path = hls_dir / f"processed_segment_{int(start_ts * 1e6)}.mp4"
            height, width = processed_frames_data[0].frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*'mp4v') # type: ignore
            out_processed = cv2.VideoWriter(str(segment_path), fourcc, 30.0, (width, height))
            for fd in processed_frames_data:
                out_processed.write(fd.frame)
            out_processed.release()

            # 写 keypoints JSON
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

            # 更新播放列表
            raw_playlist_path = hls_dir / "raw_playlist.m3u8"
            segment_duration = len(raw_frames_data) / 30.0
            if not raw_playlist_path.exists():
                with raw_playlist_path.open('w') as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
            with raw_playlist_path.open('a') as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{raw_segment_path.name}\n")

            playlist_path = hls_dir / "processed_playlist.m3u8"
            segment_duration = len(processed_frames_data) / 30.0
            if not playlist_path.exists():
                with playlist_path.open('w') as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
            with playlist_path.open('a') as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{segment_path.name}\n")

            # 尝试上报 file_path（网络 IO），并包含 task_id（如果存在）
            print("[Persistent worker] Posting file paths")
            try:
                insert_url = settings.file_path_insert_url or f"http://116.204.65.72:8881/gdmp/v1/api/nt/file_path_insert"
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
                            if attempt < retries:
                                time.sleep(backoff)
                                backoff *= 2
                            else:
                                return None, str(e)

                # 尝试解析 task_id 为整数（若可用）
                try:
                    t_id = int(task_id) if task_id is not None and str(task_id).isdigit() else None
                except Exception:
                    t_id = None

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
                post_json(insert_url, payload_raw)
                post_json(insert_url, payload_processed)
            except Exception as e:
                print(f"Persistent worker file_path post failed: {e}")

        except Exception as e:
            print(f"_do_persist_segment error: {e}")

    def _send_alarm_report(self, task_id: int, step_id: int, alarm_type: str, alarm_level: str, alarm_message: str, detection_result: Optional[Dict]=None, camera_ip: Optional[str]=None, reader_ip: Optional[str]=None, alarm_count: Optional[int]=None, first_seen: Optional[str]=None, last_seen: Optional[str]=None) -> Optional[bool]:
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

    def _enqueue_segment_job(self, client_id: str, client_queues: ClientQueues, seg_len: int) -> None:
        """将指定长度的段落写盘任务放入持久化队列。

        该方法不会检查长度阈值，只负责按给定 seg_len 弹出队列并入队 job。
        """
        # 构造目录信息
        client_dir = self._db_dir / client_id
        task_id = client_queues.get_task_id()
        task_dir = client_dir / str(task_id)
        hls_dir = task_dir / "hls"
        hls_dir.mkdir(parents=True, exist_ok=True)

        # 从 client_queues 中弹出对应帧（pop_n_* 会自动限制数量）
        raw_frames_data: List[FrameData] = client_queues.pop_n_ca_raw(seg_len)
        processed_frames_data: List[FrameData] = client_queues.pop_n_ca_processed(seg_len)

        if not raw_frames_data or not processed_frames_data:
            return

        try:
            self._persist_queue.put({
                "type": "segment",
                "client_id": client_id,
                "task_id": task_id,
                "client_dir": hls_dir,
                "raw_frames": raw_frames_data,
                "processed_frames": processed_frames_data
            })
        except Exception as e:
            print(f"Failed to enqueue segment persist job for {client_id}: {e}")

    def _flush_all_remaining_segments(self, client_id: str, client_queues: ClientQueues) -> None:
        """在任务/客户端结束时，将剩余缓存（包括未达阈值的部分）全部落盘。

        - 先按正常段长反复落盘完整段；
        - 再将最后不足一个段长的残余部分也落为一个段。
        """
        try:
            seg_len = client_queues.ca_segment_len
            # 1. 先处理所有完整段
            while client_queues.has_enough_for_segment(seg_len):
                self._enqueue_segment_job(client_id, client_queues, seg_len)

            # 2. 再处理最后不足一个段长的残余
            remaining_raw = len(client_queues.ca_raw)
            remaining_processed = len(client_queues.ca_processed)
            final_len = min(remaining_raw, remaining_processed)

            if final_len > 0:
                self._enqueue_segment_job(client_id, client_queues, final_len)
        except Exception as e:
            print(f"_flush_all_remaining_segments error for {client_id}: {e}")

    def _flush_segment_if_needed(self, client_id: str, client_queues:ClientQueues):
        """当队列达到阈值时，生成原始和处理后的 HLS 视频段及关键点 JSON。"""
        # 检查阈值并将待写盘数据放入持久化队列，由持久化线程执行实际写盘/上报/落库工作
        # print(f"Checking HLS segment flush for client: {client_id}")
        seg_len = client_queues.ca_segment_len
        if not client_queues.has_enough_for_segment(seg_len):
            return

        print(f"Enqueueing HLS segment persist job for client: {client_id}")
        # 已入队给持久化线程处理（播放列表更新、DB 写入等），本函数返回
        self._enqueue_segment_job(client_id, client_queues, seg_len)
        return

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
                # 尝试批量取若干帧以利用 GPU batch
                batch: List[FrameData] = []
                with self._lock:
                    # 从 client_queues 弹出最多 batch_size 帧
                    batch = client_queues.pop_n_ca_ready(self._batch_size)

                if not batch:
                    continue

                timestamps = [bd.timestamp for bd in batch]
                frames = [bd.frame for bd in batch]

                # 推理前把原始帧副本放入 ca_raw
                with self._lock:
                    for bd in batch:
                        client_queues.append_ca_raw(bd)

                try:
                    # 使用 TaskPipeline 进行批量推理：
                    # 这里只驱动流水线填充内部 cache，不直接构造结果帧，
                    # 主进程随后从 TaskPipeline 的四个缓存中读取标准化输出。
                    self._execute_inference_pipeline_batch(frames, timestamps, client_queues.get_task(), client_id=client_id)

                    # 将 TaskPipeline 缓存中新增的 FrameData 映射回客户端队列
                    with self._lock:
                        self._drain_pipeline_caches_to_client(client_id, client_queues)

                except Exception as e:
                    print(f"批量推理异常 for {client_id}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

                # 达到阈值时生成 HLS 段（将落盘交给持久化线程）
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
        # 启动持久化线程（写盘/告警持久化）
        if self._persist_thread is None or not getattr(self._persist_thread, 'is_alive', lambda: False)():
            try:
                self._persist_thread = threading.Thread(target=self._persistent_worker, daemon=True, name="PersistThread")
                self._persist_thread.start()
            except Exception as e:
                print(f"Failed to start persistent worker thread: {e}")

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
        # 停止并等待持久化线程
        if self._persist_thread is not None:
            try:
                self._persist_thread.join(timeout=1.0)
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
            client_queues.set_task(task)
            print(f"任务已设置 for client {client_id}: {task}")
            return True

    def terminate_task_by_id(self, client_id: str) -> bool:
        """终止指定客户端的任务，清理所有队列和资源。

        Args:
            client_id: 客户端ID

        Returns:
            是否成功终止
        """
        # 先获取队列引用，用于在清理前落盘缓存
        with self._lock:
            client_queues = self._clients.get(client_id)
        if client_queues is None:
            return False

        # 在真正清理前，将该客户端当前所有缓存（包括未达阈值部分）全部落盘
        try:
            self._flush_all_remaining_segments(client_id, client_queues)
        except Exception as e:
            print(f"Failed to flush remaining segments when terminating task for {client_id}: {e}")

        # 再次进入锁范围，安全地清理队列和注册表
        with self._lock:
            client_queues = self._clients.get(client_id)
            if client_queues is None:
                return False

            # 清理所有队列与引用
            client_queues.clear()

            # 清理与该客户端关联的 TaskPipeline
            try:
                self._pipeline_registry.remove_pipelines_for_client(client_id)
            except Exception:
                pass

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
            return client_queues.get_task() if client_queues else None


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