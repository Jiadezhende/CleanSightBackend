#!/usr/bin/env python3
"""
CleanSight RTMP 集成示例
演示如何在项目中集成和使用RTMP服务
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.append(str(project_root))

from app.services.rtmp_service import rtmp_service
from app.services.ai import AIService

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class CleanSightRTMPDemo:
    """CleanSight RTMP集成演示"""
    
    def __init__(self):
        """初始化"""
        self.ai_service = None
        
    async def initialize_services(self):
        """初始化服务"""
        logger.info("初始化AI服务...")
        
        # 初始化AI服务（如果存在）
        try:
            self.ai_service = AIService()
            await self.ai_service.initialize()
            logger.info("AI服务初始化成功")
        except Exception as e:
            logger.warning(f"AI服务初始化失败: {e}")
            self.ai_service = None
    
    def frame_processor(self, frame, frame_number: int, stream_name: str):
        """
        帧处理回调函数
        
        Args:
            frame: 视频帧
            frame_number: 帧编号
            stream_name: 流名称
        """
        try:
            # 每100帧处理一次（减少计算负载）
            if frame_number % 100 == 0:
                logger.info(f"处理 {stream_name} 第 {frame_number} 帧")
                
                # 如果有AI服务，进行推理
                if self.ai_service:
                    # 这里可以调用AI推理
                    # result = await self.ai_service.process_frame(frame)
                    # logger.info(f"AI推理结果: {result}")
                    pass
                
                # 示例：简单的图像分析
                import cv2
                import numpy as np
                
                # 转换为灰度图
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                
                # 计算图像统计信息
                brightness = np.mean(gray)
                contrast = np.std(gray)
                
                # 边缘检测
                edges = cv2.Canny(gray, 50, 150)
                edge_density = np.sum(edges > 0) / edges.size
                
                logger.info(f"图像分析 - 亮度: {brightness:.2f}, 对比度: {contrast:.2f}, 边缘密度: {edge_density:.4f}")
                
                # 这里可以根据分析结果做出决策
                # 例如：检测异常、触发告警等
                
        except Exception as e:
            logger.error(f"帧处理出错: {e}")
    
    async def demo_rtmp_integration(self):
        """演示RTMP集成"""
        print("\n" + "="*50)
        print("CleanSight RTMP 服务集成演示")
        print("="*50)
        
        # 1. 检查RTMP服务状态
        print("\n1. 检查RTMP服务状态...")
        service_status = rtmp_service.check_service_status()
        print(f"服务状态: {service_status}")
        
        if service_status.get('status') != 'running':
            print("❌ RTMP服务未运行，请先启动nginx-rtmp服务")
            print("运行: sudo systemctl start nginx")
            return
        
        print("✅ RTMP服务运行正常")
        
        # 2. 获取流配置
        print("\n2. 流配置信息...")
        stream_name = "demo_camera"
        stream_url = rtmp_service.get_stream_url(stream_name)
        print(f"流名称: {stream_name}")
        print(f"推流地址: {stream_url}")
        print(f"拉流地址: {stream_url}")
        
        # 3. 测试流连接
        print(f"\n3. 测试流连接...")
        print("正在测试连接，请确保有设备推流到该地址...")
        print(f"推流命令示例:")
        print(f"  ffmpeg -re -i test.mp4 -c copy -f flv {stream_url}")
        print(f"  或使用测试脚本:")
        print(f"  python3 nginx-rtmp/test_rtmp_stream.py --stream {stream_name}")
        
        # 等待用户确认
        input("\n按回车键继续测试连接...")
        
        is_connected = rtmp_service.test_stream_connection(stream_name, timeout=15)
        
        if not is_connected:
            print("❌ 无法连接到流，请检查:")
            print("  1. 是否有设备正在推流")
            print("  2. 流名称是否正确")
            print("  3. 网络连接是否正常")
            return
        
        print("✅ 流连接成功")
        
        # 4. 启动流处理
        print(f"\n4. 启动流处理器...")
        
        # 设置帧处理回调
        success = rtmp_service.start_stream_processor(
            stream_name, 
            callback=self.frame_processor
        )
        
        if not success:
            print("❌ 启动流处理器失败")
            return
        
        print("✅ 流处理器启动成功")
        
        try:
            # 5. 监控流处理
            print(f"\n5. 监控流处理 (按Ctrl+C停止)...")
            print("处理状态:")
            
            while True:
                await asyncio.sleep(5)  # 每5秒检查一次
                
                # 获取流统计
                stats = rtmp_service.get_stream_stats(stream_name)
                if stats:
                    print(f"  帧数: {stats.get('frame_count', 0)}, "
                          f"FPS: {stats.get('fps', 0):.1f}, "
                          f"错误: {stats.get('error_count', 0)}")
                else:
                    print("  无法获取统计信息")
                
        except KeyboardInterrupt:
            print("\n用户中断处理")
        
        finally:
            # 6. 清理资源
            print(f"\n6. 停止流处理器...")
            rtmp_service.stop_stream_processor(stream_name)
            print("✅ 流处理器已停止")
    
    async def demo_api_usage(self):
        """演示API使用"""
        print("\n" + "="*50)
        print("CleanSight RTMP API 使用演示")
        print("="*50)
        
        # 这里可以添加HTTP API调用示例
        # 由于这是演示脚本，我们直接调用服务方法
        
        print("\n1. 获取服务配置...")
        config = {
            "rtmp_host": rtmp_service.rtmp_host,
            "rtmp_port": rtmp_service.rtmp_port,
            "rtmp_base_url": rtmp_service.rtmp_base_url
        }
        print(f"配置: {config}")
        
        print("\n2. 列出活跃流...")
        streams = rtmp_service.list_active_streams()
        print(f"活跃流数量: {len(streams)}")
        for stream in streams:
            print(f"  - {stream.get('name')}: {stream.get('status')}")
        
        print("\n3. API端点列表:")
        endpoints = [
            "GET /rtmp/status - 获取服务状态",
            "GET /rtmp/streams - 列出活跃流",
            "GET /rtmp/streams/{name} - 获取流信息",
            "GET /rtmp/streams/{name}/url - 获取流URL",
            "POST /rtmp/streams/{name}/test - 测试流连接",
            "POST /rtmp/streams/{name}/start - 启动流处理",
            "POST /rtmp/streams/{name}/stop - 停止流处理",
            "GET /rtmp/streams/{name}/stats - 获取流统计",
            "GET /rtmp/config - 获取RTMP配置",
            "GET /rtmp/health - 健康检查"
        ]
        
        for endpoint in endpoints:
            print(f"  {endpoint}")
        
        print(f"\n4. 使用示例:")
        print("  curl http://localhost:8000/rtmp/status")
        print("  curl http://localhost:8000/rtmp/streams/camera01/url")
        print("  curl -X POST http://localhost:8000/rtmp/streams/camera01/start")

async def main():
    """主函数"""
    demo = CleanSightRTMPDemo()
    
    try:
        # 初始化服务
        await demo.initialize_services()
        
        print("CleanSight RTMP 集成演示")
        print("请选择演示模式:")
        print("1. RTMP服务集成演示")
        print("2. API使用演示")
        print("3. 两者都运行")
        
        choice = input("请输入选择 (1/2/3): ").strip()
        
        if choice in ['1', '3']:
            await demo.demo_rtmp_integration()
        
        if choice in ['2', '3']:
            await demo.demo_api_usage()
            
    except KeyboardInterrupt:
        print("\n演示被用户中断")
    except Exception as e:
        logger.error(f"演示出错: {e}")
        raise
    
    print("\n演示完成")

if __name__ == "__main__":
    # 检查环境
    if not os.path.exists('.env'):
        print("警告: 未找到.env文件，将使用默认配置")
    
    # 运行演示
    asyncio.run(main())