"""便捷工厂函数用于创建 ModelWorkerService。

提供简便的方式来创建和配置推理服务实例。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.services.inference.core.service import ModelWorkerService


def create_model_worker_service_from_manager(
    stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    max_batch_per_stage: int = 8,
    use_cuda_stream: bool = True,
    num_worker_threads: int = 2,
) -> ModelWorkerService:
    """从 ClientManager 创建 ModelWorkerService（推荐方式）。

    **重要**: 客户端是动态创建和清理的，推理服务会获取初始化时刻的客户端列表快照。
    当有新客户端加入或离开时，需要调用 `service.refresh_client_queues()` 来同步。

    使用方法：
    ```python
    from app.services.client import client_manager
    from app.services.inference import create_model_worker_service_from_manager
    import threading
    import time

    # 1. 创建服务（自动从 ClientManager 获取当前所有客户端）
    service = create_model_worker_service_from_manager()

    # 2. 启动推理服务
    service.start()

    # 3. 定期刷新客户端列表（推荐方式）
    def refresh_loop():
        while True:
            service.refresh_client_queues()
            time.sleep(5)  # 每 5 秒刷新一次

    threading.Thread(target=refresh_loop, daemon=True).start()

    # 或者在客户端添加/移除时手动刷新
    # new_client = client_manager.get_client("new_client_id")
    # service.refresh_client_queues()

    # 4. 停止服务（程序退出前）
    service.stop()
    ```

    客户端动态管理说明：
    - 新客户端加入: 需要调用 refresh_client_queues() 才能被推理服务识别
    - 客户端离开: 推理服务会自动跳过已清理的客户端，无需特殊处理
    - 推理过程中离开: 结果回写会安全检查，不会崩溃

    Args:
        stage_configs: Stage 配置（如果为 None，使用默认 LEAK 配置）
        max_batch_per_stage: 每个 stage 最大 batch 大小
        use_cuda_stream: 是否使用 CUDA Stream 并行
        num_worker_threads: 推理线程数

    Returns:
        ModelWorkerService 实例
    """
    # 如果没有提供 stage_configs，使用默认配置
    if stage_configs is None:
        stage_configs = _create_default_stage_configs()

    # 从 ClientManager 创建服务
    service = ModelWorkerService(
        client_queues_map=None,  # 自动从 ClientManager 获取
        stage_configs=stage_configs,
        max_batch_per_stage=max_batch_per_stage,
        use_cuda_stream=use_cuda_stream,
        num_worker_threads=num_worker_threads,
    )

    return service


def create_model_worker_service_example(
    client_queues_map: Dict[str, Any],
) -> ModelWorkerService:
    """示例：手动提供 client_queues_map 创建 ModelWorkerService。

    使用方法：
    ```python
    from app.services.client import ClientQueues
    from app.services.inference import create_model_worker_service_example

    # 1. 准备 ClientQueues map
    client_queues_map = {
        "client_1": ClientQueues(client_id="client_1"),
        "client_2": ClientQueues(client_id="client_2"),
        # ...
    }

    # 2. 创建服务
    service = create_model_worker_service_example(client_queues_map)

    # 3. 启动
    service.start()

    # 4. 停止（程序退出前）
    service.stop()
    ```
    """
    stage_configs = _create_default_stage_configs()

    # 创建服务
    service = ModelWorkerService(
        client_queues_map=client_queues_map,
        stage_configs=stage_configs,
        max_batch_per_stage=8,
        use_cuda_stream=True,
        num_worker_threads=2,
    )

    return service


def _create_default_stage_configs() -> Dict[str, Dict[str, Any]]:
    """创建默认的 Stage 配置（LEAK 阶段）。

    完全解耦版本：直接使用 InferenceTask，不依赖 pipeline_base。
    """
    import os

    from app.services.inference.config import load_stage_config
    from app.services.models.bending import EndoscopeBendingDetectionTask
    from app.services.models.bubble import BubbleDetectionTask

    # 从环境变量获取模型路径，如果未设置则使用默认值
    model_base_path = os.environ.get("CLEANSIGHT_MODEL_PATH", "./app/data")

    # 从配置文件读取 batch_size
    try:
        inference_config = load_stage_config()
        batch_size = inference_config.batch_size if inference_config else 4
    except Exception:
        batch_size = 4

    # 创建模型实例（基于 InferenceTask）
    bubble_task = BubbleDetectionTask(
        model_path=f"{model_base_path}/bubble-best.pt",
        conf_threshold=0.5,
        iou_threshold=0.45,
        enabled=True,
    )

    bending_task = EndoscopeBendingDetectionTask(
        model_path=f"{model_base_path}/bend-best.pt",
        conf_threshold=0.6,
        iou_threshold=0.45,
        enabled=True,
    )

    # Stage 配置（使用 InferenceTask，不再使用 SubtaskPipeline）
    stage_configs = {
        "LEAK": {
            "models": [bubble_task, bending_task],  # 直接传入 InferenceTask 实例
            "batch_size": batch_size,  # 从配置文件读取
        },
        # 可以添加更多 stage
        # "CLEAN": {
        #     "models": [...],
        #     "batch_size": batch_size,
        # },
    }

    return stage_configs
