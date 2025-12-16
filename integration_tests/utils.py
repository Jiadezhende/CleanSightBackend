"""
集成测试工具函数模块

提供数据库操作、ffmpeg 控制、WebSocket 连接等工具函数
"""
import os
import sys
import subprocess
import requests
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from urllib.parse import urlparse

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_db
from app.models.task import DBTask
from sqlalchemy import text


class FFmpegController:
    """FFmpeg 推流控制器"""
    
    def __init__(self, video_path: str, stream_url: str, protocol: str = "rtmp"):
        self.video_path = video_path
        self.stream_url = stream_url
        self.protocol = protocol.lower()
        self.process: Optional[subprocess.Popen] = None
        
        # 尝试查找 ffmpeg
        self.ffmpeg_path = self._find_ffmpeg()
    
    def _find_ffmpeg(self) -> str:
        """查找 ffmpeg 可执行文件"""
        # 优先使用 Chocolatey 安装的版本
        choco_path = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
        if os.path.exists(choco_path):
            return choco_path
        
        # 尝试系统 PATH
        try:
            result = subprocess.run(['ffmpeg', '-version'], 
                                  capture_output=True, 
                                  timeout=2)
            if result.returncode == 0:
                return 'ffmpeg'
        except:
            pass
        
        raise FileNotFoundError("未找到 ffmpeg，请确保已安装")
    
    def start(self) -> bool:
        """启动 ffmpeg 推流"""
        if not Path(self.video_path).exists():
            print(f"❌ 测试视频不存在: {self.video_path}")
            return False

        if self.protocol == "rtmp":
            cmd = [
                self.ffmpeg_path,
                '-re',
                '-stream_loop', '-1',  # 循环播放
                '-i', self.video_path,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-f', 'flv',
                self.stream_url
            ]
        elif self.protocol == "rtsp":
            cmd = [
                self.ffmpeg_path,
                '-an',
                '-re',
                '-stream_loop', '-1',  # 循环播放
                '-i', self.video_path,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-rtsp_transport', 'tcp',  # RTSP over TCP
                '-f', 'rtsp',
                self.stream_url
            ]
        else:
            print(f"❌ 不支持的协议: {self.protocol}")
            return False
        
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            # 打印命令以便调试
            try:
                print("FFmpeg cmd:", " ".join(cmd))
            except Exception:
                pass

            # 在 Ubuntu 上捕获 stderr 以便诊断失败原因
            # 不将 stderr 保留为 PIPE（若不读取会导致缓冲区填满，从而使 ffmpeg 阻塞）
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags
            )

            # 等待推流建立
            print(f"正在向{self.stream_url}推流...")

            time.sleep(5)

            if self.process.poll() is not None:
                # 读取并打印 stderr 帮助定位错误（进程已退出）
                stderr = ""
                try:
                    if self.process.stderr:
                        stderr = self.process.stderr.read().decode(errors='ignore')
                except Exception:
                    stderr = "<failed to read stderr>"

                print(f"❌ ffmpeg 推流进程已退出 (退出码: {self.process.returncode})")
                if stderr:
                    print("ffmpeg stderr:\n", stderr)
                return False

            print(f"✅ ffmpeg {self.protocol.upper()} 推流已启动: {self.stream_url}")
            return True
        except Exception as e:
            print(f"❌ 启动 ffmpeg 失败: {e}")
            return False
    
    def stop(self):
        """停止 ffmpeg 推流"""
        if self.process:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                        print("✅ ffmpeg 推流已停止")
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                        print("⚠️ ffmpeg 强制停止")
            except Exception as e:
                print(f"⚠️ 停止 ffmpeg 失败: {e}")
    
    def is_running(self) -> bool:
        """检查 ffmpeg 是否运行"""
        return self.process is not None and self.process.poll() is None


class DatabaseHelper:
    """数据库操作辅助类"""
    
    @staticmethod
    def get_task(task_id: int) -> Optional[DBTask]:
        """从数据库获取任务"""
        db = next(get_db())
        try:
            task = db.query(DBTask).filter(DBTask.task_id == task_id).first()
            return task
        finally:
            db.close()
    
    @staticmethod
    def create_test_task(task_id: int = 0, source_ip: str = "test") -> bool:
        """创建测试任务（如果不存在）"""
        db = next(get_db())
        try:
            existing = db.query(DBTask).filter(DBTask.task_id == task_id).first()
            if existing:
                print(f"✅ 任务 {task_id} 已存在")
                return True
            
            now_ts = int(time.time())
            new_task = DBTask(
                task_id=task_id,
                source_ip=source_ip,
                current_step="0",
                status="paused",
                created_at=now_ts,
                updated_at=now_ts,
                start_time=0,
                end_time=0
            )
            
            db.add(new_task)
            db.commit()
            print(f"✅ 创建测试任务 {task_id}")
            return True
        except Exception as e:
            db.rollback()
            print(f"❌ 创建测试任务失败: {e}")
            return False
        finally:
            db.close()
    
    @staticmethod
    def update_task_status(task_id: int, status: str) -> bool:
        """更新任务状态"""
        db = next(get_db())
        try:
            task = db.query(DBTask).filter(DBTask.task_id == task_id).first()
            if not task:
                return False
            
            task.status = status  # type: ignore
            task.updated_at = int(time.time())  # type: ignore
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            print(f"❌ 更新任务状态失败: {e}")
            return False
        finally:
            db.close()
    
    @staticmethod
    def cleanup_test_task(task_id: int):
        """清理测试任务（可选）"""
        db = next(get_db())
        try:
            db.execute(text(f"DELETE FROM task WHERE task_id = {task_id}"))
            db.commit()
            print(f"✅ 清理测试任务 {task_id}")
        except Exception as e:
            db.rollback()
            print(f"⚠️ 清理测试任务失败: {e}")
        finally:
            db.close()


class APIClient:
    """后端 API 客户端"""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
    
    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """通用请求方法"""
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(method, url, timeout=10, **kwargs)
            response.raise_for_status()
            try:
                return response.json()
            except Exception:
                return {"text": response.text}
        except Exception as e:
            # 捕获超时/网络错误，返回包含错误信息的结构，调用方可据此优雅停止任务
            return {"error": str(e)}
    
    def check_health(self) -> bool:
        """检查 API 是否可用"""
        try:
            response = requests.get(f"{self.base_url}/ai/status", timeout=2)
            return response.status_code == 200
        except Exception:
            return False
    
    def start_rtmp_capture(self, client_id: str, rtmp_url: str, fps: int = 30) -> Dict[str, Any]:
        """启动 RTMP 捕获"""
        url = f"{self.base_url}/inspection/start_rtmp_stream"
        payload = {
            "client_id": client_id,
            "rtmp_url": rtmp_url,
            "fps": fps
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def stop_rtmp_capture(self, client_id: str) -> Dict[str, Any]:
        """停止 RTMP 捕获"""
        url = f"{self.base_url}/inspection/stop_rtmp_stream?client_id={client_id}"
        try:
            response = requests.post(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def start_rtsp_capture(self, client_id: str, rtsp_url: str, fps: int = 30) -> Dict[str, Any]:
        """启动 RTSP 捕获"""
        url = f"{self.base_url}/inspection/start_rtsp_stream"
        payload = {
            "client_id": client_id,
            "rtsp_url": rtsp_url,
            "fps": fps
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def stop_rtsp_capture(self, client_id: str) -> Dict[str, Any]:
        """停止 RTSP 捕获"""
        url = f"{self.base_url}/inspection/stop_rtsp_stream?client_id={client_id}"
        try:
            response = requests.post(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def start_task(self, task_id: int) -> Dict[str, Any]:
        """加载任务到 AI 服务"""
        url = f"{self.base_url}/ai/load_task/{task_id}"
        try:
            response = requests.get(url, timeout=10)
            print(f"加载任务响应: {response.text}")
            response.raise_for_status()
            try:
                return response.json()
            except Exception:
                return {"text": response.text}
        except Exception as e:
            return {"error": str(e)}
    
    def terminate_task(self, client_id: str) -> Dict[str, Any]:
        """终止任务（通过 client_id）"""
        url = f"{self.base_url}/ai/terminate_task/{client_id}"
        try:
            response = requests.post(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def get_ai_status(self) -> Dict[str, Any]:
        """获取 AI 服务状态"""
        url = f"{self.base_url}/ai/status"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}


def check_hls_files(client_id: str, task_id: int) -> Dict[str, Any]:
    """检查 HLS 文件是否生成"""
    base_dir = Path(__file__).parent.parent / "database"
    
    # 根据任务查找目录
    task_dir = base_dir / f"task_{task_id}" / client_id / "hls"
    
    if not task_dir.exists():
        # 尝试查找其他可能的路径
        for subdir in base_dir.rglob("hls"):
            if client_id in str(subdir):
                task_dir = subdir
                break
    
    result = {
        "exists": task_dir.exists(),
        "path": str(task_dir),
        "segments": [],
        "keypoints": [],
        "playlists": []
    }
    
    if task_dir.exists():
        # 查找视频段
        result["segments"] = [str(f) for f in task_dir.glob("*_segment_*.mp4")]
        # 查找关键点文件
        result["keypoints"] = [str(f) for f in task_dir.glob("keypoints_*.json")]
        # 查找播放列表
        result["playlists"] = [str(f) for f in task_dir.glob("*.m3u8")]
    
    return result


def wait_for_condition(condition_func, timeout: int = 30, interval: float = 1.0, 
                       description: str = "条件满足") -> bool:
    """等待条件满足"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition_func():
            return True
        time.sleep(interval)
    
    print(f"⏱️ 超时：{description} 未在 {timeout} 秒内满足")
    return False
