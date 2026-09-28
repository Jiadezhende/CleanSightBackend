"""client 中台全局单例（唯一定义处）

`service.py` 只放类（测试可自由构造），要那一个全局注册表的人才 import 本模块：

    from app.services.client.instance import client_service

client 中台是零跨服务依赖的 leaf，不受规范 §6 单例引用面限制，谁都可以向下依赖它。
"""

from .service import ClientService

client_service: ClientService = ClientService()
