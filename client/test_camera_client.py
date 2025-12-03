"""
摄像头客户端测试脚本
用于测试命令行客户端和API服务
"""

import time
import requests
import subprocess
import sys
import argparse
import signal
import os


class ClientTester:
    """客户端测试器"""
    
    def __init__(self, client_id: str = "test_camera", duration: int = 30):
        self.client_id = client_id
        self.duration = duration
        self.server_url = "ws://localhost:8000/inspection/upload_stream"
        self.api_url = "http://localhost:8001"
        
    def test_cli_client(self) -> bool:
        """测试命令行客户端"""
        print("=" * 60)
        print("🧪 测试命令行客户端")
        print("=" * 60)
        
        try:
            # 启动命令行客户端
            cmd = [
                sys.executable,
                "camera_client.py",
                "--client-id", self.client_id,
                "--duration", str(self.duration)
            ]
            
            print(f"执行命令: {' '.join(cmd)}")
            print(f"运行时长: {self.duration} 秒")
            print()
            
            # 运行客户端
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            
            # 实时输出日志
            for line in process.stdout:
                print(line, end='')
            
            # 等待进程结束
            return_code = process.wait()
            
            if return_code == 0:
                print("\n✅ 命令行客户端测试通过")
                return True
            else:
                print(f"\n❌ 命令行客户端测试失败 (返回码: {return_code})")
                return False
                
        except KeyboardInterrupt:
            print("\n⚠️  测试被用户中断")
            return False
        except Exception as e:
            print(f"\n❌ 测试失败: {e}")
            return False
    
    def test_api_service(self) -> bool:
        """测试API服务"""
        print("=" * 60)
        print("🧪 测试API服务")
        print("=" * 60)
        
        # 检查API服务是否运行
        try:
            response = requests.get(f"{self.api_url}/health", timeout=2)
            print("✅ API服务已运行")
        except requests.exceptions.RequestException:
            print("❌ API服务未运行")
            print("   请先启动API服务: python camera_client_api.py")
            return False
        
        try:
            # 1. 测试启动摄像头
            print("\n1️⃣  测试启动摄像头...")
            response = requests.post(
                f"{self.api_url}/start",
                json={
                    "client_id": self.client_id,
                    "server_url": self.server_url,
                    "camera_id": 0,
                    "fps": 30,
                    "width": 640,
                    "height": 480,
                    "jpeg_quality": 70
                },
                timeout=10
            )
            
            if response.status_code == 200:
                print("   ✅ 启动成功")
                print(f"   响应: {response.json()}")
            else:
                print(f"   ❌ 启动失败: {response.text}")
                return False
            
            # 2. 等待几秒并检查状态
            print(f"\n2️⃣  运行 {self.duration} 秒...")
            for i in range(self.duration):
                time.sleep(1)
                
                # 每5秒检查一次状态
                if (i + 1) % 5 == 0:
                    response = requests.get(f"{self.api_url}/status", timeout=2)
                    if response.status_code == 200:
                        stats = response.json()
                        print(f"   [{i+1}s] 帧数: {stats['frames_sent']} | "
                              f"FPS: {stats['average_fps']:.2f} | "
                              f"成功率: {stats['success_rate']:.2f}%")
            
            # 3. 测试获取状态
            print("\n3️⃣  测试获取状态...")
            response = requests.get(f"{self.api_url}/status", timeout=2)
            
            if response.status_code == 200:
                stats = response.json()
                print("   ✅ 获取状态成功")
                print(f"   运行状态: {stats['is_running']}")
                print(f"   发送帧数: {stats['frames_sent']}")
                print(f"   成功率: {stats['success_rate']:.2f}%")
                print(f"   平均FPS: {stats['average_fps']:.2f}")
            else:
                print(f"   ❌ 获取状态失败: {response.text}")
            
            # 4. 测试停止摄像头
            print("\n4️⃣  测试停止摄像头...")
            response = requests.post(f"{self.api_url}/stop", timeout=10)
            
            if response.status_code == 200:
                print("   ✅ 停止成功")
                result = response.json()
                print(f"   最终统计: {result['final_stats']}")
            else:
                print(f"   ❌ 停止失败: {response.text}")
                return False
            
            # 5. 验证已停止
            print("\n5️⃣  验证停止状态...")
            response = requests.get(f"{self.api_url}/status", timeout=2)
            
            if response.status_code == 200:
                stats = response.json()
                if not stats['is_running']:
                    print("   ✅ 已确认停止")
                else:
                    print("   ❌ 状态异常，仍在运行")
                    return False
            
            print("\n✅ API服务测试通过")
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"\n❌ API请求失败: {e}")
            return False
        except Exception as e:
            print(f"\n❌ 测试失败: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description='摄像头客户端测试脚本')
    parser.add_argument('--mode', '-m',
                       type=str,
                       choices=['cli', 'api', 'both'],
                       default='both',
                       help='测试模式: cli(命令行), api(API服务), both(全部)')
    parser.add_argument('--client-id', '-c',
                       type=str,
                       default='test_camera',
                       help='客户端ID (默认: test_camera)')
    parser.add_argument('--duration', '-d',
                       type=int,
                       default=30,
                       help='测试时长（秒）(默认: 30)')
    
    args = parser.parse_args()
    
    tester = ClientTester(
        client_id=args.client_id,
        duration=args.duration
    )
    
    print("=" * 60)
    print("🚀 摄像头客户端测试")
    print("=" * 60)
    print(f"测试模式: {args.mode}")
    print(f"客户端ID: {args.client_id}")
    print(f"测试时长: {args.duration} 秒")
    print("=" * 60)
    print()
    
    # 检查服务器是否运行
    try:
        response = requests.get("http://localhost:8000/", timeout=2)
        print("✅ CleanSight服务器正在运行")
    except requests.exceptions.RequestException:
        print("❌ CleanSight服务器未运行")
        print("   请先启动服务器: uvicorn app.main:app --reload")
        print()
        sys.exit(1)
    
    success = True
    
    # 运行测试
    if args.mode in ['cli', 'both']:
        print()
        if not tester.test_cli_client():
            success = False
    
    if args.mode in ['api', 'both']:
        print()
        if not tester.test_api_service():
            success = False
    
    # 总结
    print()
    print("=" * 60)
    if success:
        print("✅ 所有测试通过")
    else:
        print("❌ 部分测试失败")
    print("=" * 60)
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
