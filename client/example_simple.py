"""
简单示例：启动摄像头采集30秒后自动停止
"""

from camera_client import CameraClient
import time

def main():
    print("=" * 60)
    print("📹 简单示例：摄像头采集")
    print("=" * 60)
    
    # 创建客户端
    client = CameraClient(
        client_id="example_camera",
        server_url="ws://localhost:8000/inspection/upload_stream",
        camera_id=0,
        fps=30,
        jpeg_quality=70,
        frame_width=640,
        frame_height=480
    )
    
    # 启动采集
    print("启动摄像头采集...")
    if not client.start():
        print("❌ 启动失败")
        return
    
    # 运行30秒
    print("运行30秒...")
    try:
        for i in range(30):
            time.sleep(1)
            if (i + 1) % 10 == 0:
                # 每10秒显示一次统计
                stats = client.get_stats()
                print(f"[{i+1}s] 已发送 {stats['frames_sent']} 帧, "
                      f"FPS: {stats['average_fps']:.2f}, "
                      f"成功率: {stats['success_rate']:.2f}%")
    
    except KeyboardInterrupt:
        print("\n收到中断信号")
    
    finally:
        # 停止采集
        print("\n停止摄像头...")
        client.stop()
        
        # 显示最终统计
        stats = client.get_stats()
        print("\n最终统计:")
        print(f"  总帧数: {stats['frames_sent']}")
        print(f"  成功: {stats['frames_success']}")
        print(f"  失败: {stats['frames_error']}")
        print(f"  成功率: {stats['success_rate']:.2f}%")
        print(f"  平均FPS: {stats['average_fps']:.2f}")


if __name__ == "__main__":
    main()
