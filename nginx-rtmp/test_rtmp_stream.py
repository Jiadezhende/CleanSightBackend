#!/usr/bin/env python3
"""
RTMP推流测试工具
用于测试nginx-rtmp服务是否正常工作
"""

import cv2
import os
import time
import logging
import argparse
import subprocess
from typing import Optional
import numpy as np

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RTMPStreamTester:
    """RTMP流测试器"""
    
    def __init__(self, rtmp_url: str):
        """
        初始化RTMP流测试器
        
        Args:
            rtmp_url: RTMP推流地址
        """
        self.rtmp_url = rtmp_url
        self.writer: Optional[cv2.VideoWriter] = None
    
    def test_push_from_camera(self, camera_id: int = 0, duration: int = 30):
        """
        从摄像头推流测试
        
        Args:
            camera_id: 摄像头ID
            duration: 推流时长（秒）
        """
        logger.info(f"开始摄像头推流测试，时长: {duration}秒")
        
        # 打开摄像头
        cap = cv2.VideoCapture(camera_id)
        if not cap.isOpened():
            logger.error(f"无法打开摄像头 {camera_id}")
            return False
        
        # 获取摄像头参数
        fps = int(cap.get(cv2.CAP_PROP_FPS)) or 30
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        logger.info(f"摄像头参数: {width}x{height}@{fps}fps")
        
        # 创建推流编码器
        fourcc = cv2.VideoWriter_fourcc(*'H264')
        
        # 修改RTMP URL格式以适应推流
        rtmp_push_url = self.rtmp_url
        if not rtmp_push_url.startswith('rtmp://'):
            rtmp_push_url = f"rtmp://{rtmp_push_url}"
        
        self.writer = cv2.VideoWriter(
            rtmp_push_url,
            fourcc,
            fps,
            (width, height)
        )
        
        if not self.writer.isOpened():
            logger.error("无法创建RTMP推流")
            cap.release()
            return False
        
        logger.info(f"开始推流到: {rtmp_push_url}")
        
        start_time = time.time()
        frame_count = 0
        
        try:
            while time.time() - start_time < duration:
                ret, frame = cap.read()
                if not ret:
                    logger.warning("读取摄像头帧失败")
                    break
                
                # 添加时间戳文字
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                cv2.putText(frame, timestamp, (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                
                # 添加帧计数
                cv2.putText(frame, f"Frame: {frame_count}", (10, 70),
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                
                # 推流
                self.writer.write(frame)
                frame_count += 1
                
                if frame_count % (fps * 5) == 0:  # 每5秒输出一次
                    logger.info(f"已推流 {frame_count} 帧")
                
                # 控制帧率
                time.sleep(1.0 / fps)
                
        except KeyboardInterrupt:
            logger.info("用户中断推流")
        except Exception as e:
            logger.error(f"推流出错: {e}")
        finally:
            cap.release()
            if self.writer:
                self.writer.release()
        
        logger.info(f"推流测试完成，共推流 {frame_count} 帧")
        return True
    
    def test_push_from_file(self, video_file: str, loop: bool = True, duration: int = 60):
        """
        从视频文件推流测试
        
        Args:
            video_file: 视频文件路径
            loop: 是否循环播放
            duration: 推流时长（秒）
        """
        logger.info(f"开始视频文件推流测试: {video_file}")
        
        if not os.path.exists(video_file):
            logger.error(f"视频文件不存在: {video_file}")
            return False
        
        cap = cv2.VideoCapture(video_file)
        if not cap.isOpened():
            logger.error(f"无法打开视频文件: {video_file}")
            return False
        
        # 获取视频参数
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        logger.info(f"视频参数: {width}x{height}@{fps:.2f}fps, 总帧数: {total_frames}")
        
        # 使用FFmpeg推流（更稳定）
        self._push_with_ffmpeg(video_file, loop, duration)
        
        cap.release()
        return True
    
    def _push_with_ffmpeg(self, video_file: str, loop: bool, duration: int):
        """
        使用FFmpeg推流
        
        Args:
            video_file: 视频文件路径
            loop: 是否循环播放
            duration: 推流时长（秒）
        """
        cmd = [
            'ffmpeg',
            '-re',  # 以原始帧率读取
        ]
        
        if loop:
            cmd.extend(['-stream_loop', '-1'])  # 无限循环
        
        cmd.extend([
            '-i', video_file,
            '-c:v', 'libx264',  # 视频编码器
            '-preset', 'ultrafast',  # 编码预设
            '-tune', 'zerolatency',  # 低延迟优化
            '-c:a', 'aac',  # 音频编码器
            '-strict', 'experimental',
            '-f', 'flv',  # 输出格式
        ])
        
        if duration > 0:
            cmd.extend(['-t', str(duration)])  # 限制推流时长
        
        cmd.append(self.rtmp_url)
        
        logger.info(f"FFmpeg命令: {' '.join(cmd)}")
        
        try:
            process = subprocess.run(cmd, check=True, capture_output=True, text=True)
            logger.info("FFmpeg推流完成")
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg推流失败: {e}")
            if e.stderr:
                logger.error(f"错误输出: {e.stderr}")
        except FileNotFoundError:
            logger.error("FFmpeg未安装或不在PATH中")
    
    def generate_test_video(self, output_file: str = "test_video.mp4", duration: int = 30):
        """
        生成测试视频
        
        Args:
            output_file: 输出文件名
            duration: 视频时长（秒）
        """
        logger.info(f"生成测试视频: {output_file}")
        
        fps = 30
        width, height = 640, 480
        total_frames = fps * duration
        
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_file, fourcc, fps, (width, height))
        
        if not out.isOpened():
            logger.error("无法创建视频文件")
            return False
        
        for i in range(total_frames):
            # 创建彩色测试图案
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            
            # 渐变背景
            for y in range(height):
                frame[y, :, 0] = (y * 255) // height  # 蓝色渐变
                frame[y, :, 1] = ((height - y) * 255) // height  # 绿色渐变
            
            # 移动的白色方块
            block_size = 50
            x = int((i * 2) % (width - block_size))
            y = int((i * 1) % (height - block_size))
            frame[y:y+block_size, x:x+block_size] = (255, 255, 255)
            
            # 添加文字信息
            text = f"Frame: {i+1}/{total_frames}"
            cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            
            timestamp = f"Time: {i/fps:.1f}s"
            cv2.putText(frame, timestamp, (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            
            out.write(frame)
            
            if (i + 1) % (fps * 5) == 0:  # 每5秒输出进度
                logger.info(f"生成进度: {(i+1)/total_frames*100:.1f}%")
        
        out.release()
        logger.info(f"测试视频生成完成: {output_file}")
        return True

def test_rtmp_connection(rtmp_url: str) -> bool:
    """
    测试RTMP连接
    
    Args:
        rtmp_url: RTMP地址
        
    Returns:
        bool: 连接是否成功
    """
    logger.info(f"测试RTMP连接: {rtmp_url}")
    
    try:
        # 尝试用opencv连接
        cap = cv2.VideoCapture(rtmp_url)
        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            if ret:
                logger.info("RTMP连接测试成功")
                return True
        
        logger.warning("RTMP连接测试失败")
        return False
    except Exception as e:
        logger.error(f"RTMP连接测试出错: {e}")
        return False

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='RTMP推流测试工具')
    parser.add_argument('--host', default='localhost', help='RTMP服务器地址')
    parser.add_argument('--port', default=1935, type=int, help='RTMP服务器端口')
    parser.add_argument('--stream', default='test', help='流名称')
    parser.add_argument('--camera', type=int, default=0, help='摄像头ID')
    parser.add_argument('--file', help='视频文件路径')
    parser.add_argument('--duration', type=int, default=30, help='推流时长（秒）')
    parser.add_argument('--generate', action='store_true', help='生成测试视频')
    
    args = parser.parse_args()
    
    # 构建RTMP URL
    rtmp_url = f"rtmp://{args.host}:{args.port}/live/{args.stream}"
    
    print(f"RTMP推流测试工具")
    print(f"目标地址: {rtmp_url}")
    print(f"推流时长: {args.duration}秒")
    print()
    
    tester = RTMPStreamTester(rtmp_url)
    
    try:
        if args.generate:
            # 生成测试视频
            if tester.generate_test_video(duration=args.duration):
                print("测试视频生成完成: test_video.mp4")
                print("可以使用 --file test_video.mp4 参数推流")
        elif args.file:
            # 从文件推流
            tester.test_push_from_file(args.file, duration=args.duration)
        else:
            # 从摄像头推流
            tester.test_push_from_camera(args.camera, args.duration)
            
    except KeyboardInterrupt:
        print("\n测试被用户中断")
    except Exception as e:
        print(f"测试出错: {e}")
    
    print("测试完成")

if __name__ == "__main__":
    main()