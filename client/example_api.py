"""
API控制示例：通过HTTP API控制摄像头
需要先启动API服务：python camera_client_api.py
"""

import requests
import time
import sys


def main():
    api_url = "http://localhost:8001"
    
    print("=" * 60)
    print("🌐 API控制示例")
    print("=" * 60)
    
    # 检查API服务
    print("检查API服务...")
    try:
        response = requests.get(f"{api_url}/health", timeout=2)
        print("✅ API服务正在运行")
    except requests.exceptions.RequestException:
        print("❌ API服务未运行")
        print("请先启动API服务: python camera_client_api.py")
        sys.exit(1)
    
    try:
        # 1. 启动摄像头
        print("\n1. 启动摄像头...")
        response = requests.post(
            f"{api_url}/start",
            json={
                "client_id": "example_api_camera",
                "camera_id": 0,
                "fps": 30,
                "width": 640,
                "height": 480,
                "jpeg_quality": 70
            },
            timeout=10
        )
        
        if response.status_code == 200:
            print("✅ 摄像头已启动")
            print(f"   {response.json()}")
        else:
            print(f"❌ 启动失败: {response.text}")
            sys.exit(1)
        
        # 2. 运行30秒，每5秒检查一次状态
        print("\n2. 运行30秒...")
        for i in range(6):
            time.sleep(5)
            
            response = requests.get(f"{api_url}/status", timeout=2)
            if response.status_code == 200:
                stats = response.json()
                print(f"   [{(i+1)*5}s] "
                      f"帧数: {stats['frames_sent']}, "
                      f"FPS: {stats['average_fps']:.2f}, "
                      f"成功率: {stats['success_rate']:.2f}%")
        
        # 3. 停止摄像头
        print("\n3. 停止摄像头...")
        response = requests.post(f"{api_url}/stop", timeout=10)
        
        if response.status_code == 200:
            print("✅ 摄像头已停止")
            result = response.json()
            stats = result['final_stats']
            print(f"\n最终统计:")
            print(f"  总帧数: {stats['frames_sent']}")
            print(f"  成功: {stats['frames_success']}")
            print(f"  失败: {stats['frames_error']}")
            print(f"  成功率: {stats['success_rate']:.2f}%")
            print(f"  平均FPS: {stats['average_fps']:.2f}")
        else:
            print(f"❌ 停止失败: {response.text}")
    
    except KeyboardInterrupt:
        print("\n\n收到中断信号，停止摄像头...")
        try:
            requests.post(f"{api_url}/stop", timeout=5)
            print("✅ 摄像头已停止")
        except:
            pass
    
    except requests.exceptions.RequestException as e:
        print(f"\n❌ API请求失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
