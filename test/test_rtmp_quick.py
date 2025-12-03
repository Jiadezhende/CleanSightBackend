"""
RTMP 快速测试脚本 - 一键自动化测试

【功能】
自动完成 RTMP 流捕获 + AI 推理 + WebSocket 接收的完整测试流程

【特点】
- 自动启动 ffmpeg 推流
- 自动启动后端 RTMP 捕获
- 自动接收 WebSocket 推理结果
- 自动清理资源
- 生成测试报告

【前置条件】
1. MediaMTX 正在运行 (rtmp://localhost:1935)
2. 后端 API 正在运行 (http://localhost:8000)
3. 测试视频存在 (test/test_video.mp4)
4. ffmpeg 已安装

【使用方法】
python test/test_rtmp_quick.py

【可选参数】
python test/test_rtmp_quick.py --duration 60 --client_id my_camera
"""

import asyncio
import subprocess
import requests
import websockets
import json
import time
import argparse
import os
from datetime import datetime
from typing import Optional


class RTMPQuickTest:
    def __init__(
        self, 
        client_id: str = "quick_test",
        rtmp_url: str = "rtmp://localhost:1935/live/test",
        duration: int = 30,
        fps: int = 30
    ):
        self.client_id = client_id
        self.rtmp_url = rtmp_url
        self.duration = duration
        self.fps = fps
        
        # ffmpeg 路径
        self.ffmpeg_path = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
        
        # 测试视频路径
        self.test_video = os.path.join(os.path.dirname(__file__), "test_video.mp4")
        
        # 统计数据
        self.stats = {
            "start_time": None,
            "end_time": None,
            "frames_received": 0,
            "ffmpeg_process": None
        }
    
    def check_prerequisites(self) -> bool:
        """检查前置条件"""
        print("🔍 检查前置条件...")
        
        # 检查 ffmpeg
        if not os.path.exists(self.ffmpeg_path):
            print(f"❌ ffmpeg 不存在: {self.ffmpeg_path}")
            return False
        print(f"✅ ffmpeg: {self.ffmpeg_path}")
        
        # 检查测试视频
        if not os.path.exists(self.test_video):
            print(f"❌ 测试视频不存在: {self.test_video}")
            return False
        print(f"✅ 测试视频: {self.test_video}")
        
        # 检查后端 API
        try:
            response = requests.get("http://localhost:8000/ai/status", timeout=2)
            if response.status_code == 200:
                print("✅ 后端 API: http://localhost:8000")
            else:
                print(f"❌ 后端 API 响应异常: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"❌ 后端 API 无法连接: {e}")
            print("   请先启动后端: uvicorn app.main:app --reload")
            return False
        
        # 检查 MediaMTX (尝试连接 RTMP)
        # 注意: 这里不做实际 RTMP 连接测试，让 ffmpeg 来验证
        print("✅ 前置条件检查完成")
        return True
    
    def start_ffmpeg_push(self) -> bool:
        """启动 ffmpeg 推流"""
        print(f"\n📤 启动 ffmpeg 推流到 {self.rtmp_url}")
        
        cmd = [
            self.ffmpeg_path,
            "-re",  # 按实际帧率读取
            "-stream_loop", "-1",  # 循环播放
            "-i", self.test_video,
            "-c:v", "libx264",  # H.264 编码
            "-preset", "ultrafast",  # 快速编码
            "-tune", "zerolatency",  # 低延迟
            "-f", "flv",  # RTMP 需要 FLV 封装
            self.rtmp_url
        ]
        
        try:
            # 启动 ffmpeg 进程 (后台运行,不捕获输出以避免缓冲区问题)
            # Windows 下使用 CREATE_NO_WINDOW 避免弹出窗口
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            
            self.stats["ffmpeg_process"] = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,  # 丢弃 stdout,避免缓冲区填满
                stderr=subprocess.DEVNULL,  # 丢弃 stderr,避免缓冲区填满
                creationflags=creation_flags
            )
            
            print("⏳ 等待推流建立连接 (5秒)...")
            
            # 等待 5 秒让推流完全建立
            time.sleep(5)
            
            # 检查进程是否还在运行
            if self.stats["ffmpeg_process"].poll() is not None:
                # 进程已退出
                print(f"❌ ffmpeg 推流进程已退出 (退出码: {self.stats['ffmpeg_process'].returncode})")
                print("   可能原因:")
                print("   1. 视频文件格式不兼容")
                print("   2. MediaMTX 未运行")
                print("   3. RTMP URL 错误")
                print("\n   尝试手动测试: python test_ffmpeg_push.py")
                return False
            
            print("✅ ffmpeg 推流进程运行中")
            print("   提示: 检查 MediaMTX 日志应该看到:")
            print("   INF [RTMP] [conn ...] opened")
            print("   INF [RTMP] [conn ...] is publishing to path 'live/test'")
            print()
            return True
                
        except Exception as e:
            print(f"❌ 启动 ffmpeg 失败: {e}")
            return False
    
    def start_rtmp_capture(self) -> bool:
        """启动后端 RTMP 捕获"""
        print(f"\n📥 启动后端 RTMP 捕获: {self.client_id}")
        
        url = "http://localhost:8000/inspection/start_rtmp_stream"
        payload = {
            "client_id": self.client_id,
            "rtmp_url": self.rtmp_url,
            "fps": self.fps
        }
        
        try:
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                print(f"✅ RTMP 捕获已启动: {response.json()}")
                return True
            else:
                print(f"❌ 启动捕获失败: {response.text}")
                return False
        except Exception as e:
            print(f"❌ 请求失败: {e}")
            return False
    
    def stop_rtmp_capture(self):
        """停止后端 RTMP 捕获"""
        print(f"\n🛑 停止后端 RTMP 捕获: {self.client_id}")
        
        url = f"http://localhost:8000/inspection/stop_rtmp_stream?client_id={self.client_id}"
        
        try:
            response = requests.post(url, timeout=5)
            if response.status_code == 200:
                print(f"✅ RTMP 捕获已停止")
            else:
                print(f"⚠️ 停止捕获失败: {response.text}")
        except Exception as e:
            print(f"⚠️ 请求失败: {e}")
    
    def stop_ffmpeg_push(self):
        """停止 ffmpeg 推流"""
        print("\n🛑 停止 ffmpeg 推流")
        
        if self.stats["ffmpeg_process"]:
            try:
                poll_result = self.stats["ffmpeg_process"].poll()
                
                if poll_result is None:
                    # 进程还在运行
                    self.stats["ffmpeg_process"].terminate()
                    try:
                        self.stats["ffmpeg_process"].wait(timeout=5)
                        print("✅ ffmpeg 推流已正常停止")
                    except subprocess.TimeoutExpired:
                        self.stats["ffmpeg_process"].kill()
                        self.stats["ffmpeg_process"].wait()
                        print("⚠️ ffmpeg 强制停止")
                else:
                    # 进程已经提前退出
                    print(f"⚠️ ffmpeg 进程已提前退出 (退出码: {poll_result})")
                            
            except Exception as e:
                print(f"⚠️ 停止 ffmpeg 失败: {e}")
                try:
                    if self.stats["ffmpeg_process"].poll() is None:
                        self.stats["ffmpeg_process"].kill()
                        self.stats["ffmpeg_process"].wait()
                except:
                    pass
    
    async def receive_websocket_frames(self):
        """接收 WebSocket 推理结果"""
        uri = f"ws://localhost:8000/ai/video?client_id={self.client_id}"
        print(f"\n📺 连接 WebSocket: {uri}")
        
        self.stats["start_time"] = datetime.now()
        last_print_time = time.time()
        
        try:
            async with websockets.connect(uri) as websocket:
                print("✅ WebSocket 已连接，开始接收推理结果...\n")
                
                while (datetime.now() - self.stats["start_time"]).seconds < self.duration:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        self.stats["frames_received"] += 1
                        
                        # 每秒打印一次统计
                        current_time = time.time()
                        if current_time - last_print_time >= 1.0:
                            elapsed = (datetime.now() - self.stats["start_time"]).seconds
                            fps = self.stats["frames_received"] / max(elapsed, 1)
                            print(f"⏱️  已运行 {elapsed}s | 已接收 {self.stats['frames_received']} 帧 | 平均 {fps:.1f} FPS")
                            last_print_time = current_time
                            
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        print(f"⚠️ 接收消息异常: {e}")
                        break
                        
        except Exception as e:
            print(f"❌ WebSocket 连接失败: {e}")
        
        self.stats["end_time"] = datetime.now()
    
    def print_report(self):
        """打印测试报告"""
        print("\n" + "=" * 60)
        print("📊 测试报告")
        print("=" * 60)
        
        if self.stats["start_time"] and self.stats["end_time"]:
            duration = (self.stats["end_time"] - self.stats["start_time"]).total_seconds()
            avg_fps = self.stats["frames_received"] / max(duration, 1)
            
            print(f"测试时长: {duration:.1f} 秒")
            print(f"接收帧数: {self.stats['frames_received']} 帧")
            print(f"平均帧率: {avg_fps:.1f} FPS")
            print(f"目标帧率: {self.fps} FPS")
            
            if avg_fps >= self.fps * 0.8:
                print("✅ 测试通过 (帧率达标)")
            else:
                print(f"⚠️ 测试未通过 (帧率低于目标的 80%)")
        else:
            print("❌ 测试未正常完成")
        
        print("=" * 60)
    
    async def run(self):
        """运行完整测试"""
        print("=" * 60)
        print("🚀 RTMP 快速测试")
        print("=" * 60)
        
        # 1. 检查前置条件
        if not self.check_prerequisites():
            print("\n❌ 前置条件检查失败，测试终止")
            return
        
        try:
            # 2. 启动 ffmpeg 推流
            if not self.start_ffmpeg_push():
                print("\n❌ ffmpeg 推流启动失败，测试终止")
                return
            
            # 等待推流完全建立（重要！给 MediaMTX 足够时间接收流）
            print("\n⏳ 等待推流建立...")
            print("   (观察 MediaMTX 日志,应该看到 'is publishing' 消息)")
            await asyncio.sleep(8)  # 增加到 8 秒,确保流稳定
            
            # 3. 启动后端 RTMP 捕获
            if not self.start_rtmp_capture():
                print("\n❌ 后端捕获启动失败，测试终止")
                self.stop_ffmpeg_push()
                return
            
            # 等待捕获线程初始化
            print("⏳ 等待后端捕获初始化...")
            await asyncio.sleep(5)  # 给后端足够时间连接并开始捕获
            
            # 4. 接收 WebSocket 推理结果
            await self.receive_websocket_frames()
            
        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断测试")
        
        finally:
            # 5. 清理资源
            self.stop_rtmp_capture()
            self.stop_ffmpeg_push()
            
            # 6. 打印报告
            await asyncio.sleep(1)
            self.print_report()


async def main():
    parser = argparse.ArgumentParser(description="RTMP 快速测试")
    parser.add_argument("--client_id", type=str, default="quick_test", help="客户端 ID")
    parser.add_argument("--duration", type=int, default=30, help="测试时长（秒）")
    parser.add_argument("--fps", type=int, default=30, help="捕获帧率")
    
    args = parser.parse_args()
    
    test = RTMPQuickTest(
        client_id=args.client_id,
        duration=args.duration,
        fps=args.fps
    )
    
    await test.run()


if __name__ == "__main__":
    asyncio.run(main())
