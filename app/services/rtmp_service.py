"""
RTMP流服务模块
与nginx-rtmp服务集成，提供流管理和监控功能
"""

import os
import logging
import subprocess
import requests
import cv2
import time
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime
import threading
import queue

logger = logging.getLogger(__name__)

@dataclass
class StreamInfo:
    """流信息"""
    name: str
    url: str
    status: str
    start_time: Optional[datetime] = None
    frame_count: int = 0
    last_activity: Optional[datetime] = None

class RTMPService:
    """RTMP服务管理器"""
    
    def __init__(self):
        """初始化RTMP服务"""
        self.rtmp_host = os.getenv('CLEANSIGHT_RTMP_SERVER_HOST', 'localhost')
        self.rtmp_port = int(os.getenv('CLEANSIGHT_RTMP_SERVER_PORT', 1935))
        self.rtmp_base_url = os.getenv(
            'CLEANSIGHT_RTMP_BASE_URL', 
            f'rtmp://{self.rtmp_host}:{self.rtmp_port}/live'
        )
        
        self.active_streams: Dict[str, StreamInfo] = {}
        self.stream_processors: Dict[str, 'StreamProcessor'] = {}
        
    def get_stream_url(self, stream_name: str) -> str:
        """
        获取流URL
        
        Args:
            stream_name: 流名称
            
        Returns:
            str: 完整的RTMP流URL
        """
        return f"{self.rtmp_base_url}/{stream_name}"
    
    def check_service_status(self) -> Dict[str, Any]:
        """
        检查RTMP服务状态
        
        Returns:
            dict: 服务状态信息
        """
        try:
            # 检查统计页面
            stat_url = f"http://{self.rtmp_host}/stat"
            response = requests.get(stat_url, timeout=5)
            
            if response.status_code == 200:
                return {
                    'status': 'running',
                    'host': self.rtmp_host,
                    'port': self.rtmp_port,
                    'stat_url': stat_url,
                    'last_check': datetime.now().isoformat()
                }
            else:
                return {
                    'status': 'error',
                    'message': f'HTTP {response.status_code}',
                    'last_check': datetime.now().isoformat()
                }
                
        except requests.RequestException as e:
            return {
                'status': 'offline',
                'message': str(e),
                'last_check': datetime.now().isoformat()
            }
    
    def list_active_streams(self) -> List[Dict[str, Any]]:
        """
        获取活跃流列表
        
        Returns:
            list: 活跃流信息列表
        """
        try:
            stat_url = f"http://{self.rtmp_host}/stat"
            response = requests.get(stat_url, timeout=5)
            
            if response.status_code == 200:
                # 这里需要解析XML统计信息
                # 简化版本，返回当前追踪的流
                return [
                    {
                        'name': stream_info.name,
                        'url': stream_info.url,
                        'status': stream_info.status,
                        'start_time': stream_info.start_time.isoformat() if stream_info.start_time else None,
                        'frame_count': stream_info.frame_count,
                        'last_activity': stream_info.last_activity.isoformat() if stream_info.last_activity else None
                    }
                    for stream_info in self.active_streams.values()
                ]
            
        except requests.RequestException:
            pass
        
        return []
    
    def test_stream_connection(self, stream_name: str, timeout: int = 10) -> bool:
        """
        测试流连接
        
        Args:
            stream_name: 流名称
            timeout: 超时时间（秒）
            
        Returns:
            bool: 连接是否成功
        """
        stream_url = self.get_stream_url(stream_name)
        
        try:
            cap = cv2.VideoCapture(stream_url)
            
            if not cap.isOpened():
                return False
            
            # 尝试读取一帧
            start_time = time.time()
            while time.time() - start_time < timeout:
                ret, frame = cap.read()
                if ret and frame is not None:
                    cap.release()
                    return True
                time.sleep(0.1)
            
            cap.release()
            return False
            
        except Exception as e:
            logger.error(f"测试流连接失败: {e}")
            return False
    
    def start_stream_processor(self, stream_name: str, callback=None) -> bool:
        """
        启动流处理器
        
        Args:
            stream_name: 流名称
            callback: 帧处理回调函数
            
        Returns:
            bool: 是否启动成功
        """
        if stream_name in self.stream_processors:
            logger.warning(f"流处理器已存在: {stream_name}")
            return False
        
        stream_url = self.get_stream_url(stream_name)
        processor = StreamProcessor(stream_name, stream_url, callback)
        
        if processor.start():
            self.stream_processors[stream_name] = processor
            self.active_streams[stream_name] = StreamInfo(
                name=stream_name,
                url=stream_url,
                status='processing',
                start_time=datetime.now()
            )
            logger.info(f"流处理器启动成功: {stream_name}")
            return True
        else:
            logger.error(f"流处理器启动失败: {stream_name}")
            return False
    
    def stop_stream_processor(self, stream_name: str) -> bool:
        """
        停止流处理器
        
        Args:
            stream_name: 流名称
            
        Returns:
            bool: 是否停止成功
        """
        if stream_name not in self.stream_processors:
            logger.warning(f"流处理器不存在: {stream_name}")
            return False
        
        processor = self.stream_processors.pop(stream_name)
        processor.stop()
        
        if stream_name in self.active_streams:
            self.active_streams[stream_name].status = 'stopped'
        
        logger.info(f"流处理器已停止: {stream_name}")
        return True
    
    def get_stream_stats(self, stream_name: str) -> Optional[Dict[str, Any]]:
        """
        获取流统计信息
        
        Args:
            stream_name: 流名称
            
        Returns:
            dict: 流统计信息，如果流不存在则返回None
        """
        if stream_name not in self.active_streams:
            return None
        
        stream_info = self.active_streams[stream_name]
        processor = self.stream_processors.get(stream_name)
        
        stats = {
            'name': stream_info.name,
            'url': stream_info.url,
            'status': stream_info.status,
            'start_time': stream_info.start_time.isoformat() if stream_info.start_time else None,
            'frame_count': stream_info.frame_count,
            'last_activity': stream_info.last_activity.isoformat() if stream_info.last_activity else None
        }
        
        if processor:
            stats.update({
                'is_running': processor.is_running,
                'error_count': processor.error_count,
                'fps': processor.current_fps
            })
        
        return stats

class StreamProcessor:
    """流处理器"""
    
    def __init__(self, stream_name: str, stream_url: str, callback=None):
        """
        初始化流处理器
        
        Args:
            stream_name: 流名称
            stream_url: 流URL
            callback: 帧处理回调函数
        """
        self.stream_name = stream_name
        self.stream_url = stream_url
        self.callback = callback
        
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self.cap: Optional[cv2.VideoCapture] = None
        
        self.frame_count = 0
        self.error_count = 0
        self.current_fps = 0.0
        self.last_frame_time = 0.0
        
        self.frame_queue = queue.Queue(maxsize=10)
    
    def start(self) -> bool:
        """
        启动流处理
        
        Returns:
            bool: 是否启动成功
        """
        if self.is_running:
            return False
        
        try:
            self.cap = cv2.VideoCapture(self.stream_url)
            if not self.cap.isOpened():
                logger.error(f"无法打开流: {self.stream_url}")
                return False
            
            # 设置缓冲区大小
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            
            self.is_running = True
            self.thread = threading.Thread(target=self._process_loop, daemon=True)
            self.thread.start()
            
            logger.info(f"流处理器启动: {self.stream_name}")
            return True
            
        except Exception as e:
            logger.error(f"启动流处理器失败: {e}")
            return False
    
    def stop(self):
        """停止流处理"""
        self.is_running = False
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        
        if self.cap:
            self.cap.release()
            self.cap = None
        
        logger.info(f"流处理器已停止: {self.stream_name}")
    
    def _process_loop(self):
        """处理循环"""
        last_fps_time = time.time()
        fps_frame_count = 0
        
        while self.is_running:
            try:
                ret, frame = self.cap.read()
                
                if not ret or frame is None:
                    logger.warning(f"读取帧失败: {self.stream_name}")
                    self.error_count += 1
                    
                    # 尝试重连
                    if not self._reconnect():
                        break
                    continue
                
                self.frame_count += 1
                fps_frame_count += 1
                
                # 计算FPS
                current_time = time.time()
                if current_time - last_fps_time >= 1.0:
                    self.current_fps = fps_frame_count / (current_time - last_fps_time)
                    fps_frame_count = 0
                    last_fps_time = current_time
                
                # 处理帧
                if self.callback:
                    try:
                        self.callback(frame, self.frame_count, self.stream_name)
                    except Exception as e:
                        logger.error(f"帧处理回调出错: {e}")
                        self.error_count += 1
                
                # 更新活动时间
                self.last_frame_time = current_time
                
            except Exception as e:
                logger.error(f"处理循环出错: {e}")
                self.error_count += 1
                time.sleep(1)  # 避免快速错误循环
    
    def _reconnect(self, max_attempts: int = 3) -> bool:
        """
        重新连接流
        
        Args:
            max_attempts: 最大重连次数
            
        Returns:
            bool: 是否重连成功
        """
        for attempt in range(max_attempts):
            logger.info(f"重连尝试 {attempt + 1}/{max_attempts}: {self.stream_name}")
            
            if self.cap:
                self.cap.release()
            
            time.sleep(2)  # 等待2秒再重连
            
            try:
                self.cap = cv2.VideoCapture(self.stream_url)
                if self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    logger.info(f"重连成功: {self.stream_name}")
                    return True
            except Exception as e:
                logger.error(f"重连失败: {e}")
        
        logger.error(f"重连失败，停止处理: {self.stream_name}")
        return False

# 全局RTMP服务实例
rtmp_service = RTMPService()