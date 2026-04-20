"""多模型并行推理 Worker Pool（CUDA Stream 并行）。

完全解耦版本：使用 InferenceWorkflow 基类，不依赖 pipeline_base。

关键特性：
- 每个 stage 配置多个模型（基于 InferenceWorkflow）
- 每个模型绑定独立的 CUDA Stream，实现真正的并行推理
- 调用 InferenceWorkflow.infer_batch 接口
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.services.inference.workflows import Detector
from app.services.inference.data_models import DetectionOutput
from app.services.inference.models import InferenceRequest, InferenceResult
from app.utils.metrics import infer_failure_total, infer_latency_ms

logger = logging.getLogger(__name__)

# 可选：CUDA 支持
try:
    import torch

    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    logger.warning("torch not available, CUDA Stream disabled")


class MultiModelWorkerPool:
    """多模型并行推理 Worker Pool（CUDA Stream 并行）。

    完全解耦版本：
    - 使用 InferenceWorkflow 基类（不依赖 pipeline_base）
    - 每个模型绑定独立的 CUDA Stream
    - 支持批量推理加速
"""

    def __init__(
        self,
        stage: str,
        models: Sequence[Detector],
        use_cuda_stream: bool = True,
    ):
        """
        Args:
            stage: Stage 名称（LEAK/CLEAN）
            models: 该 stage 对应的模型列表（基于 InferenceWorkflow）
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

        logger.info(
            f"MultiModelWorkerPool initialized: stage={stage}, models={len(self.models)}, "
            f"CUDA_stream={'enabled' if self.use_cuda_stream else 'disabled'}"
        )

    def infer_batch(self, batch: List[InferenceRequest]) -> List[InferenceResult]:
        """批量推理：多个模型并行执行。

        Args:
            batch: 同一 stage 的推理请求列表

        Returns:
            推理结果列表
            
        数据流说明：
            1. 提取 frames 和 contexts
            2. 调用各 model.infer_batch() → List[DetectionOutput]
            3. 组装为 InferenceResult
               InferenceResult.result = {
                   task_name: DetectionOutput(  # 检测输出对象
                       detections=[...],
                       metadata={...},
                       timestamp=...,
                       success=True,
                       ...
                   )
               }
        """
        if not batch:
            return []

        n = len(batch)
        frames = [req.frame for req in batch]

        # 构造上下文（每帧一个）
        contexts = [{"client_id": req.client_id} for req in batch]

        # 并行执行所有模型的 infer_batch
        if self.use_cuda_stream:
            # CUDA Stream 并行版本
            model_results = self._infer_batch_parallel_cuda(frames, contexts)
        else:
            # 顺序推理版本
            model_results = self._infer_batch_sequential(frames, contexts)

        # 构造输出：将每帧的 model_results 关联到对应的客户端
        # model_results[i] = {task_name: DetectionOutput}
        results: List[InferenceResult] = []
        for i, req in enumerate(batch):
            per_frame_results = model_results[i] if i < len(model_results) else {}

            result = InferenceResult(
                client_id=req.client_id,
                timestamp=req.timestamp,
                stage=req.stage,
                result=per_frame_results,
            )
            results.append(result)

        return results

    def _infer_batch_parallel_cuda(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]],
    ) -> List[Dict[str, DetectionOutput]]:
        """CUDA Stream 并行推理。

        核心思路：
        - 为每个模型启动异步推理（使用独立 CUDA Stream）
        - 使用 torch.cuda.synchronize() 等待所有 stream 完成
        - 合并结果
        
        返回值说明：
            List[Dict[str, DetectionOutput]]
            即：List[{task_name: DetectionOutput(...)}]
        """
        if not TORCH_AVAILABLE:
            return self._infer_batch_sequential(frames, contexts)

        # 启动异步推理
        async_results: List[tuple[str, List[DetectionOutput]]] = []

        for model, cuda_stream in zip(self.models, self.cuda_streams):
            if not model.enabled:
                continue

            with torch.cuda.stream(cuda_stream):
                start_time = time.time()
                try:
                    # 调用 InferenceWorkflow.infer_batch （已经返回 DetectionOutput 格式）
                    batch_res = model.infer_batch(frames, contexts)
                    async_results.append((model.name, batch_res))

                    # 记录推理延迟（成功）
                    elapsed_ms = (time.time() - start_time) * 1000
                    infer_latency_ms.labels(model=model.name).observe(
                        elapsed_ms / len(frames)
                    )  # 平均每帧延迟

                except Exception as e:
                    # 业务逻辑层不应该捕获异常 - 让异常传播到Boundary Layer 1
                    # 但为了兼容性和防止单个模型失败影响其他模型，这里保留异常捕获
                    logger.error(
                        f"Model {model.name} inference failed: {e}", exc_info=True
                    )

                    # 记录推理失败
                    infer_failure_total.labels(
                        model=model.name,
                        error_type=type(e).__name__,
                    ).inc()

                    # 返回失败结果
                    n = len(frames)
                    failed: List[DetectionOutput] = [
                        DetectionOutput(
                            detections=[],
                            metadata={"error": str(e)},
                            timestamp=time.time(),
                            success=False,
                            error=str(e)
                        )
                        for _ in range(n)
                    ]
                    async_results.append((model.name, failed))

        # 同步所有 CUDA Stream
        torch.cuda.synchronize()

        # 合并结果：将每个模型的结果按帧索引组织
        n = len(frames)
        merged: List[Dict[str, DetectionOutput]] = [{} for _ in range(n)]

        for model_name, batch_res in async_results:
            for i in range(min(len(batch_res), n)):
                merged[i][model_name] = batch_res[i]

        return merged

    def _infer_batch_sequential(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]],
    ) -> List[Dict[str, DetectionOutput]]:
        """顺序推理（不使用 CUDA Stream）。"""
        n = len(frames)
        merged: List[Dict[str, DetectionOutput]] = [{} for _ in range(n)]

        for model in self.models:
            if not model.enabled:
                continue

            start_time = time.time()
            try:
                # 调用 InferenceTask.infer_batch （已经返回 DetectionOutput 格式）
                batch_res = model.infer_batch(frames, contexts)
                for i in range(min(len(batch_res), n)):
                    merged[i][model.name] = batch_res[i]

                # 记录推理延迟（成功）
                elapsed_ms = (time.time() - start_time) * 1000
                infer_latency_ms.labels(model=model.name).observe(
                    elapsed_ms / n
                )  # 平均每帧延迟

            except Exception as e:
                logger.error("[MultiModelWorkerPool] %s infer_batch error: %s", model.name, e, exc_info=True)

                # 记录推理失败
                infer_failure_total.labels(
                    model=model.name,
                    error_type=type(e).__name__,
                ).inc()

                for i in range(n):
                    merged[i][model.name] = DetectionOutput(
                        detections=[],
                        metadata={"error": str(e)},
                        timestamp=time.time(),
                        success=False,
                        error=str(e)
                    )

        return merged

    def warmup(self, batch_size: int = 1) -> None:
        """模型预热：执行dummy推理以消除冷启动延迟。

        Args:
            batch_size: 预热批次大小，默认1（建议与实际batch_size一致）

        工作原理：
        1. 生成dummy输入（与真实输入shape一致）
        2. 执行一次完整的并行推理流程
        3. 触发模型加载、CUDA内核编译、显存分配
        4. 丢弃预热结果
        """
        import time

        logger.info(f"Starting model warmup for stage {self.stage}...")

        # 生成dummy输入（640x480 BGR图像）
        dummy_frames = [
            np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(batch_size)
        ]
        dummy_contexts = [{} for _ in range(batch_size)]

        start_time = time.time()

        try:
            # 执行一次完整推理（触发模型加载和CUDA初始化）
            if self.use_cuda_stream:
                results = self._infer_batch_parallel_cuda(dummy_frames, dummy_contexts)
            else:
                results = self._infer_batch_sequential(dummy_frames, dummy_contexts)

            elapsed = time.time() - start_time

            logger.info(
                f"Model warmup completed for stage {self.stage}: "
                f"elapsed={elapsed*1000:.1f}ms, models={len(self.models)}"
            )

            # 清理显存缓存（可选，避免预热占用过多显存）
            if TORCH_AVAILABLE and torch.cuda.is_available():
                torch.cuda.empty_cache()

        except Exception as e:
            logger.error(
                f"Model warmup failed for stage {self.stage}: {e}", exc_info=True
            )
