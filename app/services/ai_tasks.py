import numpy as np


from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
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

    def infer_batch(self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]) -> List[InferenceResult]:
        """可选的批量推理接口，默认逐帧串行调用 `infer`。

        子类可覆盖以利用模型的 batch 接口（例如 YOLO 的 list-of-images 输入）。
        """
        results: List[InferenceResult] = []
        for f, ctx in zip(frames, contexts):
            results.append(self.infer(f, ctx))
        return results

    def requires_context(self) -> List[str]:
        """返回此任务依赖的其他任务名称列表。

        Returns:
            依赖的任务名称列表，空列表表示无依赖
        """
        return []


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