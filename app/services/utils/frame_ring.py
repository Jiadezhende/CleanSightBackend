"""跨进程帧传递的共享内存环：生产进程把帧拷进固定槽位，队列只传槽位号，消费进程原地读。

    ring = SharedFrameRing(slots=64, slot_bytes=640 * 480 * 3, prefix="csinfer")   # 主进程建
    slots = ring.acquire(len(frames))            # 不够返回 None（调用方按背压处理）
    ring.write(slots[0], frame)                  # 拷入；帧超过 slot_bytes 抛 ValueError
    msg = SharedFrames(ring.spec, slots, shapes) # 进队列的只有这个小对象
    ring.release(slots)                          # 消费方答复后归还；对端崩溃时 reset() 全量回收
    ring.close()                                 # 停机：close + unlink

    view = SharedFrameRingView(ring.spec)        # 消费进程按 spec 附着
    frame = view.read(slot, shape)               # 拷出一份独立数组（不持有共享内存视图）
    view.write(slot, result)                     # 请求-应答式：结果写回同一槽位，生产方收到答复再 read

槽位的生命周期归生产方：**消费方不归还**，生产方在收到对应答复（或判定对端已死）时归还。
依据见 docs/update/20261013_SHARED_FRAME_RING.md。
"""

from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import List, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class RingSpec:
    """附着一块环所需的全部信息（可 pickle，随 spawn 参数或消息传给消费进程）。"""

    name: str
    slots: int
    slot_bytes: int


@dataclass(frozen=True)
class SharedFrames:
    """一批帧在环里的位置：替代 `List[np.ndarray]` 进队列。"""

    spec: RingSpec
    slots: Tuple[int, ...]
    shapes: Tuple[Tuple[int, ...], ...]


class SharedFrameRing:
    """生产方持有的环：建 / 分配 / 拷入 / 归还 / 销毁。线程安全（acquire / release 带锁）。"""

    def __init__(self, slots: int, slot_bytes: int, *, prefix: str):
        if slots <= 0 or slot_bytes <= 0:
            raise ValueError(f"slots / slot_bytes 必须为正: {slots} / {slot_bytes}")
        name = f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._shm = shared_memory.SharedMemory(name=name, create=True, size=slots * slot_bytes)
        self.spec = RingSpec(name=self._shm.name, slots=slots, slot_bytes=slot_bytes)
        self._free = deque(range(slots))
        self._lock = threading.Lock()
        self._closed = False

    @property
    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    def fits(self, frame: np.ndarray) -> bool:
        return frame.dtype == np.uint8 and frame.nbytes <= self.spec.slot_bytes

    def acquire(self, n: int) -> Optional[List[int]]:
        """原子地取 n 个空槽；不够则一个都不取，返回 None。"""
        with self._lock:
            if self._closed or len(self._free) < n:
                return None
            return [self._free.popleft() for _ in range(n)]

    def release(self, slots: Sequence[int]) -> None:
        with self._lock:
            self._free.extend(slots)

    def reset(self) -> None:
        """全部槽位回收为空闲（消费进程已死、在途全部作废时用）。"""
        with self._lock:
            self._free = deque(range(self.spec.slots))

    def read(self, slot: int, shape: Tuple[int, ...]) -> np.ndarray:
        """拷出槽位内容（请求-应答式用法下读消费方写回的结果）。"""
        src = np.ndarray(shape, dtype=np.uint8, buffer=self._shm.buf,
                         offset=slot * self.spec.slot_bytes)
        return src.copy()

    def write(self, slot: int, frame: np.ndarray) -> None:
        if not self.fits(frame):
            raise ValueError(
                f"帧放不进槽位: dtype={frame.dtype} nbytes={frame.nbytes} slot_bytes={self.spec.slot_bytes}"
            )
        dst = np.ndarray(frame.shape, dtype=np.uint8, buffer=self._shm.buf,
                         offset=slot * self.spec.slot_bytes)
        dst[...] = frame

    def close(self) -> None:
        """停机：释放映射并 unlink。重复调用无害。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._shm.close()
        try:
            self._shm.unlink()
        except FileNotFoundError:
            pass


class SharedFrameRingView:
    """消费方附着的只读视图。"""

    def __init__(self, spec: RingSpec):
        self.spec = spec
        self._shm = shared_memory.SharedMemory(name=spec.name)

    def read(self, slot: int, shape: Tuple[int, ...]) -> np.ndarray:
        """拷出一份独立数组：消费方不持有共享内存视图，槽位被生产方复用也不受影响。"""
        src = np.ndarray(shape, dtype=np.uint8, buffer=self._shm.buf,
                         offset=slot * self.spec.slot_bytes)
        return src.copy()

    def read_all(self, frames: SharedFrames) -> List[np.ndarray]:
        return [self.read(s, shape) for s, shape in zip(frames.slots, frames.shapes)]

    def write(self, slot: int, frame: np.ndarray) -> None:
        """把结果写回同一槽位（请求-应答式用法：读出输入后原地写出结果，生产方收到答复再读）。"""
        if frame.dtype != np.uint8 or frame.nbytes > self.spec.slot_bytes:
            raise ValueError(
                f"帧放不进槽位: dtype={frame.dtype} nbytes={frame.nbytes} slot_bytes={self.spec.slot_bytes}"
            )
        dst = np.ndarray(frame.shape, dtype=np.uint8, buffer=self._shm.buf,
                         offset=slot * self.spec.slot_bytes)
        dst[...] = frame

    def close(self) -> None:
        self._shm.close()
