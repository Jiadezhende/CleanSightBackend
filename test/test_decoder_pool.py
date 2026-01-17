"""
测试基于进程池的解码器实现

运行此脚本以测试新的解码器进程池功能
"""

import sys
import os
import time
import requests
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 测试配置
BACKEND_URL = "http://localhost:8000"
TEST_CLIENT_ID = "test_decoder_pool_001"
TEST_RTSP_URL = "rtsp://localhost:8554/live/test"  # 需要有实际的 RTSP 流


def print_header(text: str):
    """打印测试标题"""
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


def test_decoder_stats():
    """测试获取解码器统计信息"""
    print_header("测试1: 获取解码器统计信息")
    
    try:
        response = requests.get(f"{BACKEND_URL}/inspection/decoder_stats")
        if response.status_code == 200:
            stats = response.json()
            print(f"✓ 解码器统计信息:")
            print(f"  - 总进程数: {stats['total_processes']}")
            print(f"  - 活跃进程数: {stats['alive_workers']}")
            print(f"  - 最大进程数: {stats['total_workers']}")
            print(f"  - 队列大小: {stats['frame_queue_size']}")
            print(f"  - 活跃任务: {stats['tasks']}")
            return True
        else:
            print(f"✗ 请求失败: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ 异常: {e}")
        return False


def test_start_rtsp_stream():
    """测试启动RTSP流"""
    print_header("测试2: 启动RTSP流捕获")
    
    payload = {
        "client_id": TEST_CLIENT_ID,
        "rtsp_url": TEST_RTSP_URL,
        "fps": 30
    }
    
    try:
        response = requests.post(
            f"{BACKEND_URL}/inspection/start_rtsp_stream",
            json=payload
        )
        
        if response.status_code == 200:
            result = response.json()
            print(f"✓ RTSP流启动成功:")
            print(f"  - 消息: {result['message']}")
            print(f"  - 进程池状态: {result['pool_stats']}")
            return True
        else:
            print(f"✗ 启动失败: {response.status_code}")
            print(f"  响应: {response.text}")
            return False
    except Exception as e:
        print(f"✗ 异常: {e}")
        return False


def test_check_running():
    """测试检查运行状态"""
    print_header("测试3: 检查运行状态")
    
    try:
        response = requests.get(f"{BACKEND_URL}/inspection/decoder_stats")
        if response.status_code == 200:
            stats = response.json()
            print(f"✓ 当前运行状态:")
            print(f"  - 活跃进程数: {stats['alive_workers']}/{stats['total_workers']}")
            print(f"  - 活跃任务: {stats['tasks']}")
            
            if TEST_CLIENT_ID in stats['tasks']:
                print(f"  ✓ 测试客户端 {TEST_CLIENT_ID} 正在运行")
                return True
            else:
                print(f"  ✗ 测试客户端 {TEST_CLIENT_ID} 未找到")
                return False
        else:
            print(f"✗ 请求失败: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ 异常: {e}")
        return False


def test_stop_rtsp_stream():
    """测试停止RTSP流"""
    print_header("测试4: 停止RTSP流捕获")
    
    try:
        response = requests.post(
            f"{BACKEND_URL}/inspection/stop_rtsp_stream",
            params={"client_id": TEST_CLIENT_ID}
        )
        
        if response.status_code == 200:
            result = response.json()
            print(f"✓ RTSP流停止成功:")
            print(f"  - 消息: {result['message']}")
            print(f"  - 进程池状态: {result['pool_stats']}")
            return True
        else:
            print(f"✗ 停止失败: {response.status_code}")
            print(f"  响应: {response.text}")
            return False
    except Exception as e:
        print(f"✗ 异常: {e}")
        return False


def test_multiple_streams():
    """测试多个并发流"""
    print_header("测试5: 多个并发流（压力测试）")
    
    num_streams = 3
    client_ids = [f"test_stream_{i:03d}" for i in range(num_streams)]
    
    print(f"启动 {num_streams} 个并发流...")
    
    success_count = 0
    for client_id in client_ids:
        payload = {
            "client_id": client_id,
            "rtsp_url": f"rtsp://localhost:8554/live/{client_id}",
            "fps": 30
        }
        
        try:
            response = requests.post(
                f"{BACKEND_URL}/inspection/start_rtsp_stream",
                json=payload
            )
            
            if response.status_code == 200:
                print(f"  ✓ {client_id} 启动成功")
                success_count += 1
            else:
                print(f"  ✗ {client_id} 启动失败: {response.status_code}")
        except Exception as e:
            print(f"  ✗ {client_id} 异常: {e}")
        
        time.sleep(0.5)  # 短暂延迟
    
    # 检查状态
    print(f"\n检查进程池状态...")
    try:
        response = requests.get(f"{BACKEND_URL}/inspection/decoder_stats")
        if response.status_code == 200:
            stats = response.json()
            print(f"  - 活跃进程数: {stats['alive_workers']}/{stats['total_workers']}")
            print(f"  - 活跃任务: {stats['tasks']}")
    except Exception as e:
        print(f"  ✗ 获取状态异常: {e}")
    
    # 清理：停止所有流
    print(f"\n清理: 停止所有测试流...")
    for client_id in client_ids:
        try:
            requests.post(
                f"{BACKEND_URL}/inspection/stop_stream",
                params={"client_id": client_id}
            )
            print(f"  ✓ {client_id} 已停止")
        except:
            pass
    
    print(f"\n✓ 成功启动 {success_count}/{num_streams} 个流")
    return success_count == num_streams


def main():
    """主测试流程"""
    print("=" * 60)
    print("  基于进程池的解码器测试")
    print("=" * 60)
    print(f"\n后端地址: {BACKEND_URL}")
    print(f"测试客户端ID: {TEST_CLIENT_ID}")
    print(f"测试RTSP URL: {TEST_RTSP_URL}")
    print("\n⚠️  注意: 此测试需要后端服务正在运行")
    print("⚠️  注意: 需要有可用的RTSP流源（或修改TEST_RTSP_URL）")
    
    input("\n按Enter键开始测试...")
    
    results = []
    
    # 执行测试
    results.append(("获取解码器统计", test_decoder_stats()))
    
    # 注意: 以下测试需要实际的RTSP流
    print("\n⚠️  以下测试需要实际的RTSP流，如果没有可用流源，测试将失败")
    proceed = input("是否继续测试流捕获功能? (y/n): ")
    
    if proceed.lower() == 'y':
        results.append(("启动RTSP流", test_start_rtsp_stream()))
        
        if results[-1][1]:
            time.sleep(3)  # 等待流稳定
            results.append(("检查运行状态", test_check_running()))
            time.sleep(2)
            results.append(("停止RTSP流", test_stop_rtsp_stream()))
        
        # 压力测试
        pressure_test = input("\n是否进行多流并发压力测试? (y/n): ")
        if pressure_test.lower() == 'y':
            results.append(("多流并发测试", test_multiple_streams()))
    
    # 测试总结
    print_header("测试总结")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{status}: {test_name}")
    
    print(f"\n总计: {passed}/{total} 测试通过")
    
    if passed == total:
        print("\n🎉 所有测试通过!")
    else:
        print(f"\n⚠️  {total - passed} 个测试失败")
    
    return passed == total


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
