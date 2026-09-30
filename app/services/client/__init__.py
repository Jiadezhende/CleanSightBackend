"""客户端中台：跨服务共享的运行态注册表（COW，`int task_id` 键）+ 每次 run 的 ClientQueues。

本 `__init__` 不做 re-export，消费方走深路径：

    单例      from app.services.client.instance import client_service
    类        from app.services.client.service import ClientService
    CQ        from app.services.client.queues import ClientQueues
"""
