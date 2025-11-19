import cv2
import time
import threading
import base64
import numpy as np
from typing import Optional, Dict


def infer(frame):
    """
    Mock 推理函数：给图像画一个矩形作为示例
    可替换为真实模型（YOLO、姿态估计等）
    """
    processed = frame.copy()
    h, w = processed.shape[:2]
    cx, cy = w // 2, h // 2
    rw, rh = min(200, w // 4), min(200, h // 4)
    x1 = max(0, cx - rw // 2)
    y1 = max(0, cy - rh // 2)
    x2 = min(w, cx + rw // 2)
    y2 = min(h, cy + rh // 2)

    cv2.rectangle(processed, (x1, y1), (x2, y2), (0, 0, 255), 2)
    cv2.putText(processed, 'MockDet', (x1, max(10, y1-10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)

    return processed


class InferenceManager:
    """按 client_id 管理最新输入帧与推理结果的简单管理器。
    设计要点：
    - 每个 client_id 只保留最新一帧和最新一结果，避免队列堆积
    - 使用单或少量推理线程顺序处理活跃客户端，节约资源以支持大量客户端（示例可扩展为批处理）
    """

    def __init__(self):
        self._frames: Dict[str, np.ndarray] = {}
        self._results: Dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def submit_frame(self, client_id: str, frame: np.ndarray):
        with self._lock:
            self._frames[client_id] = frame

    def submit_base64(self, client_id: str, base64_frame: str) -> str:
        try:
            frame_data = base64.b64decode(base64_frame)
            np_arr = np.frombuffer(frame_data, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is None:
                return "error: 无效的图像数据"
            self.submit_frame(client_id, img)
            return "success"
        except Exception as e:
            return f"error: {e}"

    def get_result(self, client_id: str):
        with self._lock:
            return self._results.get(client_id)

    def remove_client(self, client_id: str):
        with self._lock:
            self._frames.pop(client_id, None)
            self._results.pop(client_id, None)

    def status(self):
        with self._lock:
            return {
                "clients": len(self._frames),
                "results_cached": len(self._results)
            }

    def _inference_loop(self):
        print("AI 推理服务已启动（多客户端管理）")
        while not self._stop_event.is_set():
            # 复制当前活跃 client 列表，避免长时间持锁
            with self._lock:
                client_ids = list(self._frames.keys())

            if not client_ids:
                time.sleep(0.05)
                continue

            # 轮询每个客户端的最新帧进行处理
            for cid in client_ids:
                if self._stop_event.is_set():
                    break

                with self._lock:
                    frame = self._frames.pop(cid, None)

                if frame is None:
                    continue

                try:
                    processed = infer(frame)
                except Exception as e:
                    # 在实际项目中记录异常并继续
                    print(f"推理异常 for {cid}: {e}")
                    continue

                with self._lock:
                    self._results[cid] = processed

            # 微小休眠以避免占用全部 CPU
            time.sleep(0.001)

        print("AI 推理服务已停止（多客户端管理）")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._inference_loop, daemon=True, name="InferenceThread")
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


# 单例管理器（模块级）
manager = InferenceManager()


def start():
    manager.start()


def stop():
    manager.stop()


def submit_base64_frame(client_id: str, base64_frame: str) -> str:
    return manager.submit_base64(client_id, base64_frame)


def submit_frame(client_id: str, frame: np.ndarray):
    manager.submit_frame(client_id, frame)


def get_result(client_id: str):
    return manager.get_result(client_id)


def remove_client(client_id: str):
    manager.remove_client(client_id)


def status():
    return manager.status()