"""多模型并行推理 Worker Pool（CUDA Stream 并行）。

完全解耦版本：使用 InferenceTask 基类，不依赖 pipeline_base。

关键特性：
- 每个 stage 配置多个模型（基于 InferenceTask）
- 每个模型绑定独立的 CUDA Stream，实现真正的并行推理
- 调用 InferenceTask.infer_batch 接口
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.services.inference.models import InferenceRequest, InferenceResult
from app.services.infer_task import InferenceTask

# 可选：CUDA 支持
try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    print("[MultiModelWorkerPool] torch not available, CUDA Stream disabled")


class MultiModelWorkerPool:
    """多模型并行推理 Worker Pool（CUDA Stream 并行）。

    完全解耦版本：
    - 使用 InferenceTask 基类（不依赖 pipeline_base）
    - 每个模型绑定独立的 CUDA Stream
    - 支持批量推理加速
    """

    def __init__(
        self,
        stage: str,
        models: Sequence[InferenceTask],
        use_cuda_stream: bool = True,
    ):
        """
        Args:
            stage: Stage 名称（LEAK/CLEAN）
            models: 该 stage 对应的模型列表（基于 InferenceTask）
            use_cuda_stream: 是否使用 CUDA Stream 并行（True 推荐）
        """
        self.stage = stage
        self.models = list(models)
        self.use_cuda_stream = (
            use_cuda_stream and TORCH_AVAILABLE and torch.cuda.is_available()
        )

        # 为每个模型分配 CUDA Stream
        self.cuda_streams: List[Optional[Any]] = []
        if self.use_cuda_stream:
            for _ in self.models:
                self.cuda_streams.append(torch.cuda.Stream())
        else:
            self.cuda_streams = [None] * len(self.models)

        print(
            f"[MultiModelWorkerPool] 初始化 stage={stage}, models={len(self.models)}, "
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

        # 构造上下文（每帧一个）
        contexts = []
        for req in batch:
            # 尝试从 frame_data 获取 task 信息（如果可用）
            task_obj = None
            if hasattr(req.frame_data, 'inference_result') and req.frame_data.inference_result:
                task_obj = req.frame_data.inference_result.get('task')

            ctx = {
                "client_id": req.client_id,
                "stage": req.stage,
                "task": task_obj,  # 可能为 None
                "results": {},  # 存储各模型的结果
            }
            contexts.append(ctx)

        # 并行执行所有模型的 infer_batch
        if self.use_cuda_stream:
            # CUDA Stream 并行版本
            model_results = self._infer_batch_parallel_cuda(frames, contexts)
        else:
            # 顺序推理版本
            model_results = self._infer_batch_sequential(frames, contexts)

        # 构造输出：将每帧的 model_results 关联到对应的客户端
        results: List[InferenceResult] = []
        for i, req in enumerate(batch):
            per_frame_results = model_results[i] if i < len(model_results) else {}

            result = InferenceResult(
                client_id=req.client_id,
                timestamp=req.timestamp,
                stage=req.stage,
                result=per_frame_results,
                annotated_frame=None,  # 先不做可视化，留给后处理
                frame=req.frame,  # 保存原始帧供可视化使用
            )
            results.append(result)

        return results

    def _infer_batch_parallel_cuda(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]],
    ) -> List[Dict[str, Dict[str, Any]]]:
        """CUDA Stream 并行推理。

        核心思路：
        - 为每个模型启动异步推理（使用独立 CUDA Stream）
        - 使用 torch.cuda.synchronize() 等待所有 stream 完成
        - 合并结果
        """
        if not TORCH_AVAILABLE:
            return self._infer_batch_sequential(frames, contexts)

        # 启动异步推理
        async_results: List[tuple[str, List[Dict[str, Any]]]] = []

        for model, cuda_stream in zip(self.models, self.cuda_streams):
            if not model.enabled:
                continue

            with torch.cuda.stream(cuda_stream):
                try:
                    # 调用 InferenceTask.infer_batch
                    batch_res = model.infer_batch(frames, contexts)
                    async_results.append((model.name, batch_res))
                except Exception as e:
                    print(
                        f"[MultiModelWorkerPool] {model.name} infer_batch error: {e}"
                    )
                    # 返回失败结果
                    n = len(frames)
                    failed = [{"success": False, "error": str(e)} for _ in range(n)]
                    async_results.append((model.name, failed))

        # 同步所有 CUDA Stream
        torch.cuda.synchronize()

        # 合并结果：将每个模型的结果按帧索引组织
        n = len(frames)
        merged: List[Dict[str, Dict[str, Any]]] = [{} for _ in range(n)]

        for model_name, batch_res in async_results:
            for i in range(min(len(batch_res), n)):
                merged[i][model_name] = batch_res[i]

        return merged

    def _infer_batch_sequential(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]],
    ) -> List[Dict[str, Dict[str, Any]]]:
        """顺序推理（不使用 CUDA Stream）。"""
        n = len(frames)
        merged: List[Dict[str, Dict[str, Any]]] = [{} for _ in range(n)]

        for model in self.models:
            if not model.enabled:
                continue

            try:
                # 调用 InferenceTask.infer_batch
                batch_res = model.infer_batch(frames, contexts)
                for i in range(min(len(batch_res), n)):
                    merged[i][model.name] = batch_res[i]
                    # 更新 context，供后续依赖的模型使用
                    contexts[i]["results"][model.name] = batch_res[i]
            except Exception as e:
                print(f"[MultiModelWorkerPool] {model.name} infer_batch error: {e}")
                for i in range(n):
                    merged[i][model.name] = {"success": False, "error": str(e)}

        return merged
