"""
告警持久化策略

负责：
- 告警批量去重
- 冷却期管理
- HTTP上报（带重试）
- 数据库记录
"""

from typing import Dict, Any, Optional, List, Tuple
import threading
import time
import json
import logging
import urllib.request
from datetime import datetime

from app.settings import settings
from app.database import engine
from sqlalchemy import text

logger = logging.getLogger(__name__)


class AlarmPersistenceStrategy:
    """告警持久化策略"""

    def __init__(
        self,
        batch_interval: int = 30,
        cooldown_seconds: int = 60,
        retry_times: int = 3,
        retry_backoff: float = 1.0
    ):
        self.batch_interval = batch_interval
        self.cooldown_seconds = cooldown_seconds
        self.retry_times = retry_times
        self.retry_backoff = retry_backoff

        # 批量去重
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}  # key -> {count, first_seen, last_seen, alarm_info}
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
                    'count': 1,
                    'first_seen': time.time(),
                    'last_seen': time.time(),
                    'alarm_info': alarm_info
                }
            else:
                self._pending[task_key]['count'] += 1
                self._pending[task_key]['last_seen'] = time.time()

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
                agg_alarm = dict(item['alarm_info'])
                agg_alarm['alarm_count'] = item.get('count', 1)
                agg_alarm['first_seen'] = datetime.fromtimestamp(
                    item.get('first_seen')
                ).strftime("%Y-%m-%d %H:%M:%S")
                agg_alarm['last_seen'] = datetime.fromtimestamp(
                    item.get('last_seen')
                ).strftime("%Y-%m-%d %H:%M:%S")

                to_report.append((key, agg_alarm))

                # 更新最近上报时间并移除pending
                self._recent[key] = now
                del self._pending[key]

        return to_report

    def report_alarm(self, alarm_info: Dict[str, Any]) -> bool:
        """上报告警（HTTP + 数据库）"""
        success = True

        # 1. HTTP上报
        if self._should_send_http(alarm_info):
            http_success = self._send_alarm_http(alarm_info)
            if not http_success:
                success = False
                logger.warning("HTTP上报失败，但继续数据库记录")

        # 2. 数据库记录
        try:
            self._record_alarm_db(alarm_info)
        except Exception as e:
            logger.error("数据库记录失败: %s", e, exc_info=True)
            success = False

        return success

    def _should_send_http(self, alarm_info: Dict[str, Any]) -> bool:
        """检查是否需要HTTP上报（需要task_id和step_id）"""
        return alarm_info.get('task_id') and alarm_info.get('step_id')

    def _send_alarm_http(self, alarm_info: Dict[str, Any]) -> bool:
        """HTTP上报告警（带重试，从InferenceManager._send_alarm_report迁移）"""
        url = settings.alarm_report_url

        payload = {
            "task_id": alarm_info.get('task_id', 0),
            "step_id": alarm_info.get('step_id', 0),
            "alarm_type": alarm_info.get('alarm_type', '流程违规'),
            "alarm_level": alarm_info.get('alarm_level', 'high'),
            "alarm_message": alarm_info.get('alarm_message', 'AI推理检测到异常'),
            "alarm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # 可选字段
        if alarm_info.get('detection_result'):
            payload['detection_result'] = alarm_info['detection_result']
        if alarm_info.get('alarm_count'):
            payload['alarm_count'] = int(alarm_info['alarm_count'])
        if alarm_info.get('first_seen'):
            payload['first_seen'] = alarm_info['first_seen']
        if alarm_info.get('last_seen'):
            payload['last_seen'] = alarm_info['last_seen']

        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'CleanSight-Backend/1.0'
        }

        # 重试逻辑
        backoff = self.retry_backoff
        for attempt in range(1, self.retry_times + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_text = resp.read().decode('utf-8')
                    try:
                        j = json.loads(resp_text)
                        if j.get('code') == 0:
                            logger.info("告警上报成功: task_id=%s", payload['task_id'])
                            return True
                        else:
                            logger.warning("告警上报返回错误: %s", j)
                            return False
                    except Exception:
                        logger.warning("告警上报响应非JSON: %s", resp_text)
                        return False
            except Exception as e:
                logger.warning("告警上报尝试 %d 失败: %s", attempt, e)
                if attempt < self.retry_times:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                else:
                    return False

        return False

    def _record_alarm_db(self, alarm_info: Dict[str, Any]):
        """记录告警到数据库（从InferenceManager._record_alarm_db迁移）"""
        create_sql = '''
        CREATE TABLE IF NOT EXISTS clean_alarm (
            alarm_id SERIAL PRIMARY KEY,
            task_id INTEGER,
            step_id INTEGER,
            alarm_type TEXT,
            message TEXT,
            severity TEXT,
            resolved BOOLEAN DEFAULT FALSE,
            resolved_by INTEGER,
            detected_at BIGINT,
            resolved_at BIGINT,
            created_at TIMESTAMP DEFAULT now(),
            alarm_count INTEGER DEFAULT 1,
            first_seen BIGINT,
            last_seen BIGINT
        )
        '''

        with engine.begin() as conn:
            conn.execute(text(create_sql))

            insert_sql = '''
            INSERT INTO clean_alarm
            (task_id, step_id, alarm_type, message, severity, detected_at, alarm_count, first_seen, last_seen)
            VALUES (:task_id, :step_id, :alarm_type, :message, :severity, :detected_at, :alarm_count, :first_seen, :last_seen)
            '''

            params = {
                'task_id': alarm_info.get('task_id'),
                'step_id': alarm_info.get('step_id'),
                'alarm_type': alarm_info.get('alarm_type', '流程违规'),
                'message': alarm_info.get('alarm_message', 'AI推理检测到异常'),
                'severity': alarm_info.get('alarm_level', 'high'),
                'detected_at': int(time.time()),
                'alarm_count': alarm_info.get('alarm_count', 1),
                'first_seen': self._parse_timestamp(alarm_info.get('first_seen')),
                'last_seen': self._parse_timestamp(alarm_info.get('last_seen')),
            }

            conn.execute(text(insert_sql), params)
            logger.info("告警记录到数据库: task_id=%s, alarm_count=%s", params['task_id'], params['alarm_count'])

    def _parse_timestamp(self, ts_str: Optional[str]) -> Optional[int]:
        """解析时间戳字符串"""
        if not ts_str:
            return None
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            return None
