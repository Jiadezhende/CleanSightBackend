"""
告警持久化策略

负责：
- HTTP上报到外部数据库
"""

import json
import logging
import urllib.request
from datetime import datetime
from typing import Any, Dict

from app.services.inference.data_models import AlarmType
from app.settings import settings

logger = logging.getLogger(__name__)


class AlarmPersistenceStrategy:
    """告警持久化策略（无状态）。去重由 ClientQueues.try_pass_alarm_gate 统一控制。"""

    def __init__(self):
        pass

    def report_alarm(self, alarm_info: Dict[str, Any]):
        """上报告警到外部数据库（通过HTTP API）

        Raises:
            PersistenceError: HTTP上报失败
        """
        from app.utils.exceptions import PersistenceError

        client_id = alarm_info.get("client_id", "unknown")

        if self._should_send_http(alarm_info):
            http_success = self._send_alarm_http(alarm_info)
            if not http_success:
                raise PersistenceError(
                    message="Alarm HTTP report to external database failed",
                    client_id=client_id,
                    operation="alarm_http_report",
                    retryable=True,
                )

    def _should_send_http(self, alarm_info: Dict[str, Any]) -> bool:
        """检查是否需要HTTP上报（需要task_id和step_id）"""
        return bool(alarm_info.get("task_id") and alarm_info.get("step_id") is not None)

    def _send_alarm_http(self, alarm_info: Dict[str, Any]) -> bool:
        """HTTP上报告警到外部数据库（单次尝试，重试由GuardedExecutor处理）"""
        url = settings.alarm_report_url

        step_id = alarm_info.get("step_id")
        if step_id is None:
            logger.error("alarm_info 缺少 step_id，跳过告警上报")
            return False

        payload = {
            "task_id": alarm_info.get("task_id", 0),
            "step_id": step_id,
            "alarm_type": alarm_info.get("alarm_type", AlarmType.PROCESS_VIOLATION),
            "alarm_level": alarm_info.get("alarm_level", "high"),
            "alarm_message": alarm_info.get("alarm_message", "AI推理检测到异常"),
            "alarm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        if alarm_info.get("detection_result"):
            payload["detection_result"] = alarm_info["detection_result"]

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "CleanSight-Backend/1.0",
        }

        try:
            req = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_text = resp.read().decode("utf-8")
                try:
                    j = json.loads(resp_text)
                    if j.get("code") == 0:
                        logger.info("告警上报成功: task_id=%s", payload["task_id"])
                        return True
                    else:
                        logger.warning("告警上报返回错误: %s", j)
                        return False
                except Exception:
                    logger.warning("告警上报响应非JSON: %s", resp_text)
                    return False
        except Exception as e:
            logger.warning("告警上报失败: %s", e)
            return False
