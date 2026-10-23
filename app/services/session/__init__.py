"""session：run 注册表 + 按 run 隔离的进程内 mq。

本 `__init__` 不做 re-export，消费方走深路径：

    单例      from app.services.session.instance import session_service
    基建      from app.services.session.core import Topic, Latest, Fifo, TimeWindow, Session, SessionInfo

topic 由使用方模块声明（跨模块共享的放 `session/topics.py`），经 `sess.channel(TOPIC)` 读写。
"""
