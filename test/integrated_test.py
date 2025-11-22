#!/usr/bin/env python3
"""
CleanSight Backend 综合测试套件
集成所有API和WebSocket接口测试

使用方法：
    cd test  # 进入test目录
    python integrated_test.py [options]
"""

import asyncio
import websockets
import json
import base64
import cv2
import numpy as np
import argparse
import requests
import sys
import os
from typing import Optional, Dict, Any

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

class CleanSightTester:
    """CleanSight Backend 综合测试器"""

    def __init__(self, http_url: str = "http://localhost:8000", ws_url: str = "ws://localhost:8000"):
        self.http_url = http_url
        self.ws_url = ws_url

    # ==================== HTTP API 测试 ====================

    def test_task_initialization(self, client_id: str, actor_id: str) -> Optional[Dict[str, Any]]:
        """测试任务初始化 HTTP API"""
        print("测试任务初始化...")

        payload = {
            "client_id": client_id,
            "actor_id": actor_id
        }

        try:
            response = requests.post(f"{self.http_url}/task/initialize", json=payload, timeout=5)
            print(f"   状态码: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                print(f"   ✓ 任务创建成功: {data['task_id']}")
                print(f"   状态: {data['status']}, 阶段: {data['cleaning_stage']}")
                return data
            else:
                print(f"   ✗ 请求失败: {response.text}")
                return None

        except Exception as e:
            print(f"   ✗ 网络错误: {e}")
            return None

    def test_task_termination(self, client_id: str, task_id: str) -> bool:
        """测试任务终止 HTTP API"""
        print("测试任务终止...")

        payload = {
            "client_id": client_id,
            "task_id": task_id
        }

        try:
            response = requests.post(f"{self.http_url}/task/terminate", json=payload, timeout=5)
            print(f"   状态码: {response.status_code}")

            if response.status_code == 200:
                print(f"   ✓ 任务终止成功: {task_id}")
                return True
            else:
                print(f"   ✗ 终止失败: {response.text}")
                return False

        except Exception as e:
            print(f"   ✗ 网络错误: {e}")
            return False

    # ==================== AI 服务集成测试 ====================

    def test_ai_integration(self):
        """测试AI服务集成（检测和动作分析）"""
        print("测试AI服务集成...")

        try:
            # 导入AI服务模块
            from app.services import detection, motion
            from app.models.task import Task

            # 测试关键点检测
            print("   1. 测试关键点检测...")
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "Test Frame", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

            processed, keypoints = detection.detect_keypoints(frame)
            print(f"      ✓ 检测完成 - 帧大小: {processed.shape}, 关键点: {keypoints}")

            # 测试动作分析
            print("   2. 测试动作分析...")
            task = Task(
                task_id=1,
                initiator_operator_id=1,
                current_step=1,
                bending_count=0,
                bubble_detected=False,
                fully_submerged=False
            )

            print(f"      分析前 - 弯曲次数: {task.bending_count}")
            result = motion.analyze_motion(keypoints, task)
            print(f"      分析后 - 弯曲次数: {task.bending_count}")
            print(f"      ✓ 动作分析完成: {result}")

            return True

        except Exception as e:
            print(f"   ✗ AI集成测试失败: {e}")
            return False

    # ==================== WebSocket 测试 ====================

    async def test_websocket_video_stream(self, client_id: str) -> bool:
        """测试视频流WebSocket接口"""
        print("测试视频流WebSocket...")

        uri = f"{self.ws_url}/ai/video?client_id={client_id}"

        try:
            async with websockets.connect(uri) as websocket:
                print("   ✓ 视频流WebSocket连接成功")
                frame_count = 0

                # 只接收几帧作为测试
                while frame_count < 3:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=3.0)

                        if isinstance(message, bytes):
                            message = message.decode('utf-8')

                        if isinstance(message, str) and message.startswith("data:image/jpeg;base64,"):
                            frame_count += 1
                            print(f"      ✓ 收到帧 #{frame_count} ({len(message)} 字符)")
                        else:
                            print(f"      收到未知消息: {str(message)[:50]}...")

                    except asyncio.TimeoutError:
                        print("      ⚠ 等待帧超时")
                        break

                print(f"   视频流测试完成，共收到 {frame_count} 帧")
                return frame_count > 0

        except Exception as e:
            print(f"   ✗ 视频流测试失败: {e}")
            return False

    async def test_websocket_task_status(self, client_id: str) -> bool:
        """测试任务状态WebSocket接口"""
        print("测试任务状态WebSocket...")

        uri = f"{self.ws_url}/task/status/{client_id}"

        try:
            async with websockets.connect(uri) as websocket:
                print("   ✓ 任务状态WebSocket连接成功")
                message_count = 0

                # 接收几条状态消息
                while message_count < 2:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                        data = json.loads(message)
                        message_count += 1
                        print(f"      ✓ 收到状态更新 #{message_count}: {data.get('status', 'unknown')}")

                    except asyncio.TimeoutError:
                        print("      ⚠ 等待状态更新超时")
                        break
                    except json.JSONDecodeError:
                        print(f"      收到非JSON消息: {message}")

                print(f"   任务状态测试完成，共收到 {message_count} 条消息")
                return message_count > 0

        except Exception as e:
            print(f"   ✗ 任务状态测试失败: {e}")
            return False

    async def test_websocket_frame_upload(self, client_id: str, image_path: Optional[str] = None) -> bool:
        """测试帧上传WebSocket接口"""
        print("测试帧上传WebSocket...")

        uri = f"{self.ws_url}/inspection/upload_stream?client_id={client_id}"

        # 准备测试帧
        if image_path and image_path != "mock":
            frame = cv2.imread(image_path)
            if frame is None:
                print(f"   ✗ 无法读取图片: {image_path}")
                return False
        else:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, f"Test Frame - {client_id}", (50, 50),
                       cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

        # 编码为base64
        _, buffer = cv2.imencode('.jpg', frame)
        frame_b64 = base64.b64encode(buffer).decode('utf-8')

        try:
            async with websockets.connect(uri) as websocket:
                print("   ✓ 帧上传WebSocket连接成功")

                # 发送几帧测试
                success_count = 0
                for i in range(2):
                    await websocket.send(frame_b64)
                    response = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    if response == "success":
                        success_count += 1
                        print(f"      ✓ 发送帧 #{i+1} 成功")
                    else:
                        print(f"      ✗ 发送帧 #{i+1} 失败: {response}")

                    await asyncio.sleep(0.2)  # 短暂延迟

                print(f"   帧上传测试完成，成功率: {success_count}/2")
                return success_count > 0

        except Exception as e:
            print(f"   ✗ 帧上传测试失败: {e}")
            return False

    # ==================== 综合测试 ====================

    async def run_comprehensive_test(self, client_id: str = "test_client",
                                   actor_id: str = "test_actor",
                                   image_path: Optional[str] = None):
        """运行完整的综合测试"""
        print("=" * 70)
        print("CleanSight Backend 综合测试套件")
        print("=" * 70)
        print(f"HTTP服务器: {self.http_url}")
        print(f"WebSocket服务器: {self.ws_url}")
        print(f"客户端ID: {client_id}")
        print(f"执行者ID: {actor_id}")
        print()

        results = {}

        # 1. AI服务集成测试
        print("1. AI服务集成测试")
        print("-" * 30)
        results['ai_integration'] = self.test_ai_integration()
        print()

        # 2. HTTP API测试
        print("\n2. HTTP API测试")
        print("-" * 30)

        # 初始化任务
        task_data = self.test_task_initialization(client_id, actor_id)
        results['task_init'] = task_data is not None

        if task_data:
            task_id = task_data['task_id']

            # 等待一下让任务状态传播
            await asyncio.sleep(1)

            # 测试WebSocket接口（需要并发）
            print("\n3. WebSocket接口测试")
            print("-" * 30)

            # 并发测试WebSocket接口
            ws_tasks = [
                self.test_websocket_video_stream(client_id),
                self.test_websocket_task_status(client_id),
                self.test_websocket_frame_upload(client_id, image_path)
            ]

            ws_results = await asyncio.gather(*ws_tasks, return_exceptions=True)
            results['video_ws'] = not isinstance(ws_results[0], Exception) and ws_results[0]
            results['task_ws'] = not isinstance(ws_results[1], Exception) and ws_results[1]
            results['upload_ws'] = not isinstance(ws_results[2], Exception) and ws_results[2]

            # 终止任务
            print("\n4. 任务清理")
            print("-" * 30)
            results['task_terminate'] = self.test_task_termination(client_id, task_id)
        else:
            print("!!! 任务初始化失败，跳过WebSocket和终止测试")
            results.update({
                'video_ws': False,
                'task_ws': False,
                'upload_ws': False,
                'task_terminate': False
            })

        # 总结报告
        print("\n" + "=" * 70)
        print("测试结果总结")
        print("=" * 70)

        test_items = {
            'ai_integration': 'AI服务集成',
            'task_init': '任务初始化',
            'video_ws': '视频流WebSocket',
            'task_ws': '任务状态WebSocket',
            'upload_ws': '帧上传WebSocket',
            'task_terminate': '任务终止'
        }

        passed = 0
        total = len(test_items)

        for key, name in test_items.items():
            status = "✅ 通过" if results.get(key, False) else "❌ 失败"
            print("25")
            if results.get(key, False):
                passed += 1

        print("-" * 70)
        print(f"总体结果: {passed}/{total} 测试通过")

        if passed == total:
            print("🎉 所有测试通过！系统运行正常")
        elif passed >= total * 0.8:
            print("⚠️ 大部分测试通过，系统基本正常")
        else:
            print("❌ 多项测试失败，请检查系统配置")

        return results


async def main():
    parser = argparse.ArgumentParser(description="CleanSight Backend 综合测试套件")
    parser.add_argument("--http-url", default="http://localhost:8000",
                       help="HTTP服务器地址 (默认: http://localhost:8000)")
    parser.add_argument("--ws-url", default="ws://localhost:8000",
                       help="WebSocket服务器地址 (默认: ws://localhost:8000)")
    parser.add_argument("--client-id", default="test_client",
                       help="测试用的客户端ID (默认: test_client)")
    parser.add_argument("--actor-id", default="test_actor",
                       help="测试用的执行者ID (默认: test_actor)")
    parser.add_argument("--image", default="mock",
                       help="用于帧上传测试的图片路径 (默认: 创建模拟帧)")
    parser.add_argument("--test", choices=["all", "ai", "http", "ws"],
                       default="all", help="要运行的测试类型")

    args = parser.parse_args()

    tester = CleanSightTester(args.http_url, args.ws_url)

    try:
        if args.test == "all":
            await tester.run_comprehensive_test(args.client_id, args.actor_id,
                                              args.image if args.image != "mock" else None)
        elif args.test == "ai":
            print("🧪 仅测试AI服务集成...")
            result = tester.test_ai_integration()
            print(f"结果: {'✅ 通过' if result else '❌ 失败'}")
        elif args.test == "http":
            print("🧪 仅测试HTTP API...")
            task_data = tester.test_task_initialization(args.client_id, args.actor_id)
            if task_data:
                await asyncio.sleep(1)
                tester.test_task_termination(args.client_id, task_data['task_id'])
        elif args.test == "ws":
            print("🧪 仅测试WebSocket接口...")
            ws_results = await asyncio.gather(
                tester.test_websocket_video_stream(args.client_id),
                tester.test_websocket_task_status(args.client_id),
                tester.test_websocket_frame_upload(args.client_id,
                                                 args.image if args.image != "mock" else None)
            )
            for i, result in enumerate(ws_results):
                test_names = ["视频流", "任务状态", "帧上传"]
                print(f"{test_names[i]}: {'✅ 通过' if result else '❌ 失败'}")

    except KeyboardInterrupt:
        print("\n🛑 测试被用户中断")
    except Exception as e:
        print(f"❌ 测试过程中发生错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())