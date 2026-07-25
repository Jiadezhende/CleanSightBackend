"""
运维 Admin API — 聚合仪表盘、Prometheus 指标、延迟探针
路由前缀：/admin-f3m8（路径混淆，防自动扫描器命中）
告警查询直接使用 GET /task/{task_id}/alarms（已有双源路由实现）
"""

import time
import logging

from fastapi import APIRouter, Query

from app.services.client.manager import client_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin-f3m8", tags=["admin"])


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _client_info(client_id: int, client_queues) -> dict:
    depths = client_queues.get_queue_depths()
    return {
        "client_id": client_id,  # 注册表键 = task_id(int)
        "task_id": client_queues.task_id,
        "source_ip": client_queues.source_ip,  # /ai/video 按 source_ip 路由，前端据此连 WS
        "step_id": client_queues.step_id,
        "queue_depths": depths,
    }


def _parse_metrics_json() -> dict:
    """从 Prometheus REGISTRY 提取 5 个核心指标，返回结构化 JSON。"""
    from prometheus_client import REGISTRY

    # 按 metric family 名聚合
    families: dict = {}
    for metric_family in REGISTRY.collect():
        families[metric_family.name] = metric_family

    result: dict = {}

    # 1. 推理延迟 Histogram — P50 / P95 / P99
    latency_fam = families.get("infer_latency_ms")
    if latency_fam:
        buckets_by_model: dict = {}
        counts_by_model: dict = {}
        for sample in latency_fam.samples:
            model = sample.labels.get("model", "unknown")
            if sample.name.endswith("_bucket"):
                le = float(sample.labels.get("le", "inf"))
                buckets_by_model.setdefault(model, []).append((le, sample.value))
            elif sample.name.endswith("_count"):
                counts_by_model[model] = sample.value

        latency_result: dict = {}
        for model, raw_buckets in buckets_by_model.items():
            raw_buckets.sort(key=lambda x: x[0])
            total = counts_by_model.get(model, 0)
            latency_result[model] = {
                "p50": _quantile(raw_buckets, 0.50, total),
                "p95": _quantile(raw_buckets, 0.95, total),
                "p99": _quantile(raw_buckets, 0.99, total),
                "total_count": int(total),
            }
        result["infer_latency_ms"] = latency_result

    # 2. 推理失败 Counter（family 名去 _total 后缀：prometheus 对 Counter 剥 _total）
    fail_fam = families.get("infer_failure")
    if fail_fam:
        by_type: dict = {}
        total_fail = 0
        for sample in fail_fam.samples:
            if not sample.name.endswith("_total"):
                continue
            error_type = sample.labels.get("error_type", "unknown")
            by_type[error_type] = by_type.get(error_type, 0) + sample.value
            total_fail += sample.value
        result["infer_failure_total"] = {"total": int(total_fail), "by_type": by_type}

    # 3. 帧丢弃 Counter（family 名去 _total）
    drop_fam = families.get("frame_drop")
    if drop_fam:
        by_reason: dict = {}
        total_drop = 0
        for sample in drop_fam.samples:
            if not sample.name.endswith("_total"):
                continue
            reason = sample.labels.get("reason", "unknown")
            by_reason[reason] = by_reason.get(reason, 0) + sample.value
            total_drop += sample.value
        result["frame_drop_total"] = {"total": int(total_drop), "by_reason": by_reason}

    # 4. GPU OOM Counter（family 名去 _total）
    oom_fam = families.get("gpu_oom")
    if oom_fam:
        total_oom = sum(s.value for s in oom_fam.samples if s.name.endswith("_total"))
        result["gpu_oom_total"] = int(total_oom)

    # 5. 重试 Counter（family 名去 _total）
    retry_fam = families.get("retry")
    if retry_fam:
        by_op: dict = {}
        total_retry = 0
        for sample in retry_fam.samples:
            if not sample.name.endswith("_total"):
                continue
            op = sample.labels.get("operation", "unknown")
            by_op[op] = by_op.get(op, 0) + sample.value
            total_retry += sample.value
        result["retry_total"] = {"total": int(total_retry), "by_operation": by_op}

    return result


def _quantile(sorted_buckets: list, q: float, total: float) -> float:
    """从排好序的 (upper_bound, cumulative_count) 列表估算分位数。"""
    if not sorted_buckets or total <= 0:
        return 0.0
    target = q * total
    prev_upper = 0.0
    prev_count = 0.0
    for upper, count in sorted_buckets:
        if upper == float("inf"):
            break
        if count >= target:
            bucket_count = count - prev_count
            if bucket_count <= 0:
                return upper
            fraction = (target - prev_count) / bucket_count
            return round(prev_upper + fraction * (upper - prev_upper), 3)
        prev_upper = upper
        prev_count = count
    # 超出最大 bucket — 返回最大有限上界
    finite = [u for u, _ in sorted_buckets if u != float("inf")]
    return float(finite[-1]) if finite else 0.0


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@router.get("/overview")
def get_overview():
    """聚合仪表盘：活跃 run（任务）、队列深度。前端每 3s 轮询。

    换键后注册表键即 task_id，一条目 = 一个活跃 run；响应键 `clients`/`client_id`
    沿用旧名（admin 页 wire，值为 task_id），语义已是 run/任务。
    """
    all_clients = client_manager.snapshot()
    clients_info = [_client_info(cid, q) for cid, q in all_clients.items()]
    total_queued = sum(
        d["queue_depths"].get("ca_ready", 0)
        + d["queue_depths"].get("ca_raw", 0)
        + d["queue_depths"].get("ca_processed", 0)
        for d in clients_info
    )
    return {
        "timestamp": int(time.time()),
        "active_clients": len(clients_info),
        "total_queued_frames": total_queued,
        "clients": clients_info,
    }


@router.get("/clients")
def get_clients():
    """活跃 run（任务）列表（轻量），供前端下拉框使用。

    一 run 一 CQ = `registry[task_id]`；响应/路径的 `clients`·`client_id` 为 admin 页 wire 旧名（值=task_id）。
    """
    all_clients = client_manager.snapshot()
    return [_client_info(cid, q) for cid, q in all_clients.items()]


@router.get("/clients/{client_id}/alarms")
def get_client_alarms(client_id: int, n: int = Query(20, ge=1, le=100)):
    """从内存告警日志读取该 run（task_id）最近 n 条告警（不走 DB）。"""
    if not client_manager.has_client(client_id):
        return {"client_id": client_id, "alarms": [], "error": "client_not_found"}
    cq = client_manager.get(client_id)
    alarms = cq.get_recent_alarms(n=n)
    return {
        "client_id": client_id,
        "alarms": [
            {
                "seq": a.seq,
                "alarm_type": a.alarm_type,
                "alarm_level": a.alarm_level,
                "alarm_message": a.alarm_message,
                "mode": a.mode,
                "metric": a.metric,
                "stage": a.stage,
                "timestamp": int(a.timestamp),
            }
            for a in alarms
        ],
    }


@router.get("/metrics/json")
def get_metrics_json():
    """Prometheus 5 个核心指标结构化为 JSON，前端每 5s 刷新。"""
    try:
        return _parse_metrics_json()
    except Exception as exc:
        logger.warning("metrics json parse failed: %s", exc)
        return {}


@router.get("/ping")
def ping():
    """延迟测试探针：立即返回服务端毫秒时间戳，前端计算 RTT。"""
    return {"server_time_ms": time.time() * 1000}
