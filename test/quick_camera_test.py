"""
电脑摄像头快速测试脚本

一键启动摄像头并测试 RTMP 流 + AI 推理的完整流程

使用方法:
    python test/quick_camera_test.py

需要先安装:
    pip install opencv-python requests websockets
"""

import cv2
import asyncio
import argparse
import requests
import websockets
import threading
import time
import sys
from datetime import datetime


class CameraStreamer:
    """摄像头流处理器"""
    
    def __init__(self, camera_id: int = 0, fps: int = 30):
        self.camera_id = camera_id
        self.fps = fps
        self.cap = None
        self.stop_event = threading.Event()
        self.frame_count = 0
        
    def start(self):
        """启动摄像头"""
        self.cap = cv2.VideoCapture(self.camera_id)
        if not self.cap.isOpened():
            raise RuntimeError(f"✗ 无法打开摄像头 {self.camera_id}")
        
        # 设置参数
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        print(f"✓ 摄像头已启动")
        print(f"  ID: {self.camera_id}")
        print(f"  分辨率: {width}x{height}")
        print(f"  目标帧率: {self.fps} FPS")
        
    def stream_to_backend(self, client_id: str):
        """直接将帧提交到后端 AI 服务（模拟 RTMP 流）"""
        frame_interval = 1.0 / self.fps
        last_capture_time = 0.0
        
        print(f"\n开始捕获帧...")
        print(f"按 'q' 键停止\n")
        
        while not self.stop_event.is_set():
            current_time = time.time()
            
            if current_time - last_capture_time >= frame_interval:
                if self.cap is None:
                    print("✗ 摄像头未启动")
                    break
                ret, frame = self.cap.read()
                if not ret:
                    print("✗ 摄像头读取失败")
                    time.sleep(0.1)
                    continue
                
                # 显示预览窗口
                cv2.imshow('Camera Preview - Press Q to quit', frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    self.stop_event.set()
                    break
                
                self.frame_count += 1
                if self.frame_count % 100 == 0:
                    elapsed = time.time() - last_capture_time * self.frame_count
                    actual_fps = self.frame_count / elapsed if elapsed > 0 else 0
                    print(f"已捕获 {self.frame_count} 帧 (实际 FPS: {actual_fps:.1f})")
                
                last_capture_time = current_time
            else:
                time.sleep(0.001)
    
    def stop(self):
        """停止摄像头"""
        self.stop_event.set()
        if self.cap:
            self.cap.release()
        cv2.destroyAllWindows()
        print(f"\n✓ 摄像头已停止，共捕获 {self.frame_count} 帧")


async def monitor_websocket(client_id: str, duration: int = 30):
    """监听 WebSocket 推送的实时推理结果"""
    uri = f"ws://localhost:8000/ai/video?client_id={client_id}"
    print(f"\n连接到 WebSocket: {uri}")
    
    frame_count = 0
    start_time = datetime.now()
    
    try:
        async with websockets.connect(uri) as websocket:
            print("✓ WebSocket 已连接，开始接收推理结果...\n")
            
            while (datetime.now() - start_time).seconds < duration:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    frame_count += 1
                    if frame_count % 30 == 0:
                        print(f"已接收 {frame_count} 帧推理结果")
                except asyncio.TimeoutError:
                    continue
                    
    except websockets.exceptions.WebSocketException as e:
        print(f"✗ WebSocket 连接失败: {e}")
        print("提示: 请确保后端服务正在运行且 RTMP 捕获已启动")
    except Exception as e:
        print(f"✗ WebSocket 错误: {e}")
    
    if frame_count > 0:
        print(f"\n✓ 总计接收 {frame_count} 帧推理结果")
    else:
        print(f"\n✗ 未接收到推理结果")


def check_backend():
    """检查后端服务是否运行"""
    try:
        response = requests.get("http://localhost:8000/", timeout=2)
        return response.status_code == 200
    except:
        return False


def check_status():
    """检查 AI 推理服务状态"""
    try:
        url = "http://localhost:8000/ai/status"
        response = requests.get(url, timeout=2)
        
        if response.status_code == 200:
            status = response.json()
            print(f"\n{'='*60}")
            print(f"AI 服务状态:")
            
            # 检查数据格式
            if not isinstance(status, dict):
                print(f"✗ 返回数据格式错误: {type(status)}")
                print(f"原始数据: {status}")
                return None
            
            # 安全获取客户端数量
            clients_count = status.get('clients', 0)
            print(f"  客户端数量: {clients_count}")
            
            # 安全获取队列信息
            queues = status.get('queues', {})
            if not queues:
                print(f"  当前无活跃客户端")
            else:
                for client_id, queue_info in queues.items():
                    print(f"  客户端 {client_id}:")
                    print(f"    CA-ReadyQueue: {queue_info.get('ca_ready', 0)} 帧")
                    print(f"    CA-RawQueue: {queue_info.get('ca_raw', 0)} 帧")
                    print(f"    CA-ProcessedQueue: {queue_info.get('ca_processed', 0)} 帧")
                    print(f"    RT-ProcessedQueue: {queue_info.get('rt_processed', 0)} 帧")
                    if queue_info.get('rtmp_url'):
                        print(f"    RTMP URL: {queue_info['rtmp_url']}")
            
            print(f"{'='*60}")
            return status
        else:
            print(f"✗ 状态查询失败 (HTTP {response.status_code})")
            print(f"响应内容: {response.text}")
            return None
    except requests.exceptions.ConnectionError:
        print(f"✗ 无法连接到后端服务 (http://localhost:8000)")
        print(f"请确保后端服务正在运行: uvicorn app.main:app --reload")
        return None
    except requests.exceptions.Timeout:
        print(f"✗ 请求超时")
        return None
    except KeyError as e:
        print(f"✗ 数据格式错误，缺少字段: {e}")
        print(f"完整响应: {response.text if 'response' in locals() else 'N/A'}")
        return None
    except Exception as e:
        print(f"✗ 无法获取状态: {e}")
        print(f"错误类型: {type(e).__name__}")
        if 'response' in locals():
            print(f"响应内容: {response.text}")
        return None


def start_rtmp_capture(client_id: str, rtmp_url: str, fps: int = 30):
    """启动 RTMP 流捕获"""
    try:
        url = "http://localhost:8000/inspection/start_rtmp_stream"
        payload = {
            "client_id": client_id,
            "rtmp_url": rtmp_url,
            "fps": fps
        }
        
        print(f"\n启动 RTMP 捕获...")
        print(f"  客户端: {client_id}")
        print(f"  RTMP: {rtmp_url}")
        print(f"  FPS: {fps}")
        
        response = requests.post(url, json=payload, timeout=5)
        
        if response.status_code == 200:
            print(f"✓ RTMP 捕获已启动")
            return True
        else:
            print(f"✗ 启动失败: {response.text}")
            return False
    except Exception as e:
        print(f"✗ 启动失败: {e}")
        return False


def stop_rtmp_capture(client_id: str):
    """停止 RTMP 流捕获"""
    try:
        url = f"http://localhost:8000/inspection/stop_rtmp_stream?client_id={client_id}"
        response = requests.post(url, timeout=5)
        
        if response.status_code == 200:
            print(f"\n✓ RTMP 捕获已停止")
        else:
            print(f"\n✗ 停止失败: {response.text}")
    except Exception as e:
        print(f"\n✗ 停止失败: {e}")


async def main():
    parser = argparse.ArgumentParser(description="电脑摄像头快速测试")
    parser.add_argument("--camera_id", type=int, default=0, help="摄像头 ID (0=默认)")
    parser.add_argument("--client_id", type=str, default="quick_test", help="客户端 ID")
    parser.add_argument("--rtmp_url", type=str, default="rtmp://localhost:1935/live/test", 
                        help="RTMP 流地址")
    parser.add_argument("--fps", type=int, default=30, help="捕获帧率")
    parser.add_argument("--duration", type=int, default=30, help="测试时长（秒）")
    parser.add_argument("--skip-rtmp", action="store_true", help="跳过 RTMP 捕获测试")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("CleanSight 摄像头快速测试")
    print("=" * 60)
    
    # 检查后端服务
    print("\n[1/4] 检查后端服务...")
    if not check_backend():
        print("✗ 后端服务未运行")
        print("\n请先启动后端服务:")
        print("  uvicorn app.main:app --reload")
        return
    print("✓ 后端服务正常")
    
    # 列出可用摄像头
    print("\n[2/4] 检测摄像头...")
    available_cameras = []
    for i in range(3):  # 检测前 3 个摄像头
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            available_cameras.append(i)
            cap.release()
    
    if not available_cameras:
        print("✗ 未检测到可用摄像头")
        return
    
    print(f"✓ 检测到摄像头: {available_cameras}")
    if args.camera_id not in available_cameras:
        print(f"✗ 指定的摄像头 {args.camera_id} 不可用")
        print(f"可用摄像头: {available_cameras}")
        return
    
    # 启动摄像头
    print(f"\n[3/4] 启动摄像头 {args.camera_id}...")
    camera = CameraStreamer(args.camera_id, args.fps)
    
    try:
        camera.start()
        
        # 启动 RTMP 捕获（如果指定了 RTMP URL）
        if not args.skip_rtmp and args.rtmp_url:
            print("\n[4/4] 启动 RTMP 捕获...")
            if start_rtmp_capture(args.client_id, args.rtmp_url, args.fps):
                await asyncio.sleep(2)
                
                # 检查初始状态
                check_status()
                
                # 在后台运行摄像头捕获
                capture_thread = threading.Thread(
                    target=camera.stream_to_backend,
                    args=(args.client_id,),
                    daemon=True
                )
                capture_thread.start()
                
                # 监听 WebSocket 推送
                print(f"\n开始监听推理结果 ({args.duration} 秒)...")
                try:
                    await monitor_websocket(args.client_id, args.duration)
                except KeyboardInterrupt:
                    print("\n用户中断")
                
                # 停止捕获
                camera.stop_event.set()
                capture_thread.join(timeout=2)
                stop_rtmp_capture(args.client_id)
                
                # 检查最终状态
                await asyncio.sleep(1)
                check_status()
        else:
            # 仅显示摄像头预览
            print("\n[4/4] 预览模式（未启用 RTMP）...")
            print(f"按 'q' 键停止")
            camera.stream_to_backend(args.client_id)
            
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"\n错误: {e}")
    finally:
        camera.stop()
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
    
    # 显示结果文件位置
    import os
    db_path = os.path.join(os.getcwd(), "database", args.client_id)
    if os.path.exists(db_path):
        print(f"\n生成的文件位置:")
        print(f"  {db_path}")
    else:
        print(f"\n提示: 完整测试需要启动 RTMP 服务器")
        print(f"  1. 下载 MediaMTX: https://github.com/bluenviron/mediamtx/releases")
        print(f"  2. 运行 mediamtx.exe")
        print(f"  3. 推流到 RTMP: ffmpeg -i ... -f flv rtmp://localhost:1935/live/test")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n程序已退出")
