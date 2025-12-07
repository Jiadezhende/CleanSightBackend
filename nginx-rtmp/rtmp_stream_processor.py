#!/usr/bin/env python3
"""
RTMP流处理示例
演示如何从nginx-rtmp服务拉取流并进行AI推理
"""

import cv2
import os
import time
import logging
from typing import Optional, Tuple
import numpy as np

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RTMPStreamProcessor:
    """RTMP流处理器"""
    
    def __init__(self, rtmp_url: str, stream_name: str = "camera"):
        """
        初始化RTMP流处理器
        
        Args:
            rtmp_url: RTMP流地址
            stream_name: 流名称
        """
        self.rtmp_url = rtmp_url
        self.stream_name = stream_name
        self.cap: Optional[cv2.VideoCapture] = None
        self.is_running = False
        
    def connect_stream(self, timeout: int = 10) -> bool:
        """
        连接RTMP流
        
        Args:
            timeout: 连接超时时间（秒）
            
        Returns:
            bool: 是否连接成功
        """
        logger.info(f"正在连接RTMP流: {self.rtmp_url}")
        
        try:
            # 创建VideoCapture对象
            self.cap = cv2.VideoCapture(self.rtmp_url)
            
            # 设置缓冲区大小（减少延迟）
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            
            # 等待连接建立
            start_time = time.time()
            while time.time() - start_time < timeout:
                ret, frame = self.cap.read()
                if ret and frame is not None:
                    logger.info("RTMP流连接成功")
                    return True
                time.sleep(0.1)
            
            logger.error("RTMP流连接超时")
            return False
            
        except Exception as e:
            logger.error(f"连接RTMP流失败: {e}")
            return False
    
    def process_stream(self, frame_callback=None, max_frames: int = 0):
        """
        处理RTMP流
        
        Args:
            frame_callback: 帧处理回调函数
            max_frames: 最大处理帧数（0表示无限制）
        """
        if not self.cap or not self.cap.isOpened():
            logger.error("RTMP流未连接")
            return
        
        self.is_running = True
        frame_count = 0
        
        logger.info("开始处理RTMP流...")
        
        try:
            while self.is_running:
                ret, frame = self.cap.read()
                
                if not ret or frame is None:
                    logger.warning("读取帧失败，尝试重连...")
                    if not self._reconnect():
                        break
                    continue
                
                frame_count += 1
                
                # 处理帧
                if frame_callback:
                    try:
                        frame_callback(frame, frame_count)
                    except Exception as e:
                        logger.error(f"帧处理回调出错: {e}")
                
                # 检查最大帧数限制
                if max_frames > 0 and frame_count >= max_frames:
                    logger.info(f"达到最大帧数限制: {max_frames}")
                    break
                
                # 简单的帧率控制
                time.sleep(0.033)  # 约30FPS
                
        except KeyboardInterrupt:
            logger.info("用户中断处理")
        except Exception as e:
            logger.error(f"流处理出错: {e}")
        finally:
            self.stop()
    
    def _reconnect(self, max_attempts: int = 3) -> bool:
        """
        重新连接RTMP流
        
        Args:
            max_attempts: 最大重连次数
            
        Returns:
            bool: 是否重连成功
        """
        for attempt in range(max_attempts):
            logger.info(f"重连尝试 {attempt + 1}/{max_attempts}")
            
            if self.cap:
                self.cap.release()
            
            time.sleep(2)  # 等待2秒再重连
            
            if self.connect_stream():
                return True
        
        logger.error("重连失败")
        return False
    
    def stop(self):
        """停止处理"""
        logger.info("停止RTMP流处理")
        self.is_running = False
        
        if self.cap:
            self.cap.release()
            self.cap = None
    
    def get_stream_info(self) -> dict:
        """
        获取流信息
        
        Returns:
            dict: 流信息
        """
        if not self.cap or not self.cap.isOpened():
            return {}
        
        info = {
            'width': int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            'height': int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            'fps': self.cap.get(cv2.CAP_PROP_FPS),
            'frame_count': int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            'codec': int(self.cap.get(cv2.CAP_PROP_FOURCC))
        }
        
        return info

def simple_frame_processor(frame: np.ndarray, frame_number: int):
    """
    简单的帧处理函数示例
    
    Args:
        frame: 视频帧
        frame_number: 帧编号
    """
    # 获取帧信息
    height, width, channels = frame.shape
    
    # 每100帧输出一次信息
    if frame_number % 100 == 0:
        logger.info(f"处理帧 {frame_number}, 分辨率: {width}x{height}")
    
    # 这里可以添加AI推理代码
    # 例如：目标检测、图像分类等
    
    # 示例：检测图像亮度
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = np.mean(gray)
    
    if frame_number % 300 == 0:  # 每10秒输出一次（假设30fps）
        logger.info(f"帧 {frame_number} 平均亮度: {brightness:.2f}")

def ai_inference_processor(frame: np.ndarray, frame_number: int):
    """
    AI推理处理函数示例
    
    Args:
        frame: 视频帧
        frame_number: 帧编号
    """
    # 这里是一个更复杂的处理示例
    # 可以集成YOLO等AI模型
    
    # 示例：边缘检测
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edge_count = np.sum(edges > 0)
    
    if frame_number % 100 == 0:
        logger.info(f"帧 {frame_number} 边缘像素数: {edge_count}")
    
    # 这里可以添加实际的AI推理代码
    # 例如：
    # - YOLO目标检测
    # - 人脸检测
    # - 动作识别
    # - 异常检测等

def main():
    """主函数"""
    # 从环境变量读取RTMP配置
    rtmp_host = os.getenv('CLEANSIGHT_RTMP_SERVER_HOST', 'localhost')
    rtmp_port = os.getenv('CLEANSIGHT_RTMP_SERVER_PORT', '1935')
    
    # 构建RTMP URL
    stream_name = "camera01"  # 可以从命令行参数获取
    rtmp_url = f"rtmp://{rtmp_host}:{rtmp_port}/live/{stream_name}"
    
    print(f"RTMP流地址: {rtmp_url}")
    print("请确保:")
    print("1. nginx-rtmp服务正在运行")
    print("2. 有摄像头或其他设备正在推流到该地址")
    print("3. 网络连接正常")
    print()
    
    # 创建流处理器
    processor = RTMPStreamProcessor(rtmp_url, stream_name)
    
    try:
        # 连接流
        if not processor.connect_stream():
            print("无法连接到RTMP流")
            return
        
        # 获取流信息
        stream_info = processor.get_stream_info()
        if stream_info:
            print("流信息:")
            for key, value in stream_info.items():
                print(f"  {key}: {value}")
            print()
        
        # 选择处理函数
        print("选择处理模式:")
        print("1. 简单处理（亮度检测）")
        print("2. AI推理处理（边缘检测示例）")
        choice = input("请输入选择 (1 或 2): ").strip()
        
        if choice == "2":
            callback = ai_inference_processor
        else:
            callback = simple_frame_processor
        
        # 开始处理流
        processor.process_stream(frame_callback=callback)
        
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"处理出错: {e}")
    finally:
        processor.stop()
        print("处理完成")

if __name__ == "__main__":
    main()