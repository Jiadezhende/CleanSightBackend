"""
健康监控路由

提供全局健康监控服务的状态查询接口。生命周期归 `app.services.health_monitor.lifespan()`
（由 `main.py` 嵌套），本模块只读不管起停。
"""

import logging

from fastapi import APIRouter

from app.services.health_monitor.instance import health_monitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


def get_health_monitor():
    """获取全局健康监控实例

    职责边界：
    - 这是访问全局健康监控的唯一入口
    - API 路由通过此方法获取健康监控实例
    - 健康监控负责协调所有清理操作

    单例本身在 `app.services.health_monitor.instance`，本函数只是给 router 侧留的
    旧调用面；服务侧（非 router）要用请直接 import 单例，别反向依赖 `app.routers`。

    Returns:
        GlobalHealthMonitor 单例（恒非 None；未启动时 `is_running` 为 False）

    Usage:
        from app.routers.health import get_health_monitor

        monitor = get_health_monitor()
        result = monitor.cleanup_client(client_id, "API termination request")
    """
    return health_monitor


@router.get("/monitor/stats")
async def get_monitor_stats():
    """
    获取健康监控统计信息（累计统计）

    返回监控循环的累计统计数据（自启动以来的总计）：
    - checks: 监控循环执行次数
    - disconnects: 检测到断线、进入重连模式的总次数（含首启失败）
    - cleanups: 执行完整清理的总次数
    - reconnects: 发起 respawn 的总次数
    - reconnect_successes: 重连成功的总次数
    - orphans_detected: 检测到孤儿（孤儿流 + 孤儿解码器）的总次数
    - reconnecting_count: 当前重连中的客户端数量（实时快照）
    - reconnecting_clients: 当前重连中的客户端ID列表（实时快照）

    注意：
    - 前 6 项为累计统计，持续增长
    - 后 2 项为实时快照，反映当前状态

    GET /health/monitor/stats
    """
    if not health_monitor.is_running:
        return {
            "status": "not_initialized",
            "message": "Health monitor not initialized",
        }

    stats = health_monitor.get_stats()
    return {"status": "running", **stats}


@router.get("/monitor/config")
async def get_monitor_config():
    """
    获取健康监控配置信息

    返回监控配置参数：
    - check_interval: 检查间隔（秒）
    - heartbeat_timeout: 心跳超时（秒）
    - reconnect_interval: 重连间隔（秒）
    - cleanup_timeout: 无帧多久放弃重连并清理（秒）
    - orphan_timeout: 孤儿流超时（秒）

    GET /health/monitor/config
    """
    if not health_monitor.is_running:
        return {
            "status": "not_initialized",
            "message": "Health monitor not initialized",
        }

    config = health_monitor.config
    return {
        "status": "running",
        "config": {
            "check_interval": config.check_interval,
            "heartbeat_timeout": config.heartbeat_timeout,
            "reconnect_interval": config.reconnect_interval,
            "cleanup_timeout": config.cleanup_timeout,
            "orphan_timeout": config.orphan_timeout,
        },
        # 原 "derived" 块已删：cleanup_timeout 提成一等配置项后，块里两个值都退化成 config
        # 的恒等副本（suspect_timeout ≡ heartbeat_timeout），同一响应里回显两遍纯属冗余。
    }


@router.get("/status")
async def get_system_status():
    """
    获取系统整体状态（实时快照）

    职责边界：
    - 健康监控负责系统级别的状态汇总
    - 整合来自多个模块的信息（ClientManager、StreamService、InferenceManager）
    - 提供统一的系统状态视图
    - 替代 /ai/status 端点（推荐使用此端点）

    返回系统状态信息：
    - clients: 客户端实时统计（来自最近一次监控检查的快照）
      - total_clients: 总客户端数（有队列的客户端）
      - active_streams: 活跃流数量（有解码器且不在重连中的客户端）
      - reconnecting: 重连中的客户端数量
      - orphan_streams: 孤儿流数量（有队列但无解码器且不在重连中）
      - orphan_decoders: 孤儿解码器数量（有解码器但无队列且不在重连中）

      注意：各分类互斥，total_clients = active_streams + reconnecting + orphan_streams

    - queues: 各客户端的队列状态详情（字典，key 为 client_id）
      - raw_queue_size: 原始帧队列大小
      - ready_queue_size: 就绪帧队列大小
      - latest_timestamp: 最新帧时间戳
      - ... (更多队列状态信息)

    - monitor_stats: 监控累计统计信息（自启动以来的总计）
      - checks: 监控循环执行次数
      - disconnects: 检测到断线、进入重连模式的总次数（含首启失败）
      - cleanups: 执行完整清理的总次数
      - reconnects: 发起 respawn 的总次数
      - reconnect_successes: 重连成功的总次数
      - orphans_detected: 检测到孤儿的总次数
      - reconnecting_clients: 当前重连中的客户端ID列表

    性能特性：
    - 客户端统计为缓存数据（每 check_interval 更新一次，默认 1 秒）
    - 队列状态为实时查询（调用时获取）
    - 延迟 < 1ms（无重计算开销）

    GET /health/status
    """
    if not health_monitor.is_running:
        return {
            "status": "not_initialized",
            "message": "Health monitor not initialized",
        }

    system_status = health_monitor.get_system_status()
    return {"status": "running", **system_status}
