"""
基于进程池的FFmpeg解码器模块

使用multiprocessing.Pool管理多个解码进程,充分利用16核CPU性能。
每个进程独立运行FFmpeg拉流和解码任务。
"""

import subprocess
import multiprocessing as mp
from multiprocessing import Queue, Process
from multiprocessing.synchronize import Event as EventType
import numpy as np
import cv2
import os
import time
from typing import Dict, Optional, Callable
import traceback
import threading


# 全局配置
PROCESS_POOL_SIZE = 16  # 针对16核CPU优化
MODEL_INPUT_WIDTH = int(os.environ.get('MODEL_INPUT_WIDTH', 0))
MODEL_INPUT_HEIGHT = int(os.environ.get('MODEL_INPUT_HEIGHT', 0))
MODEL_INPUT_COLOR = os.environ.get('MODEL_INPUT_COLOR', 'bgr').lower()


def _find_ffmpeg() -> Optional[str]:
    """查找FFmpeg可执行文件路径"""
    # 1) 环境变量指定的路径
    env_path = os.environ.get('FFMPEG_PATH')
    if env_path and os.path.exists(env_path):
        return env_path

    # 2) 系统PATH
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, 
                              timeout=2)
        if result.returncode == 0:
            return 'ffmpeg'
    except Exception:
        pass

    # 3) 常见Windows Chocolatey路径
    choco_path = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
    if os.path.exists(choco_path):
        return choco_path

    return None


def _standardize_frame(frame: np.ndarray) -> Optional[np.ndarray]:
    """标准化帧格式为HxWx3的uint8 numpy数组"""
    if frame is None:
        return None

    # 转为numpy数组
    if not isinstance(frame, np.ndarray):
        frame = np.array(frame)

    # 灰度转BGR
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

    # 移除alpha通道
    if frame.shape[2] == 4:
        frame = frame[:, :, :3]

    # 确保uint8类型
    if frame.dtype != np.uint8:
        try:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        except Exception:
            frame = frame.astype(np.uint8, copy=False)

    # 可选缩放
    if MODEL_INPUT_WIDTH > 0 and MODEL_INPUT_HEIGHT > 0:
        try:
            frame = cv2.resize(frame, 
                             (MODEL_INPUT_WIDTH, MODEL_INPUT_HEIGHT), 
                             interpolation=cv2.INTER_LINEAR)
        except Exception:
            pass

    # 可选颜色空间转换
    if MODEL_INPUT_COLOR == 'rgb':
        try:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception:
            pass

    # 确保内存连续
    return np.ascontiguousarray(frame)


def _decoder_worker(worker_id: int,
                   task_queue: Queue,
                   frame_queue: Queue,
                   stop_event):
    """
    通用解码器工作进程
    
    Args:
        worker_id: 工作进程ID
        task_queue: 任务输入队列（接收解码任务）
        frame_queue: 帧输出队列（发送给主进程）
        stop_event: 停止事件
    """
    print(f"[DecoderWorker-{worker_id}] 进程启动 (PID: {os.getpid()})")
    
    ffmpeg_path = _find_ffmpeg()
    if not ffmpeg_path:
        print(f"[DecoderWorker-{worker_id}] ❌ 未找到FFmpeg")
        return
    
    print(f"[DecoderWorker-{worker_id}] ✓ FFmpeg可用: {ffmpeg_path}")
    
    try:
        while not stop_event.is_set():
            # 等待新任务
            task = None
            try:
                task = task_queue.get(timeout=1.0)
            except:
                # 队列为空超时，继续等待
                continue
            
            if task is None:  # 毒丸信号，退出工作进程
                print(f"[DecoderWorker-{worker_id}] 收到停止信号")
                break
            
            try:
                client_id = task['client_id']
                stream_url = task['stream_url']
                protocol = task['protocol']
                fps = task['fps']
                
                print(f"[DecoderWorker-{worker_id}] 接收任务: {client_id} ({protocol})")
                
                # 构建FFmpeg命令
                cmd = [ffmpeg_path]
                
                # if protocol == "RTSP":
                #     cmd += [
                #         "-rtsp_transport", "udp",
                #         "-analyzeduration", "10000000",
                #         "-probesize", "10000000",
                #         "-fflags", "nobuffer",
                #         "-flags", "low_delay",
                #         "-max_delay", "500000",
                #     ]
                
                # cmd += [
                #     "-i", stream_url,
                #     "-map", "0:v:0",
                #     "-f", "rawvideo",
                #     "-pix_fmt", "bgr24",
                #     "-vf", f"fps={fps},scale=640:480",
                #     "pipe:1"
                # ]
                # RTSP 低延迟输入参数（假设你前面已经根据 protocol 加过）
                cmd += [
                    "-rtsp_transport", "udp",
                    "-fflags", "nobuffer",
                    "-flags", "low_delay",
                    "-analyzeduration", "1000000",
                    "-probesize", "1000000",
                ]

                # 视频输入 + 输出到 pipe
                cmd += [
                    "-i", stream_url,
                    "-map", "0:v:0",
                    "-vsync", "drop",
                    "-vf", f"scale=640:480,fps={fps}",
                    "-pix_fmt", "bgr24",
                    "-f", "rawvideo",
                    "pipe:1"
                ]
                
                frame_count = 0
                frame_size = 640 * 480 * 3
                
                print(f"[DecoderWorker-{worker_id}] 启动FFmpeg for {client_id}")
                
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0
                )
                
                print(f"[DecoderWorker-{worker_id}] FFmpeg进程启动 (PID: {process.pid})")
                
                # 等待FFmpeg初始化
                time.sleep(2)
                if process.poll() is not None:
                    stderr_output = process.stderr.read().decode('utf-8', errors='ignore')  # type: ignore
                    print(f"[DecoderWorker-{worker_id}] ❌ FFmpeg提前退出: {stderr_output[:500]}")
                    continue  # 跳过这个任务，继续等待下一个
                
                buffer = b''
                
                # 解码循环
                while not stop_event.is_set() and process.poll() is None:
                    try:
                        # 检查是否有新任务（意味着应该停止当前任务）
                        try:
                            new_task = task_queue.get_nowait()
                            if new_task is None:
                                stop_event.set()
                                break
                            # 有新任务，停止当前FFmpeg，将新任务放回队列
                            task_queue.put(new_task)
                            print(f"[DecoderWorker-{worker_id}] 检测到新任务，停止当前解码")
                            break
                        except:
                            pass  # 没有新任务，继续当前解码
                        
                        # 读取数据块
                        chunk = process.stdout.read(32768)  # type: ignore
                        if len(chunk) == 0:
                            print(f"[DecoderWorker-{worker_id}] 流结束 ({client_id})")
                            break
                        
                        buffer += chunk
                        
                        # 解析完整帧
                        while len(buffer) >= frame_size:
                            frame_data = buffer[:frame_size]
                            buffer = buffer[frame_size:]
                            
                            # 重塑为图像
                            frame = np.frombuffer(frame_data, dtype=np.uint8).reshape((480, 640, 3))
                            std_frame = _standardize_frame(frame)
                            
                            if std_frame is not None:
                                # 发送到队列（非阻塞，队列满时丢弃旧帧）
                                try:
                                    frame_queue.put((client_id, std_frame), block=False)
                                    frame_count += 1
                                    
                                    if frame_count % 30 == 0:
                                        print(f"[DecoderWorker-{worker_id}] 已解码 {frame_count} 帧 ({client_id})")
                                except:
                                    # 队列满，丢弃帧
                                    pass
                                
                    except Exception as e:
                        print(f"[DecoderWorker-{worker_id}] 处理帧错误: {e}")
                        break
                
                # 清理FFmpeg进程
                try:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)
                except:
                    try:
                        process.kill()
                    except:
                        pass
                
                print(f"[DecoderWorker-{worker_id}] 任务完成: {client_id}, 共解码 {frame_count} 帧")
                
            except Exception as task_error:
                print(f"[DecoderWorker-{worker_id}] 任务处理错误: {task_error}")
                traceback.print_exc()
                time.sleep(0.1)
    
    except Exception as e:
        print(f"[DecoderWorker-{worker_id}] 异常: {e}")
        traceback.print_exc()
    
    print(f"[DecoderWorker-{worker_id}] 进程停止")


class DecoderPool:
    """
    FFmpeg解码器进程池管理器（通用池模式）
    
    维护一个固定大小的工作进程池，进程不绑定特定客户端。
    通过任务队列分配解码任务，空闲进程自动获取新任务。
    """
    
    def __init__(self, max_workers: int = PROCESS_POOL_SIZE):
        """
        初始化解码器进程池
        
        Args:
            max_workers: 工作进程数
        """
        self.max_workers = max_workers
        self.task_queue = Queue()  # 任务队列
        self.frame_queue = Queue(maxsize=1000)  # 共享帧队列
        self.workers: list = []  # 工作进程列表
        self.stop_event = mp.Event()  # 全局停止事件
        self.active_tasks: Dict[str, dict] = {}  # client_id -> task信息
        self._running = False
        
        print(f"[DecoderPool] 初始化通用进程池，工作进程数: {max_workers}")
        
        # 启动工作进程池
        self._start_workers()
    
    def _start_workers(self):
        """启动所有工作进程"""
        print(f"[DecoderPool] 启动 {self.max_workers} 个工作进程...")
        
        for i in range(self.max_workers):
            process = Process(
                target=_decoder_worker,
                args=(i, self.task_queue, self.frame_queue, self.stop_event),
                name=f"DecoderWorker-{i}",
                daemon=True
            )
            
            process.start()
            self.workers.append({
                'id': i,
                'process': process,
                'started_at': time.time()
            })
            
            print(f"[DecoderPool] 工作进程 {i+1}/{self.max_workers} 已启动 (PID: {process.pid})")
        
        self._running = True
        
        # 短暂等待，验证进程启动成功
        time.sleep(0.5)
        alive = sum(1 for w in self.workers if w['process'].is_alive())
        print(f"[DecoderPool] 进程池就绪，{alive}/{self.max_workers} 个进程存活")
    
    def start_decoder(self, 
                     client_id: str,
                     stream_url: str,
                     protocol: str = "RTSP",
                     fps: int = 30) -> bool:
        """
        提交解码任务到进程池
        
        Args:
            client_id: 客户端ID（用于标识输出帧）
            stream_url: 流地址
            protocol: 协议类型（RTMP/RTSP）
            fps: 目标帧率
            
        Returns:
            是否成功提交任务
        """
        if client_id in self.active_tasks:
            print(f"[DecoderPool] 客户端 {client_id} 已有活跃任务")
            return False
        
        if not self._running:
            print(f"[DecoderPool] 进程池未运行")
            return False
        
        # 构建任务
        task = {
            'client_id': client_id,
            'stream_url': stream_url,
            'protocol': protocol,
            'fps': fps,
            'submitted_at': time.time()
        }
        
        # 提交到任务队列
        try:
            self.task_queue.put(task, block=False)
            self.active_tasks[client_id] = task
            print(f"[DecoderPool] 提交任务: {client_id} -> {stream_url}")
            return True
        except:
            print(f"[DecoderPool] 任务队列已满")
            return False
    
    def stop_decoder(self, client_id: str) -> bool:
        """
        停止指定客户端的解码任务
        
        注意：由于采用通用进程池，无法直接停止特定任务。
        只能标记任务为已停止，工作进程会在完成当前循环后检测到。
        
        Args:
            client_id: 客户端ID
            
        Returns:
            是否成功标记停止
        """
        if client_id not in self.active_tasks:
            print(f"[DecoderPool] 客户端 {client_id} 无活跃任务")
            return False
        
        # 从活跃任务中移除
        del self.active_tasks[client_id]
        print(f"[DecoderPool] 标记停止任务: {client_id}")
        
        # 注意：工作进程可能仍在处理该任务，直到检测到任务队列中的新任务或超时
        return True
    
    def get_frame(self, timeout: float = 0.1) -> Optional[tuple]:
        """
        从队列获取解码后的帧
        
        Args:
            timeout: 超时时间（秒）
            
        Returns:
            (client_id, frame) 或 None
        """
        try:
            return self.frame_queue.get(timeout=timeout)
        except:
            return None
    
    def stop_all(self):
        """停止所有工作进程"""
        print(f"[DecoderPool] 停止所有工作进程...")
        
        self._running = False
        
        # 清空活跃任务
        self.active_tasks.clear()
        
        # 发送停止信号
        self.stop_event.set()
        
        # 向任务队列发送毒丸信号
        for _ in range(self.max_workers):
            try:
                self.task_queue.put(None, block=False)
            except:
                pass
        
        # 等待所有进程结束
        for worker in self.workers:
            try:
                worker['process'].join(timeout=5.0)
                if worker['process'].is_alive():
                    print(f"[DecoderPool] 强制终止工作进程 {worker['id']}")
                    worker['process'].terminate()
                    worker['process'].join(timeout=2.0)
                    if worker['process'].is_alive():
                        worker['process'].kill()
            except Exception as e:
                print(f"[DecoderPool] 停止工作进程 {worker['id']} 错误: {e}")
        
        print(f"[DecoderPool] 已停止所有工作进程")
    
    def get_stats(self) -> dict:
        """获取进程池统计信息"""
        alive_count = sum(1 for w in self.workers if w['process'].is_alive())
        
        return {
            "total_workers": len(self.workers),
            "alive_workers": alive_count,
            "active_tasks": len(self.active_tasks),
            "task_queue_size": self.task_queue.qsize(),
            "frame_queue_size": self.frame_queue.qsize(),
            "tasks": list(self.active_tasks.keys())
        }


# 全局进程池实例
_decoder_pool: Optional[DecoderPool] = None


def get_decoder_pool() -> DecoderPool:
    """获取全局解码器进程池实例（单例模式）"""
    global _decoder_pool
    if _decoder_pool is None:
        _decoder_pool = DecoderPool(max_workers=PROCESS_POOL_SIZE)
    return _decoder_pool


def shutdown_decoder_pool():
    """关闭全局解码器进程池"""
    global _decoder_pool
    if _decoder_pool is not None:
        _decoder_pool.stop_all()
        _decoder_pool = None


class FrameDispatcher:
    """
    帧分发器：从解码器进程池获取帧并分发到AI服务
    
    在独立线程中运行，持续从解码器进程池的共享队列中获取帧，
    并调用AI服务的submit_frame方法进行处理。
    """
    
    def __init__(self, decoder_pool: DecoderPool, frame_callback: Callable[[str, np.ndarray], None]):
        """
        初始化帧分发器
        
        Args:
            decoder_pool: 解码器进程池实例
            frame_callback: 帧回调函数，签名为 (client_id, frame) -> None
        """
        self.decoder_pool = decoder_pool
        self.frame_callback = frame_callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
    def start(self):
        """启动帧分发器"""
        if self._running:
            print("[FrameDispatcher] 已在运行")
            return
        
        self._running = True
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="FrameDispatcher",
            daemon=True
        )
        self._thread.start()
        print("[FrameDispatcher] 已启动")
    
    def stop(self):
        """停止帧分发器"""
        if not self._running:
            return
        
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        print("[FrameDispatcher] 已停止")
    
    def _dispatch_loop(self):
        """分发循环：持续获取帧并分发"""
        print("[FrameDispatcher] 分发循环启动")
        
        while self._running:
            try:
                # 从解码器进程池获取帧
                result = self.decoder_pool.get_frame(timeout=0.1)
                
                if result is not None:
                    client_id, frame = result
                    # 调用回调函数处理帧
                    try:
                        self.frame_callback(client_id, frame)
                    except Exception as e:
                        print(f"[FrameDispatcher] 处理帧回调错误 ({client_id}): {e}")
                        
            except Exception as e:
                print(f"[FrameDispatcher] 分发循环错误: {e}")
                time.sleep(0.01)
        
        print("[FrameDispatcher] 分发循环退出")


# 全局帧分发器实例
_frame_dispatcher: Optional[FrameDispatcher] = None


def start_frame_dispatcher(frame_callback: Callable[[str, np.ndarray], None]):
    """
    启动全局帧分发器
    
    Args:
        frame_callback: 帧回调函数
    """
    global _frame_dispatcher
    if _frame_dispatcher is None:
        decoder_pool = get_decoder_pool()
        _frame_dispatcher = FrameDispatcher(decoder_pool, frame_callback)
    
    _frame_dispatcher.start()


def stop_frame_dispatcher():
    """停止全局帧分发器"""
    global _frame_dispatcher
    if _frame_dispatcher is not None:
        _frame_dispatcher.stop()


import threading
