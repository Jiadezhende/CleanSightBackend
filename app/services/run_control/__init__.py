"""运行控制：跨服务编排一次 run 的启停（控制面唯一编排出口）。

本 `__init__` 不做 re-export，消费方走深路径：

    单例      from app.services.run_control.instance import run_control_service
    类        from app.services.run_control.service import RunControlService
"""
