"""
集成测试工具函数模块

提供数据库操作、ffmpeg 控制、WebSocket 连接等工具函数
"""

import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from app.database import get_db
from app.models import DBAlarm, DBTask


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
        """使用项目自包含的 ffmpeg（与后端同源 settings.ffmpeg_path，install 脚本部署到 .ffmpeg/bin/）。

        不再走系统 PATH / Chocolatey：统一用项目内钉版二进制，避免测试与生产 ffmpeg 版本漂移。
        可用 CLEANSIGHT_FFMPEG_PATH 覆写（逃生口，如裸名走 PATH）。
        """
        from app.settings import settings

        ffmpeg = settings.ffmpeg_path
        # 显式路径（含分隔符）必须存在；裸名（PATH 逃生口）交给系统解析
        has_sep = os.sep in ffmpeg or bool(os.altsep and os.altsep in ffmpeg)
        if has_sep and not Path(ffmpeg).exists():
            raise FileNotFoundError(
                f"未找到项目内 ffmpeg: {ffmpeg}\n"
                f"请先运行 install.sh / install.ps1 部署 .ffmpeg/，"
                f"或设置 CLEANSIGHT_FFMPEG_PATH 指向可用 ffmpeg"
            )
        return ffmpeg

    def start(self) -> bool:
        """启动 ffmpeg 推流"""
        if not Path(self.video_path).exists():
            print(f"❌ 测试视频不存在: {self.video_path}")
            return False

        if self.protocol == "rtmp":
            cmd = [
                self.ffmpeg_path,
                "-re",
                "-stream_loop",
                "-1",  # 循环播放
                "-i",
                self.video_path,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-f",
                "flv",
                self.stream_url,
            ]
        elif self.protocol == "rtsp":
            cmd = [
                self.ffmpeg_path,
                "-an",
                "-re",
                "-stream_loop",
                "-1",  # 循环播放
                "-i",
                self.video_path,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "30",  # GOP=30帧(~1s一个关键帧)：小 GOP 让新 reader 快速拿到关键帧，逼近低延迟实况
                "-rtsp_transport",
                "tcp",  # RTSP over TCP
                "-f",
                "rtsp",
                self.stream_url,
            ]
        else:
            print(f"❌ 不支持的协议: {self.protocol}")
            return False

        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
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
                creationflags=creation_flags,
            )

            # 等待推流建立
            print(f"正在向{self.stream_url}推流...")

            time.sleep(5)

            if self.process.poll() is not None:
                # 读取并打印 stderr 帮助定位错误（进程已退出）
                stderr = ""
                try:
                    if self.process.stderr:
                        stderr = self.process.stderr.read().decode(errors="ignore")
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
        except Exception as e:
            print(f"❌ 获取任务失败: {e}")
            return None
        finally:
            db.close()

    @staticmethod
    def create_test_task(task_id: int = 0, source_ip: str = "test", current_step: str = "1") -> bool:
        """创建测试任务（如果不存在）

        Returns:
            True  — 新创建了任务
            False — 任务已存在或创建失败
        """
        db = next(get_db())
        try:
            existing = db.query(DBTask).filter(DBTask.task_id == task_id).first()
            if existing:
                print(f"✅ 任务 {task_id} 已存在")
                return False

            now_ts = int(time.time())
            new_task = DBTask(
                _id=uuid.uuid4().hex,
                cls_id="691dd1a8279461135967c843",
                task_id=task_id,
                source_ip=source_ip,
                current_step=current_step,
                status="paused",
                updated_time=now_ts,
                start_time=now_ts,
                end_time=0,
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
    def update_task_step(task_id: int, new_step: str) -> bool:
        """更新任务 current_step（模拟任务阶段推进）"""
        db = next(get_db())
        try:
            task = db.query(DBTask).filter(DBTask.task_id == task_id).first()
            if not task:
                return False
            task.current_step = new_step  # type: ignore
            task.updated_time = int(time.time())  # type: ignore
            db.commit()
            print(f"✅ 任务 {task_id} current_step → {new_step}")
            return True
        except Exception as e:
            db.rollback()
            print(f"❌ 更新 current_step 失败: {e}")
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
            task.updated_time = int(time.time())  # type: ignore
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
        """清理测试任务"""
        db = next(get_db())
        try:
            deleted = (
                db.query(DBTask).filter(DBTask.task_id == task_id).delete()
            )
            db.commit()
            if deleted:
                print(f"✅ 清理测试任务 {task_id}")
            else:
                print(f"⚠️ 测试任务 {task_id} 不存在，无需清理")
        except Exception as e:
            db.rollback()
            print(f"⚠️ 清理测试任务失败: {e}")
        finally:
            db.close()

    @staticmethod
    def create_test_alarm(
        alarm_id: int,
        task_id: int,
        detected_at_ms: int,
        alarm_type: str = "bubble",
        severity: str = "high",
        message: str = "test alarm",
        step_id: int = 1,
        step_name: str = "测漏",
    ) -> bool:
        """创建测试告警记录。使用原生 SQL 以填写平台必填字段 cls_id。

        Returns:
            True  — 新创建了告警
            False — 告警已存在或创建失败
        """
        db = next(get_db())
        try:
            existing = db.query(DBAlarm).filter(DBAlarm.alarm_id == alarm_id).first()
            if existing:
                print(f"✅ 告警 {alarm_id} 已存在")
                return False

            now_ts = int(time.time())
            db.execute(
                text("""
                    INSERT INTO clean_alarm
                        (_id, cls_id, alarm_id, task_id, step_id, step_name,
                         alarm_type, severity, message, detected_at,
                         resolved, resolved_by, resolved_at, create_time)
                    VALUES
                        (:_id, :cls_id, :alarm_id, :task_id, :step_id, :step_name,
                         :alarm_type, :severity, :message, :detected_at,
                         :resolved, :resolved_by, :resolved_at, :create_time)
                """),
                {
                    "_id": uuid.uuid4().hex,
                    "cls_id": "691e1d83279461135967c890",
                    "alarm_id": alarm_id,
                    "task_id": task_id,
                    "step_id": step_id,
                    "step_name": step_name,
                    "alarm_type": alarm_type,
                    "severity": severity,
                    "message": message,
                    "detected_at": detected_at_ms,
                    "resolved": False,
                    "resolved_by": None,
                    "resolved_at": None,
                    "create_time": now_ts,
                },
            )
            db.commit()
            print(f"✅ 创建测试告警 {alarm_id} (task_id={task_id}, detected_at={detected_at_ms}ms)")
            return True
        except Exception as e:
            db.rollback()
            print(f"❌ 创建测试告警失败: {e}")
            return False
        finally:
            db.close()

    @staticmethod
    def cleanup_test_alarms_for_task(task_id: int):
        """删除指定任务下的全部告警记录。"""
        db = next(get_db())
        try:
            deleted = db.query(DBAlarm).filter(DBAlarm.task_id == task_id).delete()
            db.commit()
            if deleted:
                print(f"✅ 清理 task {task_id} 的 {deleted} 条测试告警")
            else:
                print(f"⚠️  task {task_id} 没有告警记录，无需清理")
        except Exception as e:
            db.rollback()
            print(f"⚠️  清理测试告警失败: {e}")
        finally:
            db.close()

    @staticmethod
    @contextmanager
    def test_task(task_id: int = 0, source_ip: str = "test", current_step: str = "1"):
        """上下文管理器：创建测试任务，退出时自动清理

        用法::

            with DatabaseHelper.test_task(task_id=99) as tid:
                # 测试逻辑...
                pass
            # 退出 with 块后自动删除该任务
        """
        created = DatabaseHelper.create_test_task(task_id, source_ip, current_step=current_step)
        try:
            yield task_id
        finally:
            if created:
                DatabaseHelper.cleanup_test_task(task_id)


class APIClient:
    """后端 API 客户端"""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
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
            response = requests.get(f"{self.base_url}/health/status", timeout=2)
            return response.status_code == 200
        except Exception:
            return False

    def unified_start(
        self, task_id: int, rtsp_url: str, fps: int = 30
    ) -> Dict[str, Any]:
        """统一启动接口（推荐）- 合并 load_task + start_rtsp_stream"""
        url = f"{self.base_url}/api/start"
        payload = {"task_id": task_id, "rtsp_url": rtsp_url, "fps": fps}

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}

    def unified_terminate(self, client_id: str) -> Dict[str, Any]:
        """统一终止接口（推荐）- 完整清理：解码器 + 推理 + ClientManager"""
        url = f"{self.base_url}/api/terminate"
        params = {"client_id": client_id}

        try:
            response = requests.post(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}

def seed_hls_segments(
    task_id: int,
    step_id: int,
    ts_us_list: List[int],
    base_dir: Optional[Path] = None,
    segment_duration: float = 10.0,
) -> Path:
    """在 base_dir/{task_id}/{step_id}/ 下创建假 HLS 段文件，供追溯接口测试使用。

    每个 ts_us 会生成：
      - raw_segment_{ts_us}.mp4        （16 字节哑文件）
      - processed_segment_{ts_us}.mp4  （16 字节哑文件）
    另外生成 raw_playlist.m3u8 和 processed_playlist.m3u8（实时播放列表格式，无 EXT-X-ENDLIST）。

    Returns:
        task_dir Path，调用方在 finally 中用 shutil.rmtree 清理整个目录。
    """
    if base_dir is None:
        try:
            from app.services.traceback.segment_finder import get_default_base_dir
            base_dir = get_default_base_dir()
        except Exception:
            project_root = Path(__file__).parent.parent.resolve()
            base_dir = (project_root / "database").resolve()

    task_dir = Path(base_dir) / str(task_id) / str(step_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    for ts_us in ts_us_list:
        (task_dir / f"raw_segment_{ts_us}.mp4").write_bytes(b"\x00" * 16)
        (task_dir / f"processed_segment_{ts_us}.mp4").write_bytes(b"\x00" * 16)

    def _make_playlist(track: str) -> str:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{int(segment_duration)}",
        ]
        for ts_us in ts_us_list:
            lines.append(f"#EXTINF:{segment_duration:.3f},")
            lines.append(f"{track}_segment_{ts_us}.mp4")
        return "\n".join(lines) + "\n"

    (task_dir / "raw_playlist.m3u8").write_text(_make_playlist("raw"), encoding="utf-8")
    (task_dir / "processed_playlist.m3u8").write_text(
        _make_playlist("processed"), encoding="utf-8"
    )

    print(f"✅ 创建测试 HLS 段: {task_dir} ({len(ts_us_list)} 段/轨道)")
    return task_dir


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
        "playlists": [],
    }

    if task_dir.exists():
        # 查找视频段
        result["segments"] = [str(f) for f in task_dir.glob("*_segment_*.mp4")]
        # 查找播放列表
        result["playlists"] = [str(f) for f in task_dir.glob("*.m3u8")]

    return result


def wait_for_condition(
    condition_func,
    timeout: int = 30,
    interval: float = 1.0,
    description: str = "条件满足",
) -> bool:
    """等待条件满足"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition_func():
            return True
        time.sleep(interval)

    print(f"⏱️ 超时：{description} 未在 {timeout} 秒内满足")
    return False
