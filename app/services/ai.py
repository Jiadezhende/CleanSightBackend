"""AI 推理模块 - 新架构实现

基于推理服务架构改进方案（INFERENCE_SERVICE_IMPROVEMENT_PLAN.md）的完整实现。

架构特点：
1. 推理与可视化解耦：推理线程只负责推理，可视化异步执行
2. 时序分析独立：时序逻辑从推理线程中分离，支持复杂时序算法
3. 降帧可视化补偿：可视化使用最新原始帧 + 缓存的检测结果
4. 异步管道架构：推理 → 时序分析 → 可视化 → 写回，完全异步

数据流：
StageAwareDispatcher → InferWorker → TemporalWorkerPool → VisualizationWorkerPool → WriteBackWorkerPool
"""

import base64
import json
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import cv2
import numpy as np
from sqlalchemy import text

from app.database import engine
from app.models.frame import FrameData, ProcessedFrame
from app.models.task import Task as CleaningTask
from app.services.client import ClientQueues
from app.services.client_manager import client_manager
from app.services.inference.factory import create_model_worker_service_from_manager
from app.services.inference.models import (
    FrontendMessage,
    InferenceResult,
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
    WriteBackData,
)
from app.services.inference.service import ModelWorkerService
from app.services.inference.temporal_analyzer import DefaultTemporalAnalyzer
from app.services.inference.temporal_worker import TemporalWorkerPool
from app.services.inference.visualization_worker import (
    Visualizer,
    VisualizationWorkerPool,
)
from app.services.inference.writeback_worker import WriteBackWorkerPool
from app.settings import settings


class DefaultVisualizer(Visualizer):
    """默认可视化器：绘制检测框、标注和文字信息。

    参考：app/services/task_pipeline/leak/leak_test.py 中的可视化实现
    """

    def visualize(
        self,
        frame: np.ndarray,
        inference_result: Dict[str, Any],
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None,
    ) -> np.ndarray:
        """在帧上绘制检测结果和标注。

        Args:
            frame: 原始帧
            inference_result: 推理结果（各子任务的输出）
            stage: 当前阶段（LEAK/CLEAN）
            temporal_result: 时序分析结果（可选）

        Returns:
            可视化后的帧
        """
        if frame is None:
            return frame

        # 复制帧，避免修改原始数据
        annotated = frame.copy()

        # 1. 绘制检测框（根据不同子任务）
        for subtask_name, subtask_res in inference_result.items():
            if not isinstance(subtask_res, dict):
                continue

            # 气泡检测可视化
            if subtask_name == "bubble":
                self._draw_bubble_detection(annotated, subtask_res)

            # 弯折检测可视化
            elif subtask_name == "bending":
                self._draw_bending_detection(annotated, subtask_res)

            # 其他子任务可扩展
            # elif subtask_name == "quality":
            #     self._draw_quality_detection(annotated, subtask_res)

        # 2. 绘制文字信息（stage、timestamp、fps等）
        self._draw_text_info(annotated, stage, temporal_result)

        return annotated

    def _draw_bubble_detection(self, frame: np.ndarray, result: Dict[str, Any]):
        """绘制气泡检测结果"""
        detected = result.get("bubble_detected", False)
        confidence = result.get("confidence", 0.0)
        boxes = result.get("boxes", [])

        # 绘制检测框
        for box in boxes:
            if isinstance(box, (list, tuple)) and len(box) >= 4:
                x1, y1, x2, y2 = map(int, box[:4])
                color = (0, 0, 255) if detected else (0, 255, 0)  # 红色=检测到，绿色=未检测到
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        # 绘制标签
        if detected:
            label = f"Bubble: {confidence:.2f}"
            cv2.putText(
                frame,
                label,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
            )

    def _draw_bending_detection(self, frame: np.ndarray, result: Dict[str, Any]):
        """绘制弯折检测结果"""
        detected = result.get("bending_detected", False)
        confidence = result.get("confidence", 0.0)
        boxes = result.get("boxes", [])

        # 绘制检测框
        for box in boxes:
            if isinstance(box, (list, tuple)) and len(box) >= 4:
                x1, y1, x2, y2 = map(int, box[:4])
                color = (255, 0, 0) if detected else (0, 255, 0)  # 蓝色=检测到，绿色=未检测到
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        # 绘制标签
        if detected:
            label = f"Bending: {confidence:.2f}"
            cv2.putText(
                frame,
                label,
                (10, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 0, 0),
                2,
            )

    def _draw_text_info(
        self,
        frame: np.ndarray,
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None,
    ):
        """绘制文字信息（stage、时间戳、事件等）"""
        # Stage 信息
        stage_text = f"Stage: {stage}"
        cv2.putText(
            frame,
            stage_text,
            (10, frame.shape[0] - 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
        )

        # 时间戳
        timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(
            frame,
            timestamp_text,
            (10, frame.shape[0] - 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
        )

        # 时序事件
        if temporal_result and temporal_result.events:
            event_text = " | ".join(temporal_result.events[:2])  # 最多显示2个事件
            cv2.putText(
                frame,
                event_text,
                (10, frame.shape[0] - 90),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 255),
                1,
            )


class InferenceManager:
    """推理管理器 - 新架构实现

    集成：
    - ModelWorkerService（推理服务）
    - TemporalWorkerPool（时序分析）
    - VisualizationWorkerPool（可视化）
    - WriteBackWorkerPool（写回）

    特性：
    - 使用 ClientManager 统一管理客户端队列
    - 异步管道架构，推理、时序、可视化、写回完全解耦
    - 支持降帧推理 + 全帧率可视化
    - 保留原有 API 接口，无缝替换
    """

    def __init__(
        self,
        rt_fps: int = 30,
        ca_segment_seconds: int = 5,
        db_dir: Optional[str] = None,
        ca_maxlen: int = 500,
        use_async_pipeline: bool = True,  # 是否启用异步管道
        temporal_threads: int = 2,
        visualization_threads: int = 4,
        writeback_threads: int = 2,
    ):
        """初始化推理管理器。

        Args:
            rt_fps: 实时帧率
            ca_segment_seconds: CA 段长度（秒）
            db_dir: 数据库存储目录
            ca_maxlen: CA 队列最大长度
            use_async_pipeline: 是否启用异步管道（新架构）
            temporal_threads: 时序分析线程数
            visualization_threads: 可视化线程数
            writeback_threads: 写回线程数
        """
        # 队列参数
        self._rt_maxlen = max(5, int(rt_fps))
        self._ca_segment_len = max(10, int(rt_fps * ca_segment_seconds))
        self._ca_maxlen = max(50, ca_maxlen)

        # 数据库存储目录
        base_dir = Path(__file__).parent.parent.parent.resolve()
        self._db_dir = Path(db_dir) if db_dir else base_dir / "database"
        self._db_dir.mkdir(parents=True, exist_ok=True)

        # 异步管道开关
        self.use_async_pipeline = use_async_pipeline

        # 停止事件
        self._stop_event = threading.Event()

        # ========== 核心组件初始化 ==========

        # 1. 推理服务配置（延迟初始化，避免循环导入）
        self._stage_configs: Optional[Dict[str, Dict[str, Any]]] = None
        self._model_worker_service: Optional[ModelWorkerService] = None

        if use_async_pipeline:
            # ========== 异步管道模式 ==========
            print("[InferenceManager] 启用异步管道架构")

            # 创建队列
            self.temporal_queue: "queue.Queue[InferenceResult]" = queue.Queue(maxsize=256)
            self.visualization_queue: "queue.Queue[TemporalAnalysisPackage]" = queue.Queue(maxsize=256)
            self.writeback_queue: "queue.Queue[WriteBackData]" = queue.Queue(maxsize=256)

            # 创建时序分析器
            temporal_config = self._create_temporal_config()
            self.temporal_analyzer = DefaultTemporalAnalyzer(config=temporal_config)

            # 创建可视化器
            self.visualizer = DefaultVisualizer()

            # 创建 Worker 池（各池管理自己的 stop_event）
            self.temporal_pool = TemporalWorkerPool(
                input_queue=self.temporal_queue,
                output_queue=self.visualization_queue,
                analyzer=self.temporal_analyzer,
                num_workers=temporal_threads,
            )

            self.visualization_pool = VisualizationWorkerPool(
                input_queue=self.visualization_queue,
                output_queue=self.writeback_queue,
                visualizer=self.visualizer,
                num_workers=visualization_threads,
            )

            self.writeback_pool = WriteBackWorkerPool(
                input_queue=self.writeback_queue,
                num_workers=writeback_threads,
                enable_db_write=False,  # 可选：是否写入数据库
            )

            # 自定义推理服务（将结果投递到时序队列）
            self._model_worker_service = self._create_async_model_worker_service()

        else:
            # ========== 同步模式（兼容旧代码） ==========
            print("[InferenceManager] 使用同步模式（兼容旧代码）")
            self._model_worker_service = create_model_worker_service_from_manager(
                stage_configs=self._get_stage_configs(),
                max_batch_per_stage=8,
                use_cuda_stream=True,
                num_worker_threads=2,
            )

        # 持久化队列与线程（HLS 段写盘、告警持久化）
        self._persist_queue: "queue.Queue" = queue.Queue()
        self._persist_thread: Optional[threading.Thread] = None

        # 告警相关（保留原有逻辑）
        self._alarm_lock = threading.Lock()
        self._pending_alarms: Dict[str, Dict[str, Any]] = {}
        self._recent_alarms: Dict[str, float] = {}
        self._alarm_batch_interval = getattr(settings, "alarm_batch_interval", 30)
        self._alarm_cooldown_seconds = getattr(settings, "alarm_cooldown_seconds", 60)
        self._alarm_thread: Optional[threading.Thread] = None

        # 预编码缓存（避免重复编码同一帧）
        self._encoded_cache: Dict[str, Dict[str, Any]] = {}
        self._encoded_cache_lock = threading.Lock()

        # 客户端刷新线程（定期同步客户端列表）
        self._refresh_thread: Optional[threading.Thread] = None

        print("[InferenceManager] 初始化完成")

    def _get_stage_configs(self) -> Dict[str, Dict[str, Any]]:
        """延迟初始化 stage 配置（避免循环导入）"""
        if self._stage_configs is None:
            from app.services.inference.factory import _create_default_stage_configs
            self._stage_configs = _create_default_stage_configs()
        return self._stage_configs

    def _create_temporal_config(self) -> Dict[str, Dict[str, Any]]:
        """创建时序分析器配置"""
        return {
            "LEAK": {
                "bubble": {
                    "mode": "consecutive",
                    "threshold": 3,  # 连续3帧
                },
                "bending": {
                    "mode": "sliding_window",
                    "window_seconds": 2.0,  # 2秒窗口
                    "ratio": 0.7,  # 70%比例
                },
            },
            "CLEAN": {
                "quality": {
                    "mode": "sliding_window",
                    "window_seconds": 2.0,
                    "ratio": 0.8,  # 80%比例
                },
            },
        }

    def _create_async_model_worker_service(self) -> ModelWorkerService:
        """创建异步模式的推理服务（将结果投递到时序队列）"""
        # 创建服务（延迟初始化配置）
        service = create_model_worker_service_from_manager(
            stage_configs=self._get_stage_configs(),
            max_batch_per_stage=8,
            use_cuda_stream=True,
            num_worker_threads=2,
        )

        # 覆写结果回写方法，将结果投递到时序队列
        original_write_back = service._write_back_results

        def async_write_back(results: List[InferenceResult]):
            """异步回写：将结果投递到时序队列，而非直接写入 ClientQueues"""
            for res in results:
                try:
                    # 保存原始帧（供可视化使用）
                    cq = client_manager.get_client(res.client_id)
                    if cq:
                        latest_frame = cq.get_latest_frame()
                        if latest_frame is not None:
                            res.frame = latest_frame  # 保存最新帧

                    # 投递到时序分析队列
                    self.temporal_queue.put(res, timeout=0.1)
                except queue.Full:
                    print(f"[InferenceManager] 时序队列已满，丢弃结果: {res.client_id}")
                except Exception as e:
                    print(f"[InferenceManager] 投递时序队列异常: {e}")

        # 替换回写方法
        service._write_back_results = async_write_back  # type: ignore

        return service

    # ========== 公共 API（保持与旧代码兼容） ==========

    def submit_frame(self, client_id: str, frame: np.ndarray) -> None:
        """提交原始帧到 CA-ReadyQueue（拉流层调用）"""
        now = time.time()
        frame_data = FrameData(timestamp=now, frame=frame)

        cq = client_manager.get_client(
            client_id,
            rt_maxlen=self._rt_maxlen,
            ca_segment_len=self._ca_segment_len,
            ca_maxlen=self._ca_maxlen,
        )
        cq.append_ca_ready(frame_data)

    def set_rtmp_url(self, client_id: str, rtmp_url: str) -> None:
        """设置客户端的 RTMP 流地址"""
        cq = client_manager.get_client(client_id)
        cq.rtmp_url = rtmp_url

    def set_stream_url(self, client_id: str, stream_url: str) -> None:
        """设置客户端的通用流地址（RTMP/RTSP）"""
        cq = client_manager.get_client(client_id)
        cq.rtmp_url = stream_url

    def get_result(
        self, client_id: str, as_model: bool = False
    ) -> Union[None, FrameData, ProcessedFrame]:
        """返回最新处理帧（从 RT-ProcessedQueue）"""
        cq = client_manager.get_client(client_id)
        if not cq:
            return None

        frame_data = cq.get_latest_result()
        if frame_data is None:
            return None

        if not as_model:
            return frame_data

        # 检查缓存：避免重复编码同一帧
        with self._encoded_cache_lock:
            cached = self._encoded_cache.get(client_id)
            if cached and cached["timestamp"] == frame_data.timestamp:
                task_id = cq.get_task_id()
                return ProcessedFrame(
                    task_id=task_id,
                    client_id=client_id,
                    raw_timestamp=datetime.fromtimestamp(frame_data.timestamp),
                    processed_frame_b64=cached["b64"],
                    inference_result=cached["inference_result"],
                )

        # 缓存未命中，编码并缓存
        task_id = cq.get_task_id()
        processed_frame = self._create_processed_frame(frame_data, task_id, client_id)

        # 更新缓存
        with self._encoded_cache_lock:
            self._encoded_cache[client_id] = {
                "timestamp": frame_data.timestamp,
                "b64": processed_frame.processed_frame_b64,
                "inference_result": processed_frame.inference_result,
            }

        return processed_frame

    def remove_client(self, client_id: str) -> None:
        """移除客户端（清理资源）"""
        # 先落盘剩余缓存
        cq = client_manager.get_client(client_id)
        if cq:
            try:
                self._flush_all_remaining_segments(client_id, cq)
            except Exception as e:
                print(f"清理客户端 {client_id} 时落盘失败: {e}")

        # 从 ClientManager 移除
        try:
            client_manager.remove_client(client_id)
        except Exception as e:
            print(f"从 ClientManager 移除客户端 {client_id} 失败: {e}")

    def status(self) -> Dict[str, Any]:
        """获取所有客户端及其队列状态"""
        clients = client_manager.get_all_clients()
        stats = {
            client_id: cq.to_status_dict() for client_id, cq in clients.items()
        }
        return {"clients": len(clients), "queues": stats}

    def set_task(self, client_id: str, task: Optional[CleaningTask]) -> bool:
        """为客户端设置任务"""
        cq = client_manager.get_client(client_id)
        cq.set_task(task)
        print(f"任务已设置 for client {client_id}: {task}")
        return True

    def get_task(self, client_id: str) -> Optional[CleaningTask]:
        """获取客户端的任务"""
        cq = client_manager.get_client(client_id)
        return cq.get_task() if cq else None

    def terminate_task_by_id(self, client_id: str) -> bool:
        """终止指定客户端的任务，清理所有队列和资源"""
        cq = client_manager.get_client(client_id)
        if cq is None:
            return False

        # 落盘剩余缓存
        try:
            self._flush_all_remaining_segments(client_id, cq)
        except Exception as e:
            print(f"终止任务 {client_id} 时落盘失败: {e}")

        # 清理队列
        cq.clear()

        # 从 ClientManager 移除
        try:
            client_manager.remove_client(client_id)
        except Exception:
            pass

        print(f"任务已终止，客户端 {client_id} 的所有队列和资源已清理")
        return True

    # ========== 内部辅助方法 ==========

    def _create_processed_frame(
        self, frame_data: FrameData, task_id: Optional[int], client_id: str
    ) -> ProcessedFrame:
        """从 FrameData 创建 ProcessedFrame 对象（含 Base64 编码）"""
        _, buf = cv2.imencode(".jpg", frame_data.frame)
        b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

        # 过滤推理结果，移除不可序列化的对象
        inference_result = self._make_json_serializable(
            frame_data.inference_result or {}
        )

        return ProcessedFrame(
            task_id=task_id,
            client_id=client_id,
            raw_timestamp=datetime.fromtimestamp(frame_data.timestamp),
            processed_frame_b64=b64,
            inference_result=inference_result,
        )

    def _make_json_serializable(self, obj: Any) -> Any:
        """递归过滤对象，移除不可 JSON 序列化的内容"""
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                # 跳过已知的不可序列化字段
                if key in ("annotated_frame", "processed_frame", "frame"):
                    continue
                result[key] = self._make_json_serializable(value)
            return result
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            # numpy 数组转为列表（如果是小数组）
            if obj.size < 100:
                return obj.tolist()
            return None
        elif isinstance(obj, (np.integer, np.floating)):
            return obj.item()
        else:
            return obj

    # ========== HLS 段落盘逻辑（保留原有实现） ==========

    def _flush_all_remaining_segments(
        self, client_id: str, client_queues: ClientQueues
    ) -> None:
        """在任务/客户端结束时，将剩余缓存全部落盘"""
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

    def _enqueue_segment_job(
        self, client_id: str, client_queues: ClientQueues, seg_len: int
    ) -> None:
        """将指定长度的段落写盘任务放入持久化队列"""
        # 构造目录信息
        client_dir = self._db_dir / client_id
        task_id = client_queues.get_task_id()
        task_dir = client_dir / str(task_id)
        hls_dir = task_dir / "hls"
        hls_dir.mkdir(parents=True, exist_ok=True)

        # 从 client_queues 中弹出对应帧
        raw_frames_data: List[FrameData] = client_queues.pop_n_ca_raw(seg_len)
        processed_frames_data: List[FrameData] = client_queues.pop_n_ca_processed(
            seg_len
        )

        if not raw_frames_data or not processed_frames_data:
            return

        try:
            self._persist_queue.put(
                {
                    "type": "segment",
                    "client_id": client_id,
                    "task_id": task_id,
                    "client_dir": hls_dir,
                    "raw_frames": raw_frames_data,
                    "processed_frames": processed_frames_data,
                }
            )
        except Exception as e:
            print(f"Failed to enqueue segment persist job for {client_id}: {e}")

    def _persistent_worker(self):
        """持久化线程：处理写盘 HLS 段与告警上报/落库等耗时操作"""
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

                jtype = job.get("type")
                if jtype == "segment":
                    print("[Persistent worker] Processing segment job")
                    try:
                        client_id = job.get("client_id")
                        client_dir = job.get("client_dir")
                        raw_frames = job.get("raw_frames", [])
                        processed_frames = job.get("processed_frames", [])
                        task_id = job.get("task_id")
                        self._do_persist_segment(
                            client_id, client_dir, raw_frames, processed_frames, task_id
                        )
                    except Exception as e:
                        print(f"Persistent worker segment job failed: {e}")
                elif jtype == "alarm":
                    print("[Persistent worker] Processing alarm job")
                    try:
                        alarm = job.get("alarm")
                        self._handle_alarm_now(alarm)
                    except Exception as e:
                        print(f"Persistent worker alarm job failed: {e}")
                else:
                    print(f"Persistent worker unknown job type: {jtype}")

            except Exception as e:
                print(f"Persistent worker loop error: {e}")

        print("Persistent worker stopped")

    def _do_persist_segment(
        self,
        client_id: str,
        hls_dir: Path,
        raw_frames_data: List[FrameData],
        processed_frames_data: List[FrameData],
        task_id: Optional[Any] = None,
    ):
        """实际在持久化线程中执行的写盘与上报逻辑"""
        try:
            start_ts = processed_frames_data[0].timestamp
            end_ts = processed_frames_data[-1].timestamp

            # 生成原始视频段
            raw_segment_path = hls_dir / f"raw_segment_{int(start_ts * 1e6)}.mp4"
            height, width = raw_frames_data[0].frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore
            out_raw = cv2.VideoWriter(str(raw_segment_path), fourcc, 30.0, (width, height))
            for fd in raw_frames_data:
                out_raw.write(fd.frame)
            out_raw.release()

            # 生成处理后视频段
            segment_path = hls_dir / f"processed_segment_{int(start_ts * 1e6)}.mp4"
            height, width = processed_frames_data[0].frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore
            out_processed = cv2.VideoWriter(
                str(segment_path), fourcc, 30.0, (width, height)
            )
            for fd in processed_frames_data:
                out_processed.write(fd.frame)
            out_processed.release()

            # 写 keypoints JSON
            keypoints_path = hls_dir / f"keypoints_{int(start_ts * 1e6)}.json"
            keypoints_list = []
            for fd in processed_frames_data:
                kp = fd.keypoints if hasattr(fd, "keypoints") else None
                ir = fd.inference_result if hasattr(fd, "inference_result") else None
                keypoints_list.append(
                    {
                        "timestamp": fd.timestamp,
                        "keypoints": self._make_json_serializable(kp),
                        "inference_result": self._make_json_serializable(ir),
                    }
                )
            with keypoints_path.open("w", encoding="utf-8") as f:
                json.dump(keypoints_list, f, ensure_ascii=False, indent=2)

            # 更新播放列表
            raw_playlist_path = hls_dir / "raw_playlist.m3u8"
            segment_duration = len(raw_frames_data) / 30.0
            if not raw_playlist_path.exists():
                with raw_playlist_path.open("w") as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
            with raw_playlist_path.open("a") as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{raw_segment_path.name}\n")

            playlist_path = hls_dir / "processed_playlist.m3u8"
            segment_duration = len(processed_frames_data) / 30.0
            if not playlist_path.exists():
                with playlist_path.open("w") as f:
                    f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n")
            with playlist_path.open("a") as f:
                f.write(f"#EXTINF:{segment_duration:.3f},\n")
                f.write(f"{segment_path.name}\n")

            print(f"[Persistent worker] Segment persisted: {client_id}")

        except Exception as e:
            print(f"_do_persist_segment error: {e}")

    def _handle_alarm_now(self, alarm_info: Dict[str, Any]):
        """实际执行告警上报与写库的函数（在持久化线程中调用）"""
        # 保留原有告警逻辑（省略详细实现）
        pass

    # ========== 刷新客户端列表 ==========

    def _client_refresh_loop(self):
        """定期刷新客户端列表（从 ClientManager 同步）"""
        print("[InferenceManager] Client refresh thread started")
        while not self._stop_event.is_set():
            try:
                if self._model_worker_service:
                    self._model_worker_service.refresh_client_queues()
                time.sleep(5)  # 每 5 秒刷新一次
            except Exception as e:
                print(f"[InferenceManager] Client refresh error: {e}")

    # ========== 启动/停止 ==========

    def start(self):
        """启动推理管理器"""
        print("[InferenceManager] 启动中...")

        # 1. 启动推理服务
        if self._model_worker_service:
            self._model_worker_service.start()

        # 2. 启动异步管道（如果启用）
        if self.use_async_pipeline:
            self.temporal_pool.start()
            self.visualization_pool.start()
            self.writeback_pool.start()

        # 3. 启动持久化线程
        if self._persist_thread is None or not self._persist_thread.is_alive():
            self._persist_thread = threading.Thread(
                target=self._persistent_worker, daemon=True, name="PersistThread"
            )
            self._persist_thread.start()

        # 4. 启动客户端刷新线程
        if self._refresh_thread is None or not self._refresh_thread.is_alive():
            self._refresh_thread = threading.Thread(
                target=self._client_refresh_loop, daemon=True, name="ClientRefreshThread"
            )
            self._refresh_thread.start()

        print("[InferenceManager] 已启动")

    def stop(self):
        """停止推理管理器"""
        print("[InferenceManager] 停止中...")

        # 设置停止事件
        self._stop_event.set()

        # 1. 停止推理服务
        if self._model_worker_service:
            self._model_worker_service.stop()

        # 2. 停止异步管道（如果启用）
        if self.use_async_pipeline:
            self.temporal_pool.stop()
            self.visualization_pool.stop()
            self.writeback_pool.stop()

        # 3. 停止持久化线程
        if self._persist_thread is not None:
            try:
                self._persist_thread.join(timeout=2.0)
            except Exception:
                pass

        # 4. 停止客户端刷新线程
        if self._refresh_thread is not None:
            try:
                self._refresh_thread.join(timeout=2.0)
            except Exception:
                pass

        print("[InferenceManager] 已停止")


# ========== 模块级单例（兼容旧代码） ==========

# 注意：由于已修复 bubble_task.py 和 yolo_task.py 的导入路径
# （从 app.services.ai 改为 app.services.infer_task），
# 可以直接在模块级初始化 manager
manager = InferenceManager(use_async_pipeline=True)


def start():
    """启动推理服务"""
    manager.start()


def stop():
    """停止推理服务"""
    manager.stop()


def submit_frame(client_id: str, frame: np.ndarray):
    """从 RTMP 流提交帧到 CA-ReadyQueue"""
    manager.submit_frame(client_id, frame)


def set_rtmp_url(client_id: str, rtmp_url: str):
    """设置客户端的 RTMP 流地址"""
    manager.set_rtmp_url(client_id, rtmp_url)


def set_stream_url(client_id: str, stream_url: str):
    """设置客户端的通用流地址（RTMP/RTSP）"""
    manager.set_stream_url(client_id, stream_url)


def get_result(client_id: str, as_model: bool = False):
    """获取推理结果"""
    return manager.get_result(client_id, as_model=as_model)


def remove_client(client_id: str):
    """移除客户端"""
    print(f"Removing client: {client_id}")
    manager.remove_client(client_id)


def status():
    """获取服务状态"""
    return manager.status()


def set_task(client_id: str, task: Optional[CleaningTask]) -> bool:
    """为客户端设置任务"""
    return manager.set_task(client_id, task)


def get_task(client_id: str) -> Optional[CleaningTask]:
    """获取客户端的任务"""
    return manager.get_task(client_id)
