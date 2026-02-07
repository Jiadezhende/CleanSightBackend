"""
健康监控路由

提供全局健康监控服务的生命周期管理和状态查询接口
"""
from contextlib import asynccontextmanager
from fastapi import APIRouter
from app.services.health_monitor import GlobalHealthMonitor, get_health_monitor_config
from app.services.client.manager import client_manager
from app.services.stream import stream_service
from app.services import ai

router = APIRouter(prefix="/health", tags=["health"])

# 全局健康监控实例（由 lifespan 管理）
_health_monitor: GlobalHealthMonitor | None = None


def get_health_monitor() -> GlobalHealthMonitor | None:
    """获取全局健康监控实例

    职责边界：
    - 这是访问全局健康监控的唯一入口
    - API 路由通过此方法获取健康监控实例
    - 健康监控负责协调所有清理操作

    Returns:
        GlobalHealthMonitor instance if initialized, None otherwise

    Usage:
        from app.routers.health import get_health_monitor

        monitor = get_health_monitor()
        if monitor:
            result = monitor.cleanup_client(client_id, "API termination request")
    """
    return _health_monitor


@asynccontextmanager
async def lifespan():
    """全局健康监控服务生命周期管理"""
    global _health_monitor

    # 加载健康监控配置
    health_config = get_health_monitor_config()

    # 创建并启动健康监控（传入配置）
    _health_monitor = GlobalHealthMonitor(
        client_manager=client_manager,
        stream_service=stream_service,
        inference_manager=ai.manager,
        config=health_config
    )
    _health_monitor.start()
    print("✅ 全局健康监控已启动")

    try:
        yield
    finally:
        # 停止健康监控
        if _health_monitor:
            _health_monitor.stop()
            print("✅ 全局健康监控已停止")


@router.get("/monitor/stats")
async def get_monitor_stats():
    """
    获取健康监控统计信息

    返回监控循环的统计数据：
    - checks: 检查次数
    - suspects: 检测到的可疑断流次数
    - cleanups: 完整清理次数
    - reconnects: 重连尝试次数
    - reconnect_successes: 重连成功次数
    - orphans_detected: 孤儿流检测次数
    - reconnecting_count: 当前重连中的客户端数量
    - reconnecting_clients: 当前重连中的客户端ID列表

    GET /health/monitor/stats
    """
    if _health_monitor is None:
        return {
            "status": "not_initialized",
            "message": "Health monitor not initialized"
        }

    stats = _health_monitor.get_stats()
    return {
        "status": "running",
        **stats
    }


@router.get("/monitor/config")
async def get_monitor_config():
    """
    获取健康监控配置信息

    返回监控配置参数：
    - check_interval: 检查间隔（秒）
    - heartbeat_timeout: 心跳超时（秒）
    - restart_delay: 重连延迟（秒）
    - max_restart_attempts: 最大重连次数
    - orphan_timeout: 孤儿流超时（秒）

    GET /health/monitor/config
    """
    if _health_monitor is None:
        return {
            "status": "not_initialized",
            "message": "Health monitor not initialized"
        }

    config = _health_monitor.config
    return {
        "status": "running",
        "config": {
            "check_interval": config.check_interval,
            "heartbeat_timeout": config.heartbeat_timeout,
            "restart_delay": config.restart_delay,
            "max_restart_attempts": config.max_restart_attempts,
            "orphan_timeout": config.orphan_timeout,
        },
        "derived": {
            "suspect_timeout": _health_monitor.suspect_timeout,
            "cleanup_timeout": _health_monitor.cleanup_timeout,
            "reconnect_interval": _health_monitor.reconnect_interval,
            "max_reconnect_attempts": _health_monitor.max_reconnect_attempts,
        }
    }


@router.get("/status")
async def get_system_status():
    """
    获取系统整体状态

    职责边界：
    - 健康监控负责系统级别的状态汇总
    - 整合来自多个模块的信息（ClientManager、StreamService、InferenceManager）
    - 提供统一的系统状态视图
    - 替代 /ai/status 端点（推荐使用此端点）

    返回系统状态信息：
    - clients: 客户端统计
      - total: 总客户端数（有队列的）
      - active_streams: 活跃流数量（有解码器的）
      - reconnecting: 重连中的客户端数量
      - orphans: 孤儿流数量（有队列但无解码器）
    - queues: 各客户端的队列状态详情
      - raw_queue_size: 原始帧队列大小
      - ready_queue_size: 就绪帧队列大小
      - latest_timestamp: 最新帧时间戳
    - monitor_stats: 监控统计信息
      - checks: 检查次数
      - suspects: 检测到的可疑断流次数
      - cleanups: 完整清理次数
      - reconnects: 重连尝试次数
      - reconnect_successes: 重连成功次数
      - orphans_detected: 孤儿流检测次数

    GET /health/status
    """
    if _health_monitor is None:
        return {
            "status": "not_initialized",
            "message": "Health monitor not initialized"
        }

    system_status = _health_monitor.get_system_status()
    return {
        "status": "running",
        **system_status
    }
