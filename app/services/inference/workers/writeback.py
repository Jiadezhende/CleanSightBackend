"""写回工作线程池。

职责：
- 从写回队列消费完整数据包
- 写入 ClientQueues（ca_processed、rt_processed）
- 写入数据库（可选，记录推理历史）
"""

import logging
import threading
from queue import Empty, Queue

from app.models.frame import FrameData
from app.services.client import client_manager
from app.services.inference.models import WriteBackData

logger = logging.getLogger(__name__)


class WriteBackWorker:
    """写回工作线程。"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：完整数据包
        stop_event: threading.Event,
        worker_id: int = 0,
        enable_db_write: bool = False,
    ):
        """初始化写回工作线程。

        Args:
            input_queue: 写回数据包队列
            stop_event: 停止事件
            worker_id: 工作线程ID（用于调试）
            enable_db_write: 是否启用数据库写入
        """
        self.input_queue = input_queue
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.enable_db_write = enable_db_write

    def run(self):
        """工作循环。"""
        logger.debug("[WriteBackWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取完整数据包
                try:
                    data: WriteBackData = self.input_queue.get(timeout=0.1)
                except Empty:
                    continue

                # 2. 安全检查：客户端可能已清理
                if not client_manager.has_client(data.client_id):
                    continue

                cq = client_manager.get_client(data.client_id)
                if cq is None:
                    continue

                # 3. 构造 FrameData
                frame_data = FrameData(
                    timestamp=data.timestamp,
                    frame=data.processed_frame,
                    inference_result=data.inference_result,
                    keypoints=None,  # 如果需要，从 inference_result 提取
                )

                # 4. 写入队列
                cq.append_ca_processed(frame_data)
                cq.append_rt_processed(frame_data)

                # 5. 写入数据库（可选）
                if self.enable_db_write:
                    self._write_to_database(data)

            except Exception as e:
                logger.error("[WriteBackWorker-%d] Exception: %s", self.worker_id, e, exc_info=True)

        logger.debug("[WriteBackWorker-%d] Stopped", self.worker_id)

    def _write_to_database(self, data: WriteBackData):
        """写入数据库（记录推理历史）。

        Args:
            data: 写回数据包

        TODO: 实现数据库写入逻辑
        可以记录：
        - 推理结果（JSON）
        - 时序事件
        - 前端消息
        - 可视化帧的存储路径（可选）
        """
        pass


class WriteBackWorkerPool:
    """写回线程池。"""

    def __init__(
        self,
        input_queue: Queue,
        num_workers: int = 2,
        enable_db_write: bool = False,
    ):
        """初始化写回线程池。

        Args:
            input_queue: 写回数据包队列
            num_workers: 工作线程数量
            enable_db_write: 是否启用数据库写入
        """
        self.input_queue = input_queue
        self.num_workers = num_workers
        self.enable_db_write = enable_db_write

        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def start(self):
        """启动线程池。"""
        for i in range(self.num_workers):
            worker = WriteBackWorker(
                input_queue=self.input_queue,
                stop_event=self._stop_event,
                worker_id=i,
                enable_db_write=self.enable_db_write,
            )

            thread = threading.Thread(
                target=worker.run,
                daemon=True,
                name=f"WriteBackWorker-{i}",
            )
            thread.start()
            self._workers.append(thread)

        logger.info("[WriteBackWorkerPool] Started %d workers", self.num_workers)

    def stop(self):
        """停止线程池。"""
        self._stop_event.set()

        for thread in self._workers:
            thread.join(timeout=2.0)

        logger.debug("[WriteBackWorkerPool] Stopped")
