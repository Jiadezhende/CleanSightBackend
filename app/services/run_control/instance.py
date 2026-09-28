"""运行编排全局单例（唯一定义处）

`service.py` 只放类（测试可自由构造），要那一个全局实例的人才 import 本模块：

    from app.services.run_control.instance import run_control_service

按规范 §6，本单例只许被 `routers/*` 与具名例外（健康监控）引用。
"""

from .service import RunControlService

run_control_service: RunControlService = RunControlService()
