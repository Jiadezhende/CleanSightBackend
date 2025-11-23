"""
测试 RTMP 流捕获和 AI 推理的集成脚本

使用方法:
1. 启动 RTMP 服务器 (如 nginx-rtmp 或 MediaMTX)
2. 推流到 RTMP 服务器
3. 运行此脚本启动捕获和推理
4. 通过 WebSocket 客户端查看实时结果

示例:
    python test/test_rtmp_integration.py --client_id camera_001 --rtmp_url rtmp://localhost:1935/live/test
"""

import asyncio
import argparse
import requests
import websockets
import json
from datetime import datetime


async def monitor_websocket(client_id: str, duration: int = 30):
    """监听 WebSocket 推送的实时推理结果"""
    uri = f"ws://localhost:8000/ai/video?client_id={client_id}"
    print(f"连接到 WebSocket: {uri}")
    
    frame_count = 0
    start_time = datetime.now()
    
    try:
        async with websockets.connect(uri) as websocket:
            print("WebSocket 已连接，开始接收推理结果...")
            
            while (datetime.now() - start_time).seconds < duration:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    frame_count += 1
                    if frame_count % 30 == 0:  # 每秒打印一次（假设 30 FPS）
                        print(f"已接收 {frame_count} 帧推理结果")
                except asyncio.TimeoutError:
                    print("等待推理结果...")
                    
    except Exception as e:
        print(f"WebSocket 错误: {e}")
    
    print(f"\n总计接收 {frame_count} 帧推理结果")


def start_rtmp_capture(client_id: str, rtmp_url: str, fps: int = 30):
    """启动 RTMP 流捕获"""
    url = "http://localhost:8000/inspection/start_rtmp_stream"
    payload = {
        "client_id": client_id,
        "rtmp_url": rtmp_url,
        "fps": fps
    }
    
    print(f"启动 RTMP 捕获: {client_id} <- {rtmp_url}")
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        print(f"✓ RTMP 捕获已启动: {response.json()}")
        return True
    else:
        print(f"✗ 启动失败: {response.text}")
        return False


def stop_rtmp_capture(client_id: str):
    """停止 RTMP 流捕获"""
    url = f"http://localhost:8000/inspection/stop_rtmp_stream?client_id={client_id}"
    
    print(f"\n停止 RTMP 捕获: {client_id}")
    response = requests.post(url)
    
    if response.status_code == 200:
        print(f"✓ RTMP 捕获已停止: {response.json()}")
    else:
        print(f"✗ 停止失败: {response.text}")


def check_status():
    """检查 AI 推理服务状态"""
    url = "http://localhost:8000/ai/status"
    response = requests.get(url)
    
    if response.status_code == 200:
        status = response.json()
        print(f"\nAI 服务状态:")
        print(f"  客户端数量: {status['clients']}")
        for client_id, queues in status['queues'].items():
            print(f"  客户端 {client_id}:")
            print(f"    CA-ReadyQueue: {queues.get('ca_ready', 0)} 帧")
            print(f"    CA-RawQueue: {queues.get('ca_raw', 0)} 帧")
            print(f"    CA-ProcessedQueue: {queues.get('ca_processed', 0)} 帧")
            print(f"    RT-ProcessedQueue: {queues.get('rt_processed', 0)} 帧")
            print(f"    RTMP URL: {queues.get('rtmp_url', 'N/A')}")
        return status
    else:
        print(f"✗ 状态查询失败: {response.text}")
        return None


async def main():
    parser = argparse.ArgumentParser(description="测试 RTMP 流捕获和 AI 推理集成")
    parser.add_argument("--client_id", type=str, default="test_camera", help="客户端 ID")
    parser.add_argument("--rtmp_url", type=str, required=True, help="RTMP 流地址")
    parser.add_argument("--fps", type=int, default=30, help="捕获帧率")
    parser.add_argument("--duration", type=int, default=30, help="测试时长（秒）")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("RTMP 流捕获和 AI 推理集成测试")
    print("=" * 60)
    
    # 启动 RTMP 捕获
    if not start_rtmp_capture(args.client_id, args.rtmp_url, args.fps):
        return
    
    # 等待捕获启动
    await asyncio.sleep(2)
    
    # 检查初始状态
    check_status()
    
    # 监听 WebSocket 推送
    try:
        await monitor_websocket(args.client_id, args.duration)
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        # 停止捕获
        stop_rtmp_capture(args.client_id)
        
        # 检查最终状态
        await asyncio.sleep(1)
        check_status()
    
    print("\n测试完成")


if __name__ == "__main__":
    asyncio.run(main())
