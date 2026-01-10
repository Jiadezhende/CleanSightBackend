"""
任务状态 WebSocket 测试客户端

测试新的状态字典和消息格式
"""
import asyncio
import websockets
import json
from datetime import datetime


async def test_task_status_websocket(client_id: str = "integration_test_client"):
    """
    测试任务状态 WebSocket 接口
    
    Args:
        client_id: 客户端ID
    """
    uri = f"ws://localhost:8000/task/status/{client_id}"
    
    print("=" * 70)
    print(f"📡 连接任务状态 WebSocket")
    print(f"客户端 ID: {client_id}")
    print(f"连接地址: {uri}")
    print("=" * 70)
    
    try:
        async with websockets.connect(uri) as websocket:
            print(f"✅ 已连接\n")
            
            message_count = 0
            
            while True:
                try:
                    # 接收消息
                    message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    message_count += 1
                    
                    # 解析 JSON
                    data = json.loads(message)
                    
                    # 打印格式化的状态信息
                    print(f"\n{'='*70}")
                    print(f"📨 消息 #{message_count} - {datetime.now().strftime('%H:%M:%S')}")
                    print(f"{'='*70}")
                    
                    # 任务ID
                    task_id = data.get("task_id")
                    if task_id is not None:
                        print(f"🆔 任务 ID: {task_id}")
                    else:
                        print(f"🆔 任务 ID: 无")
                    
                    # 状态信息
                    status = data.get("status", {})
                    print(f"\n📊 状态:")
                    print(f"   代码: {status.get('code')}")
                    print(f"   文本: {status.get('text')}")
                    print(f"   消息: {status.get('message')}")
                    print(f"   级别: {status.get('severity')}")
                    
                    # 清洗步骤
                    step = data.get("cleaning_step")
                    if step:
                        print(f"\n🧼 清洗步骤:")
                        print(f"   编号: {step.get('code')}")
                        print(f"   名称: {step.get('name')}")
                    
                    # 检测结果
                    detection = data.get("detection")
                    if detection:
                        print(f"\n🔍 检测结果:")
                        print(f"   弯折: {'是' if detection.get('bending') else '否'}")
                        print(f"   弯折次数: {detection.get('bending_count')}")
                        print(f"   检测到气泡: {'是' if detection.get('bubble_detected') else '否'}")
                        print(f"   完全浸没: {'是' if detection.get('fully_submerged') else '否'}")
                    
                    # 消息列表
                    messages = data.get("messages", [])
                    if messages:
                        print(f"\n💬 提示消息:")
                        for msg in messages:
                            print(f"   {msg}")
                    
                    # 更新时间
                    updated_at = data.get("updated_at")
                    if updated_at:
                        print(f"\n⏰ 更新时间: {updated_at}")
                    
                    print(f"{'='*70}")
                    
                except asyncio.TimeoutError:
                    print("\n⏱️  等待消息中...")
                    continue
                except json.JSONDecodeError as e:
                    print(f"\n❌ JSON 解析错误: {e}")
                    print(f"原始消息: {message}")
                    continue
                    
    except websockets.exceptions.WebSocketException as e:
        print(f"\n❌ WebSocket 错误: {e}")
        print("\n💡 提示:")
        print("   1. 确保后端服务正在运行: uvicorn app.main:app --reload")
        print("   2. 检查客户端 ID 是否正确")
    except KeyboardInterrupt:
        print(f"\n\n⏹️  已停止监听")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import sys
    
    # 从命令行参数获取 client_id
    client_id = sys.argv[1] if len(sys.argv) > 1 else "integration_test_client"
    
    print("\n🚀 任务状态 WebSocket 测试客户端")
    print("\n使用方法:")
    print("   python test_task_status_ws.py [client_id]")
    print("\n按 Ctrl+C 停止\n")
    
    try:
        asyncio.run(test_task_status_websocket(client_id))
    except KeyboardInterrupt:
        print("\n👋 再见!")
