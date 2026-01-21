"""多模型并行推理 Worker Pool（CUDA Stream 并行）。

关键特性：
- 每个 stage 配置多个模型（2-3个）
- 每个模型绑定独立的 CUDA Stream，实现真正的并行推理
- 调用 SubtaskPipelineBase.infer_batch 接口
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch

from app.services.inference.models import InferenceRequest, InferenceResult
from app.services.pipeline_base import SubtaskPipelineBase


class MultiModelWorkerPool:
    """多模型并行推理 Worker Pool（CUDA Stream 并行）。

    关键特性：
    - 每个 stage 配置多个模型（2-3个）
    - 每个模型绑定独立的 CUDA Stream，实现真正的并行推理
    - 调用 SubtaskPipelineBase.infer_batch 接口
    """

    def __init__(
        self,
        stage: str,
        subtasks: Sequence[SubtaskPipelineBase],
        use_cuda_stream: bool = True,
    ):
        """
        Args:
            stage: Stage 名称（LEAK/CLEAN）
            subtasks: 该 stage 对应的子任务列表（例如 [BubbleSubtask, BendingSubtask]）
            use_cuda_stream: 是否使用 CUDA Stream 并行（True 推荐）
        """
        self.stage = stage
        self.subtasks = list(subtasks)
        self.use_cuda_stream = use_cuda_stream and torch.cuda.is_available()

        # 为每个子任务分配 CUDA Stream
        self.cuda_streams: List[Optional[torch.cuda.Stream]] = []
        if self.use_cuda_stream:
            for _ in self.subtasks:
                self.cuda_streams.append(torch.cuda.Stream())
        else:
            self.cuda_streams = [None] * len(self.subtasks)

        print(
            f"[MultiModelWorkerPool] 初始化 stage={stage}, subtasks={len(self.subtasks)}, "
            f"CUDA Stream={'enabled' if self.use_cuda_stream else 'disabled'}"
        )

    def infer_batch(self, batch: List[InferenceRequest]) -> List[InferenceResult]:
        """批量推理：多个模型并行执行。

        Args:
            batch: 同一 stage 的推理请求列表

        Returns:
            推理结果列表
        """
        if not batch:
            return []

        n = len(batch)
        frames = [req.frame for req in batch]
        timestamps = [req.timestamp for req in batch]

        # 并行执行所有子任务的 infer_batch
        subtask_results_list: List[Dict[str, Dict[str, Any]]] = []

        if self.use_cuda_stream:
            # CUDA Stream 并行版本
            subtask_results_list = self._infer_batch_parallel_cuda(frames, timestamps)
        else:
            # 顺序推理版本
            subtask_results_list = self._infer_batch_sequential(frames, timestamps)

        # 构造输出：将每帧的 subtask_results 关联到对应的客户端
        results: List[InferenceResult] = []
        for i, req in enumerate(batch):
            subtask_results = (
                subtask_results_list[i] if i < len(subtask_results_list) else {}
            )

            result = InferenceResult(
                client_id=req.client_id,
                timestamp=req.timestamp,
                stage=req.stage,
                result=subtask_results,
                annotated_frame=None,  # 先不做可视化，留给后处理
            )
            results.append(result)

        return results

    def _infer_batch_parallel_cuda(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[Dict[str, Dict[str, Any]]]:
        """CUDA Stream 并行推理。

        核心思路：
        - 为每个 subtask 启动异步推理（使用独立 CUDA Stream）
        - 使用 torch.cuda.synchronize() 等待所有 stream 完成
        - 合并结果
        """
        # 启动异步推理
        async_results: List[Tuple[str, List[Dict[str, Any]]]] = []

        for subtask, cuda_stream in zip(self.subtasks, self.cuda_streams):
            with torch.cuda.stream(cuda_stream):
                # 调用 subtask.infer_batch（内部会调用 YOLO.infer_batch）
                try:
                    batch_res = subtask.infer_batch(
                        frames, timestamps, prev_stage_cache=None
                    )
                    async_results.append((subtask.name, batch_res))
                except Exception as e:
                    print(
                        f"[MultiModelWorkerPool] {subtask.name} infer_batch error: {e}"
                    )
                    # 返回失败结果
                    n = len(frames)
                    failed = [{"success": False, "error": str(e)} for _ in range(n)]
                    async_results.append((subtask.name, failed))

        # 同步所有 CUDA Stream
        torch.cuda.synchronize()

        # 合并结果：将每个子任务的结果按帧索引组织
        n = len(frames)
        merged: List[Dict[str, Dict[str, Any]]] = [{} for _ in range(n)]

        for subtask_name, batch_res in async_results:
            for i in range(min(len(batch_res), n)):
                merged[i][subtask_name] = batch_res[i]

        return merged

    def _infer_batch_sequential(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[Dict[str, Dict[str, Any]]]:
        """顺序推理（不使用 CUDA Stream）。"""
        n = len(frames)
        merged: List[Dict[str, Dict[str, Any]]] = [{} for _ in range(n)]

        for subtask in self.subtasks:
            try:
                batch_res = subtask.infer_batch(
                    frames, timestamps, prev_stage_cache=None
                )
                for i in range(min(len(batch_res), n)):
                    merged[i][subtask.name] = batch_res[i]
            except Exception as e:
                print(f"[MultiModelWorkerPool] {subtask.name} infer_batch error: {e}")
                for i in range(n):
                    merged[i][subtask.name] = {"success": False, "error": str(e)}

        return merged
