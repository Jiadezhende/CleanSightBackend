"""
告警持久化策略

负责：
- 告警批量去重
- 冷却期管理
- HTTP上报到外部数据库
"""

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Tuple

from app.settings import settings

logger = logging.getLogger(__name__)


class AlarmPersistenceStrategy:
    """告警持久化策略"""

    def __init__(
        self,
        batch_interval: int = 30,
        cooldown_seconds: int = 60,
    ):
        self.batch_interval = batch_interval
        self.cooldown_seconds = cooldown_seconds

        # 批量去重
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = (
            {}
        )  # key -> {count, first_seen, last_seen, alarm_info}
        self._recent: Dict[str, float] = {}  # key -> last_report_time

    def should_report(self, task_key: str) -> bool:
        """检查是否应该上报（冷却期检查）"""
        with self._lock:
            last_report = self._recent.get(task_key)
            if last_report is None:
                return True

            return (time.time() - last_report) >= self.cooldown_seconds

    def aggregate_alarm(self, task_key: str, alarm_info: Dict[str, Any]):
        """聚合告警（批量去重）"""
        with self._lock:
            if task_key not in self._pending:
                self._pending[task_key] = {
                    "count": 1,
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                    "alarm_info": alarm_info,
                }
            else:
                self._pending[task_key]["count"] += 1
                self._pending[task_key]["last_seen"] = time.time()

    def flush_pending_alarms(self) -> List[Tuple[str, Dict[str, Any]]]:
        """刷新待处理告警（返回需要上报的告警列表）"""
        now = time.time()
        to_report = []

        with self._lock:
            keys = list(self._pending.keys())
            for key in keys:
                item = self._pending.get(key)
                if not item:
                    continue

                # 检查冷却期
                last_sent = self._recent.get(key)
                if last_sent and (now - last_sent) < self.cooldown_seconds:
                    continue

                # 构建聚合告警
                agg_alarm = dict(item["alarm_info"])
                agg_alarm["alarm_count"] = item.get("count", 1)
                agg_alarm["first_seen"] = datetime.fromtimestamp(
                    item.get("first_seen")
                ).strftime("%Y-%m-%d %H:%M:%S")
                agg_alarm["last_seen"] = datetime.fromtimestamp(
                    item.get("last_seen")
                ).strftime("%Y-%m-%d %H:%M:%S")

                to_report.append((key, agg_alarm))

                # 更新最近上报时间并移除pending
                self._recent[key] = now
                del self._pending[key]

        return to_report

    def report_alarm(self, alarm_info: Dict[str, Any]):
        """上报告警到外部数据库（通过HTTP API）
        
        Raises:
            PersistenceError: HTTP上报失败
        """
        from app.utils.exceptions import PersistenceError
        
        client_id = alarm_info.get("client_id", "unknown")

        # HTTP上报到外部数据库
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
        return alarm_info.get("task_id") and alarm_info.get("step_id")

    def _send_alarm_http(self, alarm_info: Dict[str, Any]) -> bool:
        """HTTP上报告警到外部数据库（单次尝试，重试由GuardedExecutor处理）"""
        url = settings.alarm_report_url

        payload = {
            "task_id": alarm_info.get("task_id", 0),
            "step_id": alarm_info.get("step_id", 0),
            "alarm_type": alarm_info.get("alarm_type", "流程违规"),
            "alarm_level": alarm_info.get("alarm_level", "high"),
            "alarm_message": alarm_info.get("alarm_message", "AI推理检测到异常"),
            "alarm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 可选字段
        if alarm_info.get("detection_result"):
            payload["detection_result"] = alarm_info["detection_result"]
        if alarm_info.get("alarm_count"):
            payload["alarm_count"] = int(alarm_info["alarm_count"])
        if alarm_info.get("first_seen"):
            payload["first_seen"] = alarm_info["first_seen"]
        if alarm_info.get("last_seen"):
            payload["last_seen"] = alarm_info["last_seen"]

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

