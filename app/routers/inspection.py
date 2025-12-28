from fastapi import APIRouter, WebSocket, HTTPException, Query
from pydantic import BaseModel
from app.services import ai_manager
import threading
import cv2
import time
import subprocess
import numpy as np
import os
from typing import Dict
import tempfile
from urllib.parse import urlparse
from pathlib import Path

router = APIRouter(prefix="/inspection", tags=["inspection"])

# 存储正在运行的 RTMP 捕获线程
_capture_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}


class RTMPStreamConfig(BaseModel):
    """RTMP 流配置"""
    client_id: str
    rtmp_url: str
    fps: int = 30  # 固定帧率


class RTSPStreamConfig(BaseModel):
    """RTSP 流配置"""
    client_id: str
    rtsp_url: str
    fps: int = 30  # 固定帧率
 


def _stream_capture_worker(client_id: str, stream_url: str, fps: int, stop_event: threading.Event, protocol: str = "RTMP"):
    """通用流捕获工作线程，支持 RTMP 和 RTSP 协议。

    注意：`stream_url` 是发布者上传（publish）的地址，本工作线程会从该地址拉取（pull）流并交给 AI 服务处理。
    例如：客户端 publish 到 `rtsp://mediamtx:8554/live/cam1`，本后端以该 URL 为输入向 mediamtx/发布者拉流。
    """
    print(f"[{protocol} Worker] 启动捕获线程 for {client_id}: {stream_url}")
    
    # 查找 ffmpeg 可执行文件
    def _find_ffmpeg():
        # 优先尝试系统 PATH
        # 1) 环境变量指定的路径
        env_path = os.environ.get('FFMPEG_PATH')
        if env_path and os.path.exists(env_path):
            return env_path

        # 2) 系统 PATH
        try:
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=2)
            if result.returncode == 0:
                return 'ffmpeg'
        except Exception:
            pass

        # 3) 常见 Windows Chocolatey 路径
        choco_path = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
        if os.path.exists(choco_path):
            return choco_path

        return None
    
    ffmpeg_path = _find_ffmpeg()
    if not ffmpeg_path:
        print(f"[{protocol} Worker] ❌ 未找到 ffmpeg，无法捕获 {protocol} 流")
        return
    
    # 根据协议动态调整 ffmpeg 命令
    cmd = [ffmpeg_path]

    if protocol == "RTSP":
        cmd += [
            "-rtsp_transport", "udp",

            # 给FFmpeg足够时间拿到SPS/PPS和分辨率
            "-analyzeduration", "10000000",
            "-probesize", "10000000",

            # 低延迟
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-max_delay", "500000",
        ]

    cmd += [
        "-i", stream_url,

        # 强制选择视频流，避免误选音频
        "-map", "0:v:0",

        # rawvideo 输出
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",

        # 帧率 + 缩放
        "-vf", f"fps={fps},scale=640:480",

        "pipe:1"
    ]


    frame_count = 0
    frame_size = 640 * 480 * 3

    # 读取模型/推理期望的配置（可通过环境变量覆盖）
    MODEL_INPUT_WIDTH = int(os.environ.get('MODEL_INPUT_WIDTH', 0))  # 0 表示不强制缩放
    MODEL_INPUT_HEIGHT = int(os.environ.get('MODEL_INPUT_HEIGHT', 0))
    MODEL_INPUT_COLOR = os.environ.get('MODEL_INPUT_COLOR', 'bgr').lower()  # 'bgr' or 'rgb'

    def _standardize_frame(frm: np.ndarray) -> np.ndarray:
        """确保帧为 HxWx3 的 uint8 numpy 数组，并可选对颜色和大小做小范围转换。

        不修改原始帧的纵横比（若设置了 MODEL_INPUT_* 则会做简单缩放）。
        返回值保证为连续内存（C-order）。
        """
        if frm is None:
            return frm

        # 转为 numpy 数组并确保 dtype
        if not isinstance(frm, np.ndarray):
            frm = np.array(frm)

        # 如果是灰度，转成 BGR
        if frm.ndim == 2:
            frm = cv2.cvtColor(frm, cv2.COLOR_GRAY2BGR)

        # 如果有 alpha 通道，去掉
        if frm.shape[2] == 4:
            frm = frm[:, :, :3]

        # 保证 dtype 为 uint8
        if frm.dtype != np.uint8:
            # 试图缩放/裁剪到 uint8
            try:
                frm = np.clip(frm, 0, 255).astype(np.uint8)
            except Exception:
                frm = frm.astype(np.uint8, copy=False)

        # 可选缩放
        if MODEL_INPUT_WIDTH > 0 and MODEL_INPUT_HEIGHT > 0:
            try:
                frm = cv2.resize(frm, (MODEL_INPUT_WIDTH, MODEL_INPUT_HEIGHT), interpolation=cv2.INTER_LINEAR)
            except Exception:
                pass

        # 可选颜色空间调整（保持 BGR 为默认）
        if MODEL_INPUT_COLOR == 'rgb':
            try:
                frm = cv2.cvtColor(frm, cv2.COLOR_BGR2RGB)
            except Exception:
                pass

        # 确保内存连续
        frm = np.ascontiguousarray(frm)
        return frm
    
    print(f"[{protocol} Worker] 启动 ffmpeg: {' '.join(cmd)}")
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # 捕获错误信息
            bufsize=0
        )
        
        print(f"[{protocol} Worker] ffmpeg 进程已启动 (PID: {process.pid})")
        
        # 短暂等待看是否有立即的错误
        time.sleep(2)
        if process.poll() is not None:
            stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
            print(f"[{protocol} Worker] ffmpeg 进程提前退出 (退出码: {process.returncode})")
            print(f"[{protocol} Worker] 错误信息: {stderr_output}")
            return
        
        buffer = b''
        while not stop_event.is_set() and process.poll() is None:
            try:
                # 读取数据块
                chunk = process.stdout.read(32768)  # 32KB 缓冲区
                if len(chunk) == 0:
                    print(f"[{protocol} Worker] 接收到 0 字节数据，流结束")
                    break
                
                buffer += chunk
                
                # 检查是否有完整帧
                while len(buffer) >= frame_size:
                    frame_data = buffer[:frame_size]
                    buffer = buffer[frame_size:]
                    
                    frame = np.frombuffer(frame_data, dtype=np.uint8).reshape((480, 640, 3))
                    # 统一为推理期望的 numpy 格式（HxWx3, uint8, BGR 或 RGB 可选）
                    std_frame = _standardize_frame(frame)
                    ai_manager.submit_frame(client_id, std_frame)
                    frame_count += 1
                    
                    if frame_count % 30 == 0:  # 每秒报告一次 (假设30fps)
                        print(f"[{protocol} Worker] 已处理 {frame_count} 帧")
                        
            except Exception as e:
                print(f"[{protocol} Worker] 处理帧时出错: {e}")
                break
        
    except Exception as e:
        print(f"[{protocol} Worker] 异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'process' in locals():
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                else:
                    # 进程已退出，获取错误信息
                    if hasattr(process, 'stderr') and process.stderr:
                        try:
                            stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                            if stderr_output:
                                print(f"[{protocol} Worker] 进程错误信息: {stderr_output}")
                        except:
                            pass
            except:
                try:
                    process.kill()
                except:
                    pass
        # 无需清理临时 SDP（不再生成 SDP 文件）
        print(f"[{protocol} Worker] 停止，共处理 {frame_count} 帧")

@router.post("/start_rtmp_stream")
async def start_rtmp_stream(config: RTMPStreamConfig):
    """
    启动 RTMP 流捕获。
    
    POST /inspection/start_rtmp_stream
    Body: {"client_id": "xxx", "rtmp_url": "rtmp://localhost:1935/live/stream", "fps": 30}
    """
    client_id = config.client_id
    
    # 检查是否已经在运行
    if client_id in _capture_threads and _capture_threads[client_id].is_alive():
        raise HTTPException(status_code=400, detail=f"RTMP 流已在运行 for {client_id}")
    
    # 设置流地址（RTMP/RTSP 均使用通用接口）
    ai_manager.set_stream_url(client_id, config.rtmp_url)
    
    # 创建停止事件
    stop_event = threading.Event()
    _stop_events[client_id] = stop_event
    
    # 启动捕获线程
    thread = threading.Thread(
        target=_stream_capture_worker,
        args=(client_id, config.rtmp_url, config.fps, stop_event, "RTMP"),
        daemon=True,
        name=f"RTMPCapture-{client_id}"
    )
    _capture_threads[client_id] = thread
    thread.start()
    
    return {"status": "success", "message": f"RTMP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtmp_stream")
async def stop_rtmp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTMP 流捕获。
    
    POST /inspection/stop_rtmp_stream?client_id=xxx
    """
    if client_id not in _capture_threads:
        raise HTTPException(status_code=404, detail=f"未找到 RTMP 流 for {client_id}")
    
    # 发送停止信号
    if client_id in _stop_events:
        _stop_events[client_id].set()
    
    # 等待线程结束
    thread = _capture_threads[client_id]
    thread.join(timeout=2.0)
    
    # 清理
    _capture_threads.pop(client_id, None)
    _stop_events.pop(client_id, None)
    ai_manager.remove_client(client_id)
    
    return {"status": "success", "message": f"RTMP 流捕获已停止 for {client_id}"}


@router.post("/start_rtsp_stream")
async def start_rtsp_stream(config: RTSPStreamConfig):
    """
    启动 RTSP 流捕获。
    
    POST /inspection/start_rtsp_stream
    Body: {"client_id": "xxx", "rtsp_url": "rtsp://localhost:8554/live/stream", "fps": 30}
    """
    client_id = config.client_id
    
    # 检查是否已经在运行
    if client_id in _capture_threads and _capture_threads[client_id].is_alive():
        raise HTTPException(status_code=400, detail=f"RTSP 流已在运行 for {client_id}")
    
    # 设置流地址（RTMP/RTSP 均使用通用接口）
    ai_manager.set_stream_url(client_id, config.rtsp_url)
    
    # 创建停止事件
    stop_event = threading.Event()
    _stop_events[client_id] = stop_event
    
    # 启动捕获线程
    thread = threading.Thread(
        target=_stream_capture_worker,
        args=(client_id, config.rtsp_url, config.fps, stop_event, "RTSP"),
        daemon=True,
        name=f"RTSPCapture-{client_id}"
    )
    _capture_threads[client_id] = thread
    thread.start()
    
    return {"status": "success", "message": f"RTSP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTSP 流捕获。
    
    POST /inspection/stop_rtsp_stream?client_id=xxx
    """
    if client_id not in _capture_threads:
        raise HTTPException(status_code=404, detail=f"未找到 RTSP 流 for {client_id}")
    
    # 发送停止信号
    if client_id in _stop_events:
        _stop_events[client_id].set()
    
    # 等待线程结束
    thread = _capture_threads[client_id]
    thread.join(timeout=2.0)
    
    # 清理
    _capture_threads.pop(client_id, None)
    _stop_events.pop(client_id, None)
    ai_manager.remove_client(client_id)
    
    return {"status": "success", "message": f"RTSP 流捕获已停止 for {client_id}"}


@router.post("/stop_stream")
async def stop_stream(client_id: str):
    """
    通用流停止接口，同时支持 RTMP 和 RTSP。
    
    POST /inspection/stop_stream?client_id=xxx
    """
    if client_id not in _capture_threads:
        raise HTTPException(status_code=404, detail=f"未找到流 for {client_id}")
    
    # 发送停止信号
    if client_id in _stop_events:
        _stop_events[client_id].set()
    
    # 等待线程结束
    thread = _capture_threads[client_id]
    thread.join(timeout=2.0)
    
    # 清理
    _capture_threads.pop(client_id, None)
    _stop_events.pop(client_id, None)
    ai_manager.remove_client(client_id)
    
    return {"status": "success", "message": f"流捕获已停止 for {client_id}"}