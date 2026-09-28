"""daemons 包标记：按时钟自驱的后台任务。

不属于任何 run、没有调用方向它下发工作；可依赖 services。
routers 只许读它的状态（运行与否 / 统计 / 配置），不许下发命令。

零 re-export，消费方走深路径（`from app.daemons.cleanup.instance import cleanup_worker`）。
"""
