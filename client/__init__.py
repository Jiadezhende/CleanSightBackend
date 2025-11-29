"""
CleanSight摄像头客户端包

提供从摄像头采集视频并上传到服务器的功能。

使用示例:
    from camera_client import CameraClient
    
    client = CameraClient(client_id="camera_001")
    client.start()
    # ... 运行一段时间 ...
    client.stop()
"""

from .camera_client import CameraClient

__version__ = "1.0.0"
__all__ = ["CameraClient"]
