"""
解码器进程池使用示例

演示如何使用新的基于进程池的解码器架构
"""

import requests
import time
from typing import List, Dict

# 配置
BASE_URL = "http://localhost:8000"


def start_stream(client_id: str, rtsp_url: str, fps: int = 30) -> Dict:
    """启动一个视频流"""
    response = requests.post(
        f"{BASE_URL}/inspection/start_rtsp_stream",
        json={
            "client_id": client_id,
            "rtsp_url": rtsp_url,
            "fps": fps
        }
    )
    return response.json()


def stop_stream(client_id: str) -> Dict:
    """停止一个视频流"""
    response = requests.post(
        f"{BASE_URL}/inspection/stop_stream",
        params={"client_id": client_id}
    )
    return response.json()


def get_decoder_stats() -> Dict:
    """获取解码器统计信息"""
    response = requests.get(f"{BASE_URL}/inspection/decoder_stats")
    return response.json()


def example_single_stream():
    """示例1: 单个视频流"""
    print("=" * 60)
    print("示例1: 启动单个视频流")
    print("=" * 60)
    
    client_id = "camera_001"
    rtsp_url = "rtsp://localhost:8554/live/stream1"
    
    # 启动流
    print(f"\n启动流: {client_id}")
    result = start_stream(client_id, rtsp_url)
    print(f"结果: {result['message']}")
    print(f"进程池状态: {result['pool_stats']}")
    
    # 等待几秒
    print("\n等待5秒...")
    time.sleep(5)
    
    # 检查状态
    print("\n检查状态:")
    stats = get_decoder_stats()
    print(f"活跃进程: {stats['alive_workers']}/{stats['total_workers']}")
    print(f"队列大小: {stats['frame_queue_size']}")
    print(f"活跃任务: {stats['tasks']}")
    
    # 停止流
    print(f"\n停止流: {client_id}")
    result = stop_stream(client_id)
    print(f"结果: {result['message']}")


def example_multiple_streams():
    """示例2: 多个并发视频流"""
    print("\n" + "=" * 60)
    print("示例2: 启动多个并发视频流")
    print("=" * 60)
    
    # 配置多个摄像头
    cameras = [
        {"id": "camera_001", "url": "rtsp://localhost:8554/live/stream1"},
        {"id": "camera_002", "url": "rtsp://localhost:8554/live/stream2"},
        {"id": "camera_003", "url": "rtsp://localhost:8554/live/stream3"},
    ]
    
    # 批量启动
    print(f"\n启动 {len(cameras)} 个视频流...")
    for cam in cameras:
        result = start_stream(cam["id"], cam["url"])
        print(f"✓ {cam['id']}: {result['message']}")
        time.sleep(0.5)  # 短暂延迟
    
    # 检查状态
    print("\n当前进程池状态:")
    stats = get_decoder_stats()
    print(f"活跃进程: {stats['alive_workers']}/{stats['total_workers']}")
    print(f"队列大小: {stats['frame_queue_size']}")
    print(f"活跃任务: {stats['tasks']}")
    
    # 运行一段时间
    print("\n运行10秒...")
    for i in range(10):
        time.sleep(1)
        stats = get_decoder_stats()
        print(f"  [{i+1}s] 队列: {stats['queue_size']} 帧")
    
    # 批量停止
    print("\n停止所有视频流...")
    for cam in cameras:
        result = stop_stream(cam["id"])
        print(f"✓ {cam['id']}: 已停止")


def example_monitoring():
    """示例3: 监控进程池状态"""
    print("\n" + "=" * 60)
    print("示例3: 实时监控进程池")
    print("=" * 60)
    
    print("\n按Ctrl+C停止监控...\n")
    
    try:
        while True:
            stats = get_decoder_stats()
            
            # 清屏（可选）
            # os.system('cls' if os.name == 'nt' else 'clear')
            
            print(f"\r[{time.strftime('%H:%M:%S')}] "
                  f"进程: {stats['alive_workers']}/{stats['total_workers']} | "
                  f"队列: {stats['frame_queue_size']} | "
                  f"活跃任务: {len(stats['tasks'])}",
                  end='', flush=True)
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n\n监控已停止")


def example_stress_test():
    """示例4: 压力测试（最多16个流）"""
    print("\n" + "=" * 60)
    print("示例4: 压力测试（16个并发流）")
    print("=" * 60)
    
    num_streams = 16
    client_ids = [f"stress_test_{i:02d}" for i in range(num_streams)]
    
    print(f"\n启动 {num_streams} 个并发流...")
    success_count = 0
    
    for i, client_id in enumerate(client_ids):
        rtsp_url = f"rtsp://localhost:8554/live/stress_{i}"
        
        try:
            result = start_stream(client_id, rtsp_url)
            print(f"✓ [{i+1}/{num_streams}] {client_id}")
            success_count += 1
        except Exception as e:
            print(f"✗ [{i+1}/{num_streams}] {client_id}: {e}")
        
        time.sleep(0.3)
    
    print(f"\n成功启动: {success_count}/{num_streams}")
    
    # 监控性能
    print("\n监控30秒性能...")
    for i in range(30):
        stats = get_decoder_stats()
        print(f"  [{i+1}s] "
              f"进程: {stats['alive_processes']} | "
              f"队列: {stats['queue_size']}")
        time.sleep(1)
    
    # 清理
    print("\n清理: 停止所有流...")
    for client_id in client_ids:
        try:
            stop_stream(client_id)
            print(f"✓ {client_id}")
        except:
            pass


def example_error_handling():
    """示例5: 错误处理"""
    print("\n" + "=" * 60)
    print("示例5: 错误处理示例")
    print("=" * 60)
    
    # 尝试启动一个无效的流
    print("\n1. 启动无效URL的流:")
    try:
        result = start_stream("invalid_stream", "rtsp://invalid:9999/stream")
        print(f"结果: {result}")
    except Exception as e:
        print(f"捕获异常: {e}")
    
    # 尝试停止不存在的流
    print("\n2. 停止不存在的流:")
    try:
        result = stop_stream("non_existent_stream")
        print(f"结果: {result}")
    except Exception as e:
        print(f"捕获异常: {e}")
    
    # 尝试启动重复的流
    print("\n3. 启动重复的流:")
    client_id = "duplicate_test"
    rtsp_url = "rtsp://localhost:8554/live/test"
    
    try:
        # 第一次启动
        result = start_stream(client_id, rtsp_url)
        print(f"第一次: {result['message']}")
        
        # 第二次启动（应该失败）
        result = start_stream(client_id, rtsp_url)
        print(f"第二次: {result['message']}")
    except Exception as e:
        print(f"捕获异常: {e}")
    finally:
        # 清理
        try:
            stop_stream(client_id)
        except:
            pass


def main():
    """主函数"""
    print("解码器进程池使用示例")
    print("=" * 60)
    print("\n请确保后端服务正在运行:")
    print(f"  {BASE_URL}")
    print("\n注意: 某些示例需要实际的RTSP流才能正常工作")
    
    # 选择示例
    print("\n可用示例:")
    print("  1. 单个视频流")
    print("  2. 多个并发视频流")
    print("  3. 实时监控")
    print("  4. 压力测试（16个流）")
    print("  5. 错误处理")
    print("  0. 全部运行")
    
    choice = input("\n请选择示例 (0-5): ").strip()
    
    if choice == "1":
        example_single_stream()
    elif choice == "2":
        example_multiple_streams()
    elif choice == "3":
        example_monitoring()
    elif choice == "4":
        example_stress_test()
    elif choice == "5":
        example_error_handling()
    elif choice == "0":
        example_single_stream()
        example_multiple_streams()
        example_error_handling()
    else:
        print("无效选择")
    
    print("\n示例运行完成!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n程序被用户中断")
    except Exception as e:
        print(f"\n\n发生错误: {e}")
        import traceback
        traceback.print_exc()
