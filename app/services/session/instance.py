"""session 注册表全局单例（唯一定义处）。

    from app.services.session.instance import session_service

session 包是零跨服务依赖的 leaf，谁都可以向下依赖它。
"""

from .service import SessionService

session_service: SessionService = SessionService()
